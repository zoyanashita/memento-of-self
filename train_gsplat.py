"""
Memento of Self - Minimal Gaussian Splatting Trainer
Uses gsplat 1.5.3 API directly. No simple_trainer dependency.
Usage: py -3.10 train_gsplat.py --data_dir "captures\20260709_box_workspace" --output_dir "captures\20260709_box_workspace\gsplat_output"
"""

import os
import sys
import argparse
import math
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from tqdm import tqdm

# ── helpers ──────────────────────────────────────────────────────────────────

def load_colmap(data_dir):
    """Read cameras.txt, images.txt, points3D.txt from COLMAP sparse_text."""
    sparse_dir = Path(data_dir) / "sparse_text"
    if not sparse_dir.exists():
        sparse_dir = Path(data_dir) / "sparse" / "0"

    # --- cameras ---
    cameras = {}
    with open(sparse_dir / "cameras.txt") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            cam_id = int(parts[0])
            model  = parts[1]
            w, h   = int(parts[2]), int(parts[3])
            params = list(map(float, parts[4:]))
            cameras[cam_id] = dict(model=model, w=w, h=h, params=params)

    # --- images ---
    images_meta = {}
    image_dir   = Path(data_dir).parent
    with open(sparse_dir / "images.txt") as f:
        lines = [l for l in f if not l.startswith("#") and l.strip()]
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        img_id  = int(parts[0])
        qw,qx,qy,qz = map(float, parts[1:5])
        tx,ty,tz     = map(float, parts[5:8])
        cam_id  = int(parts[8])
        name    = parts[9]
        # quaternion -> rotation matrix (world-to-cam)
        q = np.array([qw,qx,qy,qz])
        R = quat2mat(q)
        t = np.array([tx,ty,tz])
        # camera-to-world
        c2w      = np.eye(4)
        c2w[:3,:3] = R.T
        c2w[:3, 3] = -R.T @ t
        images_meta[img_id] = dict(
            name=name, cam_id=cam_id, c2w=c2w,
            path=str(image_dir / name)
        )
        i += 2   # skip the 2D point line

    # --- points3D ---
    points, colors = [], []
    with open(sparse_dir / "points3D.txt") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            xyz   = list(map(float, parts[1:4]))
            rgb   = list(map(int,   parts[4:7]))
            points.append(xyz)
            colors.append([c/255.0 for c in rgb])

    return cameras, images_meta, np.array(points, dtype=np.float32), np.array(colors, dtype=np.float32)


def quat2mat(q):
    qw,qx,qy,qz = q / np.linalg.norm(q)
    return np.array([
        [1-2*(qy**2+qz**2),   2*(qx*qy-qz*qw),   2*(qx*qz+qy*qw)],
        [  2*(qx*qy+qz*qw), 1-2*(qx**2+qz**2),   2*(qy*qz-qx*qw)],
        [  2*(qx*qz-qy*qw),   2*(qy*qz+qx*qw), 1-2*(qx**2+qy**2)]
    ])


def load_image(path, w, h):
    img = Image.open(path).convert("RGB").resize((w, h), Image.BILINEAR)
    return torch.from_numpy(np.array(img, dtype=np.float32) / 255.0)  # [H,W,3]


def get_K(cam):
    p = cam["params"]
    # OPENCV_FISHEYE or SIMPLE_RADIAL: first params are fx, fy, cx, cy (or f, cx, cy)
    if cam["model"] in ("OPENCV_FISHEYE", "OPENCV"):
        fx,fy,cx,cy = p[0],p[1],p[2],p[3]
    elif cam["model"] == "SIMPLE_RADIAL":
        fx=fy=p[0]; cx,cy=p[1],p[2]
    elif cam["model"] == "RADIAL":
        fx=fy=p[0]; cx,cy=p[1],p[2]
    else:
        fx=fy=p[0]; cx,cy=p[1],p[2]
    return torch.tensor([[fx,0,cx],[0,fy,cy],[0,0,1]], dtype=torch.float32)


def rgb_to_sh0(rgb):
    """Convert RGB to 0th order SH coefficient."""
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0


# ── training ─────────────────────────────────────────────────────────────────

def train(data_dir, output_dir, max_steps=3000, lr=1e-3, device="cuda"):
    from gsplat import rasterization

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print("Loading COLMAP data...")
    cameras, images_meta, pts, pts_rgb = load_colmap(data_dir)

    if len(pts) == 0:
        print("ERROR: No 3D points found. Check sparse_text folder.")
        sys.exit(1)

    print(f"  {len(pts)} 3D points, {len(images_meta)} images")

    # Build training frames
    frames = []
    for img_id, meta in images_meta.items():
        cam = cameras[meta["cam_id"]]
        if not os.path.exists(meta["path"]):
            print(f"  WARNING: {meta['path']} not found, skipping")
            continue
        gt = load_image(meta["path"], cam["w"], cam["h"])  # [H,W,3]
        K  = get_K(cam)
        c2w= torch.tensor(meta["c2w"], dtype=torch.float32)
        frames.append(dict(gt=gt, K=K, c2w=c2w, w=cam["w"], h=cam["h"]))

    if not frames:
        print("ERROR: No images loaded.")
        sys.exit(1)

    print(f"Loaded {len(frames)} training frames.")

    # ── Gaussian parameters ───────────────────────────────────────────────────
    N   = len(pts)
    means     = torch.nn.Parameter(torch.tensor(pts, device=device))
    quats     = torch.nn.Parameter(torch.zeros(N, 4, device=device))
    quats.data[:, 0] = 1.0  # identity rotation

    # Initial scale: average nearest-neighbour distance
    pts_t  = torch.tensor(pts, device=device)
    diffs  = pts_t.unsqueeze(1) - pts_t.unsqueeze(0)          # [N,N,3]
    dists  = diffs.norm(dim=-1)
    dists.fill_diagonal_(1e10)
    nn_dist= dists.min(dim=1).values.clamp(min=1e-4)
    log_s  = torch.log(nn_dist * 0.5).unsqueeze(-1).repeat(1,3)
    scales    = torch.nn.Parameter(log_s.to(device))

    opacities = torch.nn.Parameter(torch.logit(torch.full((N,), 0.1, device=device)))

    sh0_val= rgb_to_sh0(torch.tensor(pts_rgb, device=device))  # [N,3]
    sh0       = torch.nn.Parameter(sh0_val.unsqueeze(1))          # [N,1,3]
    shN       = torch.nn.Parameter(torch.zeros(N, 15, 3, device=device))  # up to deg 3

    optimizer = torch.optim.Adam([
        {"params": means,      "lr": lr * 5},
        {"params": quats,      "lr": lr},
        {"params": scales,     "lr": lr},
        {"params": opacities,  "lr": lr * 5},
        {"params": sh0,        "lr": lr},
        {"params": shN,        "lr": lr / 20},
    ], eps=1e-15)

    # Move frames to device
    for f in frames:
        f["gt"]  = f["gt"].to(device)
        f["K"]   = f["K"].to(device)
        f["c2w"] = f["c2w"].to(device)

    print(f"\nTraining {max_steps} steps on {N} Gaussians...")
    pbar = tqdm(range(max_steps))
    for step in pbar:
        frame = frames[step % len(frames)]
        H, W  = frame["h"], frame["w"]
        K     = frame["K"].unsqueeze(0)           # [1,3,3]
        c2w   = frame["c2w"].unsqueeze(0)         # [1,4,4]
        viewmat = torch.linalg.inv(c2w)           # [1,4,4]

        colors_sh = torch.cat([sh0, shN], dim=1)  # [N,16,3]

        render, alpha, _ = rasterization(
            means      = means,
            quats      = F.normalize(quats, dim=-1),
            scales     = torch.exp(scales),
            opacities  = torch.sigmoid(opacities),
            colors     = colors_sh,
            viewmats   = viewmat,
            Ks         = K,
            width      = W,
            height     = H,
            sh_degree  = 1,
            near_plane = 0.01,
            far_plane  = 1e10,
        )

        gt     = frame["gt"].unsqueeze(0)         # [1,H,W,3]
        render = render[..., :3]                  # [1,H,W,3]

        loss = F.l1_loss(render, gt)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if step % 100 == 0:
            pbar.set_description(f"loss={loss.item():.4f} | N={N}")

    # ── Save .ply ─────────────────────────────────────────────────────────────
    print("\nSaving .ply...")
    ply_path = output_dir / "point_cloud.ply"
    _save_ply(ply_path, means, scales, quats, opacities, sh0, shN)
    print(f"Saved: {ply_path}")

    # Also save a quick rendered image from first frame
    print("Saving preview render...")
    frame  = frames[0]
    H, W   = frame["h"], frame["w"]
    with torch.no_grad():
        colors_sh = torch.cat([sh0, shN], dim=1)
        render, _, _ = rasterization(
            means=means, quats=F.normalize(quats,dim=-1),
            scales=torch.exp(scales), opacities=torch.sigmoid(opacities),
            colors=colors_sh, viewmats=torch.linalg.inv(frame["c2w"].unsqueeze(0)),
            Ks=frame["K"].unsqueeze(0), width=W, height=H,
            sh_degree=1, near_plane=0.01, far_plane=1e10,
        )
    img = (render[0, ..., :3].clamp(0,1).cpu().numpy() * 255).astype(np.uint8)
    Image.fromarray(img).save(output_dir / "render_preview.png")
    print(f"Saved: {output_dir / 'render_preview.png'}")
    print("\nDone!")


def _save_ply(path, means, scales, quats, opacities, sh0, shN):
    """Save Gaussian splat as .ply viewable in 3D viewers."""
    import struct
    m   = means.detach().cpu().numpy()
    s   = torch.exp(scales).detach().cpu().numpy()
    q   = F.normalize(quats, dim=-1).detach().cpu().numpy()
    o   = torch.sigmoid(opacities).detach().cpu().numpy()
    c0  = sh0[:, 0, :].detach().cpu().numpy()      # [N,3] DC term
    N   = len(m)

    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {N}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property float nx\nproperty float ny\nproperty float nz\n"
        "property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n"
        "property float opacity\n"
        "property float scale_0\nproperty float scale_1\nproperty float scale_2\n"
        "property float rot_0\nproperty float rot_1\nproperty float rot_2\nproperty float rot_3\n"
        "end_header\n"
    )

    with open(path, "wb") as f:
        f.write(header.encode())
        for i in range(N):
            f.write(struct.pack("<fffffffffffffffffff",
                float(m[i,0]), float(m[i,1]), float(m[i,2]),
                0.0, 0.0, 0.0,
                float(c0[i,0]), float(c0[i,1]), float(c0[i,2]),
                float(o[i]),
                float(np.log(s[i,0])), float(np.log(s[i,1])), float(np.log(s[i,2])),
                float(q[i,0]), float(q[i,1]), float(q[i,2]), float(q[i,3]),
                0.0, 0.0  # padding to match 19 floats
            ))


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir",   required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--max_steps",  type=int, default=3000)
    parser.add_argument("--lr",         type=float, default=1e-3)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")
    train(args.data_dir, args.output_dir, args.max_steps, args.lr, device)
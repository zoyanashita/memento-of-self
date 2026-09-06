"""
Memento of Self — Post-Capture Pipeline
Runs everything AFTER capture: normalize -> COLMAP -> gsplat (WSL) ->
push to GitHub Pages -> print (with QR code linking to the live render).

Usage: py -3.10 post_process.py <folder_name>
Example: py -3.10 post_process.py 20260723_144845
"""

import subprocess
import sys
import re
import json
import time
from pathlib import Path

MAX_STEPS = 7000


def run(cmd, desc, cwd=None):
    print(f"\n{'='*60}")
    print(f"STEP: {desc}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, shell=True, cwd=cwd)
    if result.returncode != 0:
        print(f"\nERROR: '{desc}' failed with code {result.returncode}")
        return False
    print(f"DONE: {desc}")
    return True


def windows_path_to_wsl(path: Path) -> str:
    p = str(path).replace("\\", "/")
    p = re.sub(r"^([A-Za-z]):", lambda m: f"/mnt/{m.group(1).lower()}", p)
    return p


def get_render_url(render_path, folder_name, project_dir):
    """Run push_to_pages.py and parse the RENDER_URL: line from its output."""
    print(f"\n{'='*60}")
    print("STEP: Push render to GitHub Pages")
    print(f"{'='*60}")

    result = subprocess.run(
        f'py -3.10 .\\push_to_pages.py "{render_path}" "{folder_name}"',
        shell=True,
        cwd=project_dir,
        capture_output=True,
        text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print("WARNING: push to GitHub Pages failed. Continuing without QR link.")
        return None

    for line in result.stdout.splitlines():
        if line.startswith("RENDER_URL:"):
            url = line.split("RENDER_URL:", 1)[1].strip()
            print(f"DONE: Render is live at {url}")
            return url

    print("WARNING: Could not find RENDER_URL in push_to_pages.py output.")
    return None


def process(folder_name, project_dir):
    project_dir = Path(project_dir)

    def write_status(state, extra=None):
        data = {"state": state, "timestamp": time.time()}
        if extra:
            data.update(extra)
        with open(project_dir / "assets" / "_booth_status.json", "w") as f:
            json.dump(data, f)

    # --- Normalize exposure (optional) ---
    normalize_script = project_dir / "normalize_exposure.py"
    if normalize_script.exists():
        if not run(
            f'py -3.10 .\\normalize_exposure.py --image_dir "captures\\{folder_name}"',
            "Exposure normalization",
            cwd=project_dir
        ):
            return False

    # --- COLMAP reconstruction ---
    write_status("reconstructing", {"folder": folder_name})
    if not run(
        f'py -3.10 .\\reconstruct.py --image_dir "captures\\{folder_name}"',
        "COLMAP reconstruction",
        cwd=project_dir
    ):
        return False

    workspace_rel = f"{folder_name}/workspace"  # nested inside the capture folder

    # --- gsplat training via WSL (temp script file avoids quoting issues) ---
    write_status("training", {"folder": folder_name})
    wsl_project_path = windows_path_to_wsl(project_dir)

    script_content = (
        "#!/bin/bash\n"
        "set -e\n"
        "source ~/gsplat_env311/bin/activate\n"
        f"cd \"{wsl_project_path}\"\n"
        "export PATH=\"/usr/local/cuda-12.1/bin:$PATH\"\n"
        "export LD_LIBRARY_PATH=\"/usr/local/cuda-12.1/lib64:$LD_LIBRARY_PATH\"\n"
        "python3 train_gsplat.py "
        f"--data_dir 'captures/{workspace_rel}' "
        f"--output_dir 'captures/{workspace_rel}/gsplat_output' "
        f"--max_steps {MAX_STEPS}\n"
    )

    script_path_win = project_dir / "assets" / "_wsl_train_temp.sh"
    with open(script_path_win, "w", newline="\n") as f:
        f.write(script_content)

    script_path_wsl = windows_path_to_wsl(script_path_win)

    if not run(
        f'wsl bash "{script_path_wsl}"',
        "gsplat training (WSL)",
        cwd=project_dir
    ):
        return False

    render_path = f"captures\\{folder_name}\\workspace\\gsplat_output\\render_preview.png"
    full_render_path = project_dir / "captures" / folder_name / "workspace" / "gsplat_output" / "render_preview.png"

    if not full_render_path.exists():
        print(f"ERROR: Expected render not found at {full_render_path}")
        return False

    # --- Push to GitHub Pages, get the public URL for the QR code ---
    render_url = get_render_url(render_path, folder_name, project_dir)

    # Signal "receipt" state as soon as the render is live, before printing
    # (which the visitor doesn't need to wait on to see their result on screen)
    status = {
        "state": "receipt",
        "folder": folder_name,
        "render_path": render_path.replace("\\", "/"),
        "timestamp": time.time(),
    }
    if render_url:
        status["render_url"] = render_url
    with open(project_dir / "assets" / "_booth_status.json", "w") as f:
        json.dump(status, f)

    # --- Print (pass the URL as an extra argument; falls back gracefully if None) ---
    print_cmd = f'py -3.10 .\\print_receipt.py "{render_path}"'
    if render_url:
        print_cmd += f' "{render_url}"'

    if not run(print_cmd, "Thermal receipt printing", cwd=project_dir):
        return False

    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE!")
    print(f"Capture folder: {folder_name}")
    print(f"Render: {full_render_path}")
    if render_url:
        print(f"Public URL: {render_url}")
    print(f"{'='*60}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: py post_process.py <folder_name>")
        sys.exit(1)

    folder_name = sys.argv[1]
    project_dir = Path(__file__).resolve().parent
    success = process(folder_name, project_dir)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
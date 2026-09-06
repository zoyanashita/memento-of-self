"""
Memento of Self - Reconstruction Pipeline
Runs COLMAP only (feature extraction, matching, sparse reconstruction).
Usage: py -3.10 reconstruct.py --image_dir "captures\20260709_box"
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path

# --- Config ---
COLMAP_BIN = r"C:\colmap\bin\colmap.exe"

def run(cmd, desc):
    print(f"\n{'='*60}")
    print(f"STEP: {desc}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"\nERROR: '{desc}' failed with code {result.returncode}")
        sys.exit(1)
    print(f"DONE: {desc}")

def run_colmap(image_dir, workspace_dir):
    db_path = workspace_dir / "database.db"
    sparse_dir = workspace_dir / "sparse"
    sparse_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Feature extraction
    run(
        f'"{COLMAP_BIN}" feature_extractor '
        f'--image_path "{image_dir}" '
        f'--database_path "{db_path}" '
        f'--ImageReader.single_camera 0 '
        f'--ImageReader.camera_model SIMPLE_RADIAL',
        "COLMAP feature extraction"
    )

    # Step 2: Feature matching
    run(
        f'"{COLMAP_BIN}" exhaustive_matcher '
        f'--database_path "{db_path}"',
        "COLMAP feature matching"
    )

    # Step 3: Sparse reconstruction (mapper)
    run(
        f'"{COLMAP_BIN}" mapper '
        f'--image_path "{image_dir}" '
        f'--database_path "{db_path}" '
        f'--output_path "{sparse_dir}"',
        "COLMAP sparse reconstruction"
    )

    # Step 4: Pick the best reconstruction (COLMAP may produce multiple
    # disconnected models if the cameras split into separate clusters --
    # sparse/0, sparse/1, etc. Convert each candidate to text and count
    # registered images directly from images.txt (most reliable approach).
    candidate_dirs = sorted(
        d for d in sparse_dir.iterdir() if d.is_dir() and d.name.isdigit()
    )
    if not candidate_dirs:
        print("ERROR: No reconstruction models found under sparse/")
        sys.exit(1)

    print(f"Found {len(candidate_dirs)} reconstruction model(s): "
          f"{[d.name for d in candidate_dirs]}")

    best_dir = None
    best_count = -1
    best_text_dir = None

    for candidate in candidate_dirs:
        temp_text_dir = workspace_dir / f"sparse_text_candidate_{candidate.name}"
        temp_text_dir.mkdir(exist_ok=True)

        result = subprocess.run(
            f'"{COLMAP_BIN}" model_converter '
            f'--input_path "{candidate}" '
            f'--output_path "{temp_text_dir}" '
            f'--output_type TXT',
            shell=True, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  Model '{candidate.name}': conversion failed, skipping")
            continue

        images_txt = temp_text_dir / "images.txt"
        if not images_txt.exists():
            print(f"  Model '{candidate.name}': no images.txt produced, skipping")
            continue

        # images.txt has a comment header (lines starting with #), then
        # exactly 2 lines per registered image (pose line + 2D points line).
        data_lines = [
            line for line in images_txt.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        image_count = len(data_lines) // 2

        print(f"  Model '{candidate.name}': {image_count} registered images")

        if image_count > best_count:
            best_count = image_count
            best_dir = candidate
            best_text_dir = temp_text_dir

    if best_dir is None:
        print("ERROR: No valid reconstruction model could be converted.")
        sys.exit(1)

    print(f"Using model '{best_dir.name}' with {best_count} registered images.")

    # Rename the winning candidate's text folder to the canonical name,
    # and clean up the other candidates' temp folders.
    text_dir = workspace_dir / "sparse_text"
    if text_dir.exists():
        import shutil
        shutil.rmtree(text_dir)
    best_text_dir.rename(text_dir)

    for candidate in candidate_dirs:
        leftover = workspace_dir / f"sparse_text_candidate_{candidate.name}"
        if leftover.exists():
            import shutil
            shutil.rmtree(leftover, ignore_errors=True)

    return text_dir

def main():
    parser = argparse.ArgumentParser(description="COLMAP reconstruction pipeline")
    parser.add_argument("--image_dir", required=True, help="Path to folder containing captured images")
    args = parser.parse_args()

    image_dir = Path(args.image_dir).resolve()
    if not image_dir.exists():
        print(f"ERROR: Image directory not found: {image_dir}")
        sys.exit(1)

    images = list(image_dir.glob("*.jpg")) + list(image_dir.glob("*.png"))
    print(f"Found {len(images)} images in {image_dir}")
    if len(images) < 3:
        print("ERROR: Need at least 3 images for reconstruction")
        sys.exit(1)

    workspace_dir = image_dir / "workspace"
    workspace_dir.mkdir(exist_ok=True)
    print(f"Workspace: {workspace_dir}")

    text_dir = run_colmap(image_dir, workspace_dir)

    print(f"\n{'='*60}")
    print("COLMAP COMPLETE!")
    print(f"Point cloud and camera poses saved to: {text_dir}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
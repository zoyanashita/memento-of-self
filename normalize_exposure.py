"""
Memento of Self — Exposure Normalization
Matches brightness/contrast of all camera images to a reference image
using histogram matching. Run this after capture, before COLMAP.

Usage: py -3.10 normalize_exposure.py --image_dir "captures\20260717_test"
"""

import argparse
import numpy as np
from pathlib import Path
from PIL import Image


def match_histogram(source, reference):
    """
    Adjust source image so its per-channel histogram matches reference.
    Both are numpy arrays of shape (H, W, 3), dtype uint8.
    """
    matched = np.zeros_like(source)

    for channel in range(3):
        src_channel = source[..., channel].ravel()
        ref_channel = reference[..., channel].ravel()

        # Compute histograms and CDFs
        src_values, src_counts = np.unique(src_channel, return_counts=True)
        ref_values, ref_counts = np.unique(ref_channel, return_counts=True)

        src_cdf = np.cumsum(src_counts).astype(np.float64)
        src_cdf /= src_cdf[-1]

        ref_cdf = np.cumsum(ref_counts).astype(np.float64)
        ref_cdf /= ref_cdf[-1]

        # Map source values to reference values via CDF matching
        interp_values = np.interp(src_cdf, ref_cdf, ref_values)

        # Build lookup table
        lut = np.zeros(256, dtype=np.uint8)
        for i, val in enumerate(src_values):
            lut[val] = np.clip(interp_values[i], 0, 255)

        matched[..., channel] = lut[source[..., channel]]

    return matched


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image_dir", required=True, help="Folder containing camera_N.jpg images")
    parser.add_argument("--reference", type=int, default=0, help="Index of reference camera (default: 0)")
    args = parser.parse_args()

    image_dir = Path(args.image_dir)
    images = sorted(image_dir.glob("camera_*.jpg"))

    if not images:
        print(f"No camera_*.jpg files found in {image_dir}")
        return

    ref_path = image_dir / f"camera_{args.reference}.jpg"
    if not ref_path.exists():
        print(f"Reference image {ref_path} not found")
        return

    print(f"Reference: {ref_path.name}")
    reference = np.array(Image.open(ref_path).convert("RGB"))

    # Backup originals first
    backup_dir = image_dir / "originals"
    backup_dir.mkdir(exist_ok=True)

    for img_path in images:
        if img_path.name == ref_path.name:
            print(f"  {img_path.name}: skipped (reference)")
            continue

        source = np.array(Image.open(img_path).convert("RGB"))

        # Backup original
        backup_path = backup_dir / img_path.name
        if not backup_path.exists():
            Image.open(img_path).save(backup_path)

        matched = match_histogram(source, reference)
        Image.fromarray(matched).save(img_path)
        print(f"  {img_path.name}: normalized")

    print(f"\nDone. Originals backed up to: {backup_dir}")


if __name__ == "__main__":
    main()
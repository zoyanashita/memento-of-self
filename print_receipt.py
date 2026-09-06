"""
Memento of Self — Thermal Receipt Print Script
Resizes a render to fit the 80mm thermal printer width, enhances it for
darker skin tones, prints it, then prints a QR code linking to the full
color version hosted on GitHub Pages.

Usage: py -3.10 print_receipt.py path/to/render.png [render_url]
If render_url is omitted, the QR code step is skipped.
"""

import sys
import numpy as np
import cv2
from pathlib import Path
from PIL import Image, ImageEnhance
from escpos.printer import Usb

# --- Config ---
VID = 0x0FE6
PID = 0x811E
PRINTER_WIDTH_PX = 512
BOTTOM_PADDING_PX = 120


def apply_clahe(img):
    arr = np.array(img.convert("L"))
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(arr)
    return Image.fromarray(enhanced).convert("RGB")


def apply_gamma(img, gamma=0.6):
    arr = np.array(img).astype(np.float32) / 255.0
    arr = np.power(arr, gamma)
    arr = (arr * 255).astype(np.uint8)
    return Image.fromarray(arr)


def prepare_image(path, bottom_padding=BOTTOM_PADDING_PX, full_width=PRINTER_WIDTH_PX,
                   brightness=1.3, gamma=0.6, left_shift=40):
    img = Image.open(path).convert("RGB")

    img = apply_clahe(img)

    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(brightness)

    img = apply_gamma(img, gamma)

    img = img.rotate(90, expand=True)

    content_width = full_width - left_shift
    w, h = img.size
    scale = content_width / w
    new_w = content_width
    new_h = int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    img = img.convert("1", dither=Image.FLOYDSTEINBERG).convert("RGB")

    canvas = Image.new("RGB", (full_width, new_h + bottom_padding), "white")
    canvas.paste(img, (left_shift, 0))

    return canvas


def main():
    if len(sys.argv) < 2:
        print("Usage: py print_receipt.py path/to/image.png [render_url]")
        sys.exit(1)

    image_path = sys.argv[1]
    render_url = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Preparing image: {image_path}")
    img = prepare_image(image_path)

    preview_path = "assets/print_preview.png"
    img.save(preview_path)
    print(f"Preview saved to: {preview_path} (check this before it prints!)")

    if render_url:
        print(f"QR will link to: {render_url}")
    else:
        print("No render URL provided, will skip QR code.")

    print("Connecting to printer...")
    p = Usb(VID, PID, 0, profile="TM-T88III")

    print("Printing image...")
    p.image(img)

    if render_url:
        print("Printing QR code...")
        p.set(align="center")
        p.text("Scan for full color\n(may take a moment to load)\n")
        p.qr(render_url, size=6)
        p.text("\n")

    p.cut()
    print("Done!")


if __name__ == "__main__":
    main()
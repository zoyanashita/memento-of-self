"""
Memento of Self — GitHub Pages Upload
Copies a render into this project's own repo (now that everything lives
in one consolidated repo) and pushes it, so it becomes publicly
accessible for the QR code.

Usage: py -3.10 push_to_pages.py path/to/render.png folder_name
"""

import sys
import shutil
import subprocess
from pathlib import Path

# --- Config ---
# The project root IS the repo now -- update GITHUB_USERNAME/REPO_NAME
# to match your actual repo.
PAGES_REPO_DIR = Path(__file__).resolve().parent
PROJECT_SUBFOLDER = "web_renders"
GITHUB_USERNAME = "zoyanashita"      # <-- update if needed
REPO_NAME = "memento-of-self"        # <-- update to your actual repo name
BASE_URL = f"https://{GITHUB_USERNAME}.github.io/{REPO_NAME}/{PROJECT_SUBFOLDER}"


def push_render(image_path, folder_name, max_retries=3):
    image_path = Path(image_path)
    if not image_path.exists():
        print(f"ERROR: {image_path} not found")
        return None

    dest_dir = PAGES_REPO_DIR / PROJECT_SUBFOLDER / folder_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / image_path.name
    shutil.copy2(image_path, dest_path)
    print(f"Copied to: {dest_path}")

    rel_path = dest_path.relative_to(PAGES_REPO_DIR)

    import time
    for attempt in range(1, max_retries + 1):
        try:
            subprocess.run(["git", "add", str(rel_path)], cwd=PAGES_REPO_DIR, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"Add capture {folder_name}"],
                cwd=PAGES_REPO_DIR, check=True
            )
            subprocess.run(["git", "push"], cwd=PAGES_REPO_DIR, check=True)
            break
        except subprocess.CalledProcessError as e:
            print(f"Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print("Retrying in 5 seconds...")
                time.sleep(5)
            else:
                print("ERROR: git push failed after all retries.")
                return None

    url = f"{BASE_URL}/{folder_name}/{image_path.name}"
    print(f"Live at: {url}")
    return url


def main():
    if len(sys.argv) < 3:
        print("Usage: py push_to_pages.py path/to/render.png folder_name")
        sys.exit(1)

    image_path = sys.argv[1]
    folder_name = sys.argv[2]

    url = push_render(image_path, folder_name)
    if url is None:
        sys.exit(1)

    # Print just the URL on its own line so calling scripts can capture it
    print(f"RENDER_URL:{url}")


if __name__ == "__main__":
    main()
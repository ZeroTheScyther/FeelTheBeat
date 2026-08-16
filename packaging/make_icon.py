"""
Derive the packaging icons from assets/icon.png (the artwork master).

  assets/icon.ico      multi-resolution, for the Windows exe/tray/taskbar
  assets/icon-256.png  exactly 256x256, for the Linux hicolor theme

Windows picks the nearest embedded size for the 16 px tray; a single-resolution
.ico gets downscaled on the fly and looks soft.  The freedesktop hicolor spec
likewise expects the image in .../256x256/apps/ to actually be 256x256.
Idempotent — safe to re-run.
"""

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "icon.png"
TARGET_ICO = ASSETS / "icon.ico"
TARGET_PNG = ASSETS / "icon-256.png"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"[icon] Missing {SOURCE}")

    img = Image.open(SOURCE).convert("RGBA")

    bbox = img.getbbox()          # trim any transparent margin
    if bbox:
        img = img.crop(bbox)

    side = max(img.size)          # pad to square so nothing is squashed
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))

    big = square.resize((256, 256), Image.LANCZOS)

    # Pillow resamples down from this single image per requested size.
    big.save(TARGET_ICO, format="ICO", sizes=ICO_SIZES)
    big.save(TARGET_PNG, format="PNG")
    print(f"[icon] Wrote {TARGET_ICO} with sizes {[s[0] for s in ICO_SIZES]}")
    print(f"[icon] Wrote {TARGET_PNG} (256x256)")


if __name__ == "__main__":
    main()

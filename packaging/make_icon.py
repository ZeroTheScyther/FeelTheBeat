"""
Generate assets/icon.ico (Windows) and assets/icon.png (Linux desktop entry)
from the first HeavyHit frame, so no binary asset needs committing.
"""

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def source_frame() -> Path:
    frames = sorted((ROOT / "Frames" / "HeavyHit").glob("*.gif"))
    if not frames:
        sys.exit("[icon] No frames found in Frames/HeavyHit")
    # Mid-sequence frame: the character is fully in shot rather than mid-idle.
    return frames[len(frames) // 3]


def main() -> None:
    src = source_frame()
    img = Image.open(src).convert("RGBA")

    bbox = img.getbbox()          # trim the transparent margin
    if bbox:
        img = img.crop(bbox)

    side = max(img.size)          # pad to square so nothing is squashed
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))

    ASSETS.mkdir(exist_ok=True)
    square.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon.png")
    square.save(ASSETS / "icon.ico", format="ICO", sizes=ICO_SIZES)
    print(f"[icon] Wrote {ASSETS/'icon.png'} and {ASSETS/'icon.ico'} from {src.name}")


if __name__ == "__main__":
    main()

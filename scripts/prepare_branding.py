"""Encode the supplied Sol logos as Windows icons without redesign or cropping."""
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/branding"
SIZES = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]


def main():
    for name in ("sol-main", "sol-dark"):
        source = ASSETS / (name + ".jpg")
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original).convert("RGBA")
            if image.width != image.height:
                raise ValueError("Logo 应为正方形，不会自动裁掉原图：" + str(source))
            image.save(ASSETS / (name + ".ico"), format="ICO", sizes=SIZES)
        print(name + ": supplied JPG preserved; Windows icon encoded at 16-256 px")


if __name__ == "__main__":
    main()

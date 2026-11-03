"""Encode the approved transparent artwork; keep the existing tray artwork intact."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/branding"
SIZES = [(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]


def main():
    source = ASSETS / "sol-app-source.png"
    with Image.open(source) as original:
        rgba = original.convert("RGBA")
        bounds = rgba.getchannel("A").getbbox()
        if not bounds or rgba.getchannel("A").getextrema()[0] != 0:
            raise ValueError("应用图标需要非空的透明原图")
        artwork = rgba.crop(bounds)
        # Remove only empty transparent margins; preserve the artwork's aspect ratio.
        side = max(artwork.size)
        margin = round(side * .06)
        canvas = Image.new("RGBA", (side + margin * 2, side + margin * 2))
        canvas.paste(artwork, ((canvas.width-artwork.width)//2, (canvas.height-artwork.height)//2))
        canvas.save(ASSETS / "sol-app.png")
        canvas.save(ASSETS / "sol-app.ico", format="ICO", sizes=SIZES)
    print("Transparent application icon encoded at 16-256 px; tray icons unchanged.")


if __name__ == "__main__":
    main()

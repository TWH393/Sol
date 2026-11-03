"""Color-only audit. Reads palette constants; never imports or starts the application."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def luminance(color):
    channels = [int(color[n:n+2], 16)/255 for n in (1, 3, 5)]
    values = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in channels]
    return sum(v*w for v, w in zip(values, (.2126, .7152, .0722)))


def main():
    source = (ROOT / "desktop/Sol.Desktop/Theme.cs").read_text("utf-8")
    palette = {key: (light, dark) for key, light, dark in re.findall(r'\["([^"]+)"\]\s*=\s*\("(#[A-Fa-f0-9]+)",\s*"(#[A-Fa-f0-9]+)"\)', source)}
    pairs = [("Text", bg) for bg in ("Surface", "Sidebar", "Panel", "Input", "TableRow", "TableAlternate", "Selection")]
    pairs += [("Muted", bg) for bg in ("Surface", "Panel", "Input", "TableHeader")]
    pairs += [("Subtle", bg) for bg in ("Surface", "Sidebar")]
    pairs += [("AccentLabel", "NavActive"), ("AccentText", "Accent"), ("AccentText", "AccentHover"), ("NoticeText", "Notice"), ("ErrorText", "Error"), ("SuccessText", "Success")]
    rows = []
    for mode, index in (("light", 0), ("dark", 1)):
        for foreground, background in pairs:
            low, high = sorted((luminance(palette[foreground][index]), luminance(palette[background][index])))
            ratio = (high+.05)/(low+.05)
            rows.append({"mode": mode, "foreground": foreground, "background": background, "ratio": round(ratio, 2), "pass": ratio >= 4.5})
    failures = [r for r in rows if not r["pass"]]
    (ROOT / "build/theme-color-contrast.json").write_text(json.dumps({"scope": "color contrast only", "threshold": 4.5, "checks": rows}, indent=2), "utf-8")
    print(json.dumps({"pairs": len(rows), "failures": failures, "minimum_ratio": min(r["ratio"] for r in rows)}))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

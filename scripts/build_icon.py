"""Render the original SVG brand asset; optional dependency: CairoSVG==2.8.2."""

from pathlib import Path

import cairosvg

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "custom_components" / "home_heating_optimisation" / "brand"

if __name__ == "__main__":
    DEST.mkdir(exist_ok=True)
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        cairosvg.svg2png(
            url=str(ROOT / "assets" / "icon.svg"),
            write_to=str(DEST / name),
            output_width=size,
            output_height=size,
        )

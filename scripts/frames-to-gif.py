"""Assemble the frames frontend/demo-gif.mjs captured into docs/images/demo.gif.

    uv run --no-project --with pillow python scripts/frames-to-gif.py \
        target/demo-gif/frames docs/images/demo.gif

Frames are scaled to 960 pixels wide; consecutive identical frames
are merged so their durations add up, and each frame gets its own palette.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops

WIDTH = 960


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("frames", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    listed = json.loads((args.frames / "frames.json").read_text(encoding="utf-8"))
    images: list[Image.Image] = []
    durations: list[int] = []
    for item in listed:
        image = Image.open(item["file"]).convert("RGB")
        image = image.resize((WIDTH, round(image.height * WIDTH / image.width)), Image.LANCZOS)
        if images and ImageChops.difference(image, images[-1]).getbbox() is None:
            durations[-1] += item["ms"]
            continue
        images.append(image)
        durations.append(item["ms"])
    # A palette per frame: the screens are mostly flat colours, so 256 per frame keeps
    # small accents (green "Gates passed", status dots) that a shared palette loses.
    frames = [image.quantize(colors=256, method=Image.Quantize.FASTOCTREE) for image in images]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        args.output, save_all=True, append_images=frames[1:], duration=durations, loop=0,
        optimize=True, disposal=1,
    )  # fmt: skip
    size = args.output.stat().st_size / 1024 / 1024
    print(
        f"Wrote {args.output}: {len(frames)} frames, {sum(durations) / 1000:.1f} s, {size:.1f} MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

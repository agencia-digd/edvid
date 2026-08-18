"""Tile frames from N timestamps into ONE labeled PNG — a contact sheet.

The token-cheap way to eyeball several moments at once (Phase-2 verification
stills, cut-boundary spot checks, insert/hook windows): one image instead of N.

Usage:
    python helpers/contact_sheet.py <video> --times 1.0 5.2 9.8 14.3 -o sheet.png
    python helpers/contact_sheet.py <video> --times 3.2 --labels "hook" -o s.png
Options: --cols (default 3), --tile-width (default auto ≤1600px total).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def grab(video: Path, t: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", str(out)],
        check=True,
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Frames at N timestamps → one labeled contact sheet")
    ap.add_argument("video", type=Path)
    ap.add_argument("--times", type=float, nargs="+", required=True, help="timestamps in seconds")
    ap.add_argument("--labels", type=str, default=None,
                    help="comma-separated labels (default: the timestamps)")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--tile-width", type=int, default=0, help="0 = auto (≤1600px total)")
    args = ap.parse_args()

    if not args.video.exists():
        sys.exit(f"video not found: {args.video}")
    labels = ([s.strip() for s in args.labels.split(",")] if args.labels
              else [f"{t:.2f}s" for t in args.times])
    if len(labels) != len(args.times):
        sys.exit("--labels count must match --times count")

    with tempfile.TemporaryDirectory() as tmp:
        with ThreadPoolExecutor(max_workers=8) as ex:
            paths = list(ex.map(
                lambda it: grab(args.video, it[1], Path(tmp) / f"f{it[0]:03d}.png"),
                enumerate(args.times),
            ))
        tiles = [Image.open(p).convert("RGB") for p in paths]

        n = len(tiles)
        cols = max(1, min(args.cols, n))
        rows = (n + cols - 1) // cols
        tile_w = args.tile_width or max(320, min(760, 1600 // cols))
        aspect = tiles[0].height / tiles[0].width
        tile_h = int(tile_w * aspect)
        label_h = 46
        gap = 8
        sheet = Image.new("RGB", (cols * tile_w + (cols - 1) * gap,
                                  rows * (tile_h + label_h) + (rows - 1) * gap), (12, 12, 14))
        draw = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
        except OSError:
            font = ImageFont.load_default()
        for i, (img, label) in enumerate(zip(tiles, labels)):
            cx = (i % cols) * (tile_w + gap)
            cy = (i // cols) * (tile_h + label_h + gap)
            draw.text((cx + 12, cy + 8), label, fill=(255, 255, 255), font=font)
            sheet.paste(img.resize((tile_w, tile_h)), (cx, cy + label_h))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(args.output)

    print(f"{args.output} — {n} frames, {cols}×{rows} grid ({sheet.width}×{sheet.height})")


if __name__ == "__main__":
    main()

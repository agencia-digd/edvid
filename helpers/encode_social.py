"""Encode a social-sized copy of the delivered Reel.

`final.mp4` is the grade master (slow / crf 17 / ~8 Mbps). Instagram and
WhatsApp want something that actually sends: 1080×1920, H.264 high, crf 21,
aac 128k, faststart. Tags stay bt709/tv so the Phase-1 grade does not drift.

Usage:
    uv run python helpers/encode_social.py <edit>/final.mp4
    uv run python helpers/encode_social.py <edit>/final.mp4 -o <edit>/final-social.mp4
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def probe_field(path: Path, key: str) -> str:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", f"stream={key}",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout or "").strip()


def encode(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
               "setsar=1,format=yuv420p,"
               "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        raise SystemExit(proc.returncode)


def main() -> None:
    ap = argparse.ArgumentParser(description="social encode of final.mp4")
    ap.add_argument("src", type=Path, help="edit/final.mp4")
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()
    src = args.src.resolve()
    if not src.exists():
        raise SystemExit(f"não achei {src}")
    dest = args.output.resolve() if args.output else src.with_name("final-social.mp4")
    encode(src, dest)
    src_kb = src.stat().st_size / 1024
    dst_kb = dest.stat().st_size / 1024
    print(
        f"encode_social {dest.name}  "
        f"{src_kb:.0f}k → {dst_kb:.0f}k  "
        f"{probe_field(dest, 'width')}x{probe_field(dest, 'height')}  "
        f"{probe_field(dest, 'codec_name')}"
    )


if __name__ == "__main__":
    main()

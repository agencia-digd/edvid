"""Survey a video visually — scene-aware frames, deduped, tiled into contact sheets.

Ports the /watch skill's frame-selection engine into edvid. contact_sheet.py
answers "show me these N timestamps" (you must already know where to look);
this answers "what is IN this footage?": ffmpeg scene-change detection picks
the distinct moments, a perceptual dedup pass collapses near-identical frames
(a talking head becomes a handful of tiles, not 50), and everything tiles into
labeled contact sheets the LLM reads as ONE image per sheet (Principle 8).

Modes:
  scene (default) — ffmpeg scene filter, exact timestamps via showinfo.
                    Falls back to uniform sampling when the video is
                    effectively static (< 8 detected cuts).
  keyframe        — decode only I-frames (-skip_frame nokey): near-instant,
                    good for a fast inventory of long sources.
  uniform         — fixed-step sampling, forced.

--times pins extra frames at exact timestamps (transcript cues). Moments the
speaker flags verbally ("olha isso", "como voce pode ver") are usually LOW
visual change and invisible to scene detection — read takes_packed.md, pick
the deictic moments, pin them here to decide B-roll/callout/zoom placement.
Cue tiles are labeled "cue" and are never evicted by the frame cap.

Usage:
    python helpers/watch_video.py <video>
    python helpers/watch_video.py <video> --mode keyframe --max-frames 36
    python helpers/watch_video.py cut.mp4 --times 12.5 1:45 3:02
    python helpers/watch_video.py <video> --start 10:00 --end 14:00

Output: sheet PNGs in --out-dir (default <video_dir>/edit/verify/watch_<stem>/)
plus a stdout index of every tile's timestamp. Read the PNGs.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCENE_THRESHOLD = 0.20
SCENE_MIN_FRAMES = 8     # fewer detected cuts than this → effectively static → uniform
MAX_FPS = 2.0            # uniform sampling never denser than this
DEDUP_THUMB = 16         # 16x16 grayscale thumbnails for the perceptual pass
DEDUP_THRESHOLD = 2.0    # mean per-pixel diff (0-255) at or below → near-identical
LABEL_H = 40
GAP = 8
SHEET_MAX_H = 1990       # keep each sheet under the LLM image-read height clamp
SHOWINFO_TS_RE = re.compile(r"pts_time:([0-9.]+)")


def parse_time(value: str | None) -> float | None:
    """SS, MM:SS, or HH:MM:SS (optional .ms) → seconds."""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    sys.exit(f"cannot parse time: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def fmt_time(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _scale(width: int) -> str:
    return (f"scale=w='min({width},iw)':h='min(1998,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2")


def _range_args(start: float | None, end: float | None) -> list[str]:
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    return args


def _collect(out_dir: Path, stderr: str, offset: float, reason: str) -> list[dict]:
    """Pair extracted frame_*.jpg files with showinfo timestamps."""
    stamps = [round(offset + float(m.group(1)), 2) for m in SHOWINFO_TS_RE.finditer(stderr)]
    files = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {"t": stamps[i] if i < len(stamps) else offset, "path": p, "reason": reason}
        for i, p in enumerate(files)
    ]


def extract_scene(video: Path, out_dir: Path, width: int,
                  start: float | None, end: float | None) -> list[dict]:
    """First frame + every scene change, exact timestamps from showinfo."""
    vf = (f"select='eq(n\\,0)+gt(scene\\,{SCENE_THRESHOLD})',"
          f"{_scale(width)},showinfo")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
           *_range_args(start, end),
           "-i", str(video), "-vf", vf, "-vsync", "vfr", "-q:v", "4",
           str(out_dir / "frame_%04d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg scene extraction failed: {r.stderr.strip()[-400:]}")
    return _collect(out_dir, r.stderr, start or 0.0, "scene")


def extract_keyframes(video: Path, out_dir: Path, width: int,
                      start: float | None, end: float | None) -> list[dict]:
    """Decode only I-frames — near-instant; encoders place them on cuts."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
           *_range_args(start, end),
           "-skip_frame", "nokey", "-i", str(video),
           "-vf", f"{_scale(width)},showinfo", "-vsync", "vfr", "-q:v", "4",
           str(out_dir / "frame_%04d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg keyframe extraction failed: {r.stderr.strip()[-400:]}")
    return _collect(out_dir, r.stderr, start or 0.0, "keyframe")


def extract_uniform(video: Path, out_dir: Path, width: int, target: int,
                    start: float | None, end: float | None, duration: float) -> list[dict]:
    fps = min(MAX_FPS, max(target, 1) / max(duration, 0.001))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           *_range_args(start, end),
           "-i", str(video), "-vf", f"fps={fps},{_scale(width)}",
           "-frames:v", str(target), "-q:v", "4",
           str(out_dir / "frame_%04d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg uniform extraction failed: {r.stderr.strip()[-400:]}")
    offset = start or 0.0
    files = sorted(out_dir.glob("frame_%04d.jpg".replace("%04d", "*")))
    return [{"t": round(offset + i / fps, 2), "path": p, "reason": "uniform"}
            for i, p in enumerate(files)]


def grab_cues(video: Path, out_dir: Path, width: int, times: list[float],
              start: float | None, end: float | None) -> tuple[list[dict], int]:
    """One exact frame per requested timestamp; out-of-window cues dropped."""
    lo = start or 0.0
    hi = end if end is not None else float("inf")
    wanted = sorted(set(round(t, 2) for t in times))
    in_window = [t for t in wanted if lo <= t <= hi]
    out: list[dict] = []
    for t in in_window:
        path = out_dir / f"cue_{len(out):04d}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", _scale(width), "-q:v", "4", str(path)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and path.exists():
            out.append({"t": t, "path": path, "reason": "cue"})
    return out, len(wanted) - len(in_window)


def dedupe(frames: list[dict]) -> tuple[list[dict], int]:
    """Greedily drop frames near-identical to the last KEPT one (PIL 16x16 gray)."""
    if len(frames) <= 1:
        return frames, 0
    thumbs = []
    for f in frames:
        with Image.open(f["path"]) as img:
            thumbs.append(img.convert("L").resize((DEDUP_THUMB, DEDUP_THUMB)).tobytes())
    kept = [frames[0]]
    last = thumbs[0]
    dropped = 0
    for f, th in zip(frames[1:], thumbs[1:]):
        delta = sum(abs(a - b) for a, b in zip(th, last)) / len(th)
        if delta <= DEDUP_THRESHOLD:
            dropped += 1
        else:
            kept.append(f)
            last = th
    return kept, dropped


def even_sample(frames: list[dict], n: int) -> list[dict]:
    """n evenly-spaced frames, first and last always kept."""
    if n >= len(frames):
        return frames
    if n <= 1:
        return frames[:1]
    idx = {round(i * (len(frames) - 1) / (n - 1)) for i in range(n)}
    return [f for i, f in enumerate(frames) if i in idx]


def build_sheets(frames: list[dict], out_dir: Path, cols: int) -> list[tuple[Path, list[dict]]]:
    """Tile frames chronologically into labeled sheets, each under SHEET_MAX_H."""
    with Image.open(frames[0]["path"]) as first:
        aspect = first.height / first.width
    tile_w = max(320, min(760, 1600 // cols))
    tile_h = int(tile_w * aspect)
    rows_per_sheet = max(1, (SHEET_MAX_H + GAP) // (tile_h + LABEL_H + GAP))
    per_sheet = cols * rows_per_sheet

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
    except OSError:
        font = ImageFont.load_default()

    sheets: list[tuple[Path, list[dict]]] = []
    pages = [frames[i:i + per_sheet] for i in range(0, len(frames), per_sheet)]
    for page_i, page in enumerate(pages):
        n = len(page)
        pcols = max(1, min(cols, n))
        rows = (n + pcols - 1) // pcols
        sheet = Image.new(
            "RGB",
            (pcols * tile_w + (pcols - 1) * GAP,
             rows * (tile_h + LABEL_H) + (rows - 1) * GAP),
            (12, 12, 14),
        )
        draw = ImageDraw.Draw(sheet)
        for i, f in enumerate(page):
            cx = (i % pcols) * (tile_w + GAP)
            cy = (i // pcols) * (tile_h + LABEL_H + GAP)
            label = fmt_time(f["t"]) + (" cue" if f["reason"] == "cue" else "")
            draw.text((cx + 10, cy + 7), label, fill=(255, 255, 255), font=font)
            with Image.open(f["path"]) as img:
                sheet.paste(img.convert("RGB").resize((tile_w, tile_h)), (cx, cy + LABEL_H))
        path = out_dir / f"sheet_{page_i + 1:02d}.png"
        sheet.save(path)
        sheets.append((path, page))
    return sheets


def main() -> None:
    ap = argparse.ArgumentParser(description="Scene-aware visual survey → labeled contact sheets")
    ap.add_argument("video", type=Path)
    ap.add_argument("--mode", choices=["scene", "keyframe", "uniform"], default="scene")
    ap.add_argument("--max-frames", type=int, default=24,
                    help="cap on selected frames, cues included (default 24)")
    ap.add_argument("--times", nargs="*", default=[],
                    help="pinned cue timestamps (SS, MM:SS, HH:MM:SS) — e.g. deictic "
                         "moments from takes_packed.md; never evicted by the cap")
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--width", type=int, default=512, help="tile source width (default 512)")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--no-dedup", action="store_true",
                    help="keep near-identical frames (default: perceptual dedup)")
    ap.add_argument("-o", "--out-dir", type=Path, default=None,
                    help="default: <video_dir>/edit/verify/watch_<stem>/")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe not on PATH")
    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    start = parse_time(args.start)
    end = parse_time(args.end)
    duration = probe_duration(video)
    eff_duration = max(0.0, (end if end is not None else duration) - (start or 0.0))
    cues_wanted = [parse_time(t) for t in args.times]

    out_dir = args.out_dir or (video.parent / "edit" / "verify" / f"watch_{video.stem}")
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("sheet_*.png"):
        old.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        cues, cues_dropped = ([], 0)
        if cues_wanted:
            cues, cues_dropped = grab_cues(video, work, args.width, cues_wanted, start, end)

        budget = max(1, args.max_frames - len(cues))
        engine = args.mode
        if args.mode == "scene":
            cands = extract_scene(video, work, args.width, start, end)
            if len(cands) < SCENE_MIN_FRAMES:
                engine = "uniform (static source)"
                for c in cands:
                    c["path"].unlink(missing_ok=True)
                cands = extract_uniform(video, work, args.width, budget, start, end, eff_duration)
        elif args.mode == "keyframe":
            cands = extract_keyframes(video, work, args.width, start, end)
            if len(cands) < 4:
                engine = "uniform (too few keyframes)"
                for c in cands:
                    c["path"].unlink(missing_ok=True)
                cands = extract_uniform(video, work, args.width, budget, start, end, eff_duration)
        else:
            cands = extract_uniform(video, work, args.width, budget, start, end, eff_duration)

        n_cands = len(cands)
        deduped, n_dropped = (cands, 0) if args.no_dedup else dedupe(cands)
        selected = even_sample(deduped, budget)

        frames = sorted(selected + cues, key=lambda f: f["t"])
        if not frames:
            sys.exit("no frames extracted")
        sheets = build_sheets(frames, out_dir, args.cols)

    print(f"{video.name} — {fmt_time(duration)}"
          + (f", window {fmt_time(start or 0)}-{fmt_time(end if end is not None else duration)}"
             if (start is not None or end is not None) else ""))
    print(f"engine={engine} candidates={n_cands} deduped={n_dropped} "
          f"selected={len(selected)} cues={len(cues)}"
          + (f" ({cues_dropped} cue(s) outside window dropped)" if cues_dropped else ""))
    for path, page in sheets:
        labels = " · ".join(fmt_time(f["t"]) + ("*" if f["reason"] == "cue" else "")
                            for f in page)
        print(f"{path}")
        print(f"  tiles: {labels}")
    print("Read each sheet PNG above to see the video (* = pinned cue).")


if __name__ == "__main__":
    main()

"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def _color_tags(video: Path) -> dict[str, str]:
    """Read the source's color tags (empty strings when absent/unknown)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer,color_primaries,color_space,color_range",
             "-of", "default=noprint_wrappers=1", str(video)],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return {}
    tags = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            tags[k.strip()] = "" if v in ("unknown", "N/A") else v
    return tags


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    return _color_tags(video).get("color_transfer", "") in HDR_TRANSFERS


# Wide-gamut SDR (BT.2020 primaries with an ordinary transfer) is NOT caught by
# is_hdr_source — phone/mirrorless cameras routinely write bt2020 primaries with
# color_transfer=unknown. Left unconverted, cut.mp4 inherits the bt2020 tags and
# every downstream decoder re-interprets them: Chrome (which Remotion composites
# through) darkens the image by roughly a 1.2 gamma and shifts hue, so the Phase-2
# render no longer matches the Phase-1 grade the user approved. Convert to Rec.709
# at extraction so exactly one interpretation exists from the grade onwards.
WIDE_GAMUT_PRIMARIES = {"bt2020"}
WIDE_GAMUT_MATRICES = {"bt2020nc", "bt2020_ncl", "bt2020c", "bt2020_cl"}


def wide_gamut_chain(video: Path) -> str:
    """Filter chain converting a wide-gamut SDR source to Rec.709, or ''.

    Returns '' for HDR sources (TONEMAP_CHAIN already lands on Rec.709) and for
    sources that are already Rec.709 or untagged.
    """
    if is_hdr_source(video):
        return ""
    tags = _color_tags(video)
    primaries = tags.get("color_primaries", "")
    matrix = tags.get("color_space", "")
    if primaries not in WIDE_GAMUT_PRIMARIES and matrix not in WIDE_GAMUT_MATRICES:
        return ""
    in_range = "pc" if tags.get("color_range", "") == "pc" else "tv"
    # itrc: bt2020-10 is the SDR BT.2020 transfer; it matches Rec.709's curve, so
    # this converts the gamut without altering the tone curve the grade was cut on.
    return (
        f"colorspace=ispace={matrix or 'bt2020nc'}:iprimaries={primaries or 'bt2020'}"
        f":itrc=bt2020-10:irange={in_range}"
        ":space=bt709:primaries=bt709:trc=bt709:range=tv"
    )


def is_portrait_source(video: Path) -> bool:
    """Return True if the video DISPLAYS as portrait (height > width).

    Phone/mirrorless footage is often stored landscape (e.g. 3840×2160) with a
    ±90° display-matrix rotation, which ffmpeg auto-applies on decode. Reading
    only the raw stream dims would call such a clip landscape and scale it to
    the wrong size, so we swap dims when the rotation is ±90°/±270°.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        parts = out.stdout.strip().split(",")
        w, h = int(parts[0]), int(parts[1])
    except Exception:
        return False

    rot = 0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True,
        )
        vals = [v for v in r.stdout.split() if v.lstrip("-").isdigit()]
        if vals:
            rot = int(vals[0])
    except Exception:
        pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    return h > w


def source_fps(video: Path) -> float:
    """Return the source's video frame rate (frames per second).

    Reads r_frame_rate ("30000/1001") and evaluates the fraction. Returns 0.0
    if it can't be determined, so callers fall back to the safe default.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        num, _, den = out.stdout.strip().partition("/")
        return float(num) / float(den) if den else float(num)
    except Exception:
        return 0.0


def shortform_target_fps(video: Path) -> str:
    """Short-form render fps as a string for ffmpeg `-r`.

    Rule: sources shot at 30fps or higher render at 30 (keeps motion natural and
    matches Instagram/TikTok/Shorts capture); slower sources keep the 24 standard.
    """
    return "30" if source_fps(video) >= 29.5 else "24"


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    keep_resolution: bool = False,
    gain_db: float = 0.0,
    streams: str = "av",
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `streams` selects what lands in the file: "av" (default), "v" (video only) or
    "a" (audio only, PCM when the path ends in .wav). The split exists for the
    J-cut assembly, where a take's picture and its sound come from DIFFERENT
    ranges — trimming an already-extracted segment instead would leave the 30ms
    fade stranded mid-audio and turn the new end into a hard chop.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
      - keep_resolution: source resolution + source fps (LONGFORM / 16:9 YouTube).
        Skips scaling and does not force 24 fps. Draft/preview still down-scale.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    elif keep_resolution:
        scale = ""  # keep native resolution (longform)
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    else:
        wide = wide_gamut_chain(source)
        if wide:
            vf_parts.append(wide)
    if scale:
        vf_parts.append(scale)
    if grade_filter:
        # Force 8-bit BEFORE the grade. `colorlevels` — the backbone of the LOG
        # presets — is broken on 9–14 bit RGB: on a 10-bit source it collapses the
        # whole frame to a constant TV black (measured YAVG=64/1023, YBITDEPTH=1 on
        # an iPhone Apple Log ProRes). It is correct at 8-bit and at 16-bit, so a
        # 10-bit source silently renders black while an 8-bit one renders fine.
        # The output is `-pix_fmt yuv420p` regardless, so this costs nothing, and it
        # makes the render match the 8-bit frame the user approved in the
        # `grade.py --candidates` montage.
        vf_parts.append("format=yuv420p")
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # Per-segment level match (whisper/mumble rescue). Applied BEFORE the fades
    # so the edges still land at true silence. A boosted segment gets a limiter
    # so a loud syllable inside a quiet take cannot clip after the gain.
    af_parts: list[str] = []
    if abs(gain_db) > 0.05:
        af_parts.append(f"volume={gain_db:+.2f}dB")
        if gain_db > 0:
            af_parts.append("alimiter=level_in=1:level_out=1:limit=0.95")

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    af_parts.append("afade=t=in:st=0:d=0.03")
    af_parts.append(f"afade=t=out:st={fade_out_start:.3f}:d=0.03")
    af = ",".join(af_parts)

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.6f}",
        "-i", str(source),
        "-t", f"{duration:.6f}",
    ]
    if streams != "a":
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p"]
        # Every path above lands on Rec.709 (tonemap for HDR, wide_gamut_chain for
        # BT.2020 SDR, passthrough for the rest), so tag it explicitly. Without this
        # the segments can inherit the source's tags and downstream decoders
        # (Chrome/Remotion in Phase 2) silently re-interpret the graded image.
        cmd += ["-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-color_range", "tv"]
        if not keep_resolution:
            # short-form fps: 30 if the source is 30fps+ (natural motion, matches
            # IG/TikTok/Shorts capture), else the 24 standard; longform keeps source.
            cmd += ["-r", shortform_target_fps(source)]
    else:
        cmd += ["-vn"]

    if streams != "v":
        cmd += ["-af", af]
        if out_path.suffix.lower() == ".wav":
            # PCM for the J-cut mix — the segments get summed, so no point
            # compounding AAC generations before the single final encode.
            cmd += ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-an"]

    if out_path.suffix.lower() in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd += [str(out_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def snap_ranges_to_frames(edl: dict, fps: int) -> int:
    """Round every range's DURATION up to a whole number of frames.

    ffmpeg encodes a segment's video as whole frames but keeps its audio at the
    exact requested length, so a range whose duration is not a frame multiple
    produces a clip whose audio is a few ms shorter than its picture. Across a
    28-cut edit that summed to +0.44s of video over audio here, and cut.mp4 came
    out with an audio track LONGER than its video. Remotion then stretches that
    audio across the composition and the voice slides progressively out of sync —
    barely visible at the start, half a second adrift by the end.

    Snapping UP only ever extends a range into its trailing pad (≤1 frame), so it
    cannot clip a word. Returns how many ranges were changed.
    """
    changed = 0
    for r in edl.get("ranges", []):
        dur = r["end"] - r["start"]
        # Round the frame count to 4 decimals BEFORE ceiling. `end` is persisted to
        # 6 decimals, so a duration that is already a whole number of frames comes
        # back as e.g. 1.866667 → 1.866667*30 = 56.00001, which a bare ceil() turns
        # into 57. That made the snap non-idempotent: every re-render of the same
        # EDL grew the affected ranges by one more frame, and since this function
        # rewrites edl.json the drift persisted.
        frames = math.ceil(round(dur * fps, 4))
        snapped = r["start"] + frames / fps
        if abs(snapped - r["end"]) > 1e-6:
            r["end"] = round(snapped, 6)
            changed += 1
    return changed


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    keep_resolution: bool = False,
    jobs: int = 0,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    Segments are independent, so they encode in PARALLEL (`jobs` ffmpeg
    processes; 0 = auto ≈ cores/3, capped at 4 — each libx264 already uses
    several threads). Order is preserved by the seg_NN filenames.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    # mirror of the J-cut path — see the note there on why ALL of them go
    for stale in list(clips_dir.glob("seg_*.mp4")) + list(clips_dir.glob("seg_*.wav")):
        stale.unlink(missing_ok=True)

    if jobs <= 0:
        jobs = max(1, min(4, (os.cpu_count() or 4) // 3))
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  ({jobs} parallel)")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")

    def work(i: int, r: dict) -> Path:
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        gain_db = float(r.get("gain_db", 0.0) or 0.0)

        note = r.get("beat") or r.get("note") or ""
        grade_note = f"  grade: {seg_filter or '(none)'}" if is_auto else ""
        gain_note = f"  gain: {gain_db:+.1f}dB" if abs(gain_db) > 0.05 else ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}{grade_note}{gain_note}", flush=True)
        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft, keep_resolution=keep_resolution,
            gain_db=gain_db,
        )
        return out_path

    if jobs == 1 or len(ranges) == 1:
        return [work(i, r) for i, r in enumerate(ranges)]
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        return list(ex.map(work, range(len(ranges)), ranges))


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- J-cut assembly (Phase-1 default cleanup) ---------------------------
#
# A straight concat leaves a beat of silence at every junction: the outgoing take
# keeps its trailing pad and the incoming one starts with its own. Measured on a
# real 3-take edit that was 130ms and 140ms — small on paper, clearly a pause in
# the room.
#
# The J-cut removes it by OVERLAPPING instead of butting: the outgoing take's audio
# runs to its natural end while the incoming take's audio starts `lead` frames
# earlier, on its own track, and the two are summed. The incoming take's PICTURE
# starts where the outgoing audio ends, skipping `lead` frames of its own head —
# so the voice arrives before the face. Sync inside each take is preserved by
# construction:  video_in = audio_in + lead  and  video_offset = audio_offset + lead.
#
# Getting the seam tighter is done by trimming the OUTGOING take's tail, not by
# raising the lead: a bigger lead also pushes the picture deeper into the incoming
# take's speech, which reads as entering mid-word.
#
# The tail trim is MEASURED, never blind. Cutting a fixed 2 frames off every take
# would eventually decapitate a word on footage whose take ends tight — so the trim
# is capped by the silence actually present at the end of that range, keeping 10ms.

JCUT_LEAD_FRAMES = 5
JCUT_TAIL_TRIM_FRAMES = 2


def jcut_settings(edl: dict, fps: int) -> dict | None:
    """Resolve J-cut config. On by default; `"jcut": false` in the EDL disables it.

    Returns None when there is nothing to overlap (single range) or when disabled.
    """
    cfg = edl.get("jcut", True)
    if cfg is False or cfg == "off" or cfg == "none":
        return None
    if len(edl.get("ranges", [])) < 2:
        return None
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "lead_frames": max(0, int(cfg.get("lead_frames", JCUT_LEAD_FRAMES))),
        "tail_trim_frames": max(0, int(cfg.get("tail_trim_frames", JCUT_TAIL_TRIM_FRAMES))),
        "fps": fps,
    }


def trailing_silence(source: Path, start: float, end: float,
                     noise_db: int = -35) -> float:
    """Seconds of silence at the END of a source range. 0.0 if it ends in speech.

    This is what bounds the tail trim: we only ever remove what is already silent.
    """
    dur = end - start
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.6f}",
         "-t", f"{dur:.6f}", "-i", str(source), "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d=0.02", "-f", "null", "-"],
        capture_output=True, text=True)
    starts: list[float] = []
    ends: list[float] = []
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            try:
                starts.append(float(line.rsplit("silence_start:", 1)[1].split()[0]))
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line:
            try:
                ends.append(float(line.rsplit("silence_end:", 1)[1].split()[0]))
            except (ValueError, IndexError):
                pass
    if not starts:
        return 0.0
    last = starts[-1]
    # the range ends in silence only if no speech resumed after `last`
    if any(e > last + 1e-6 and e < dur - 0.02 for e in ends):
        return 0.0
    return max(0.0, dur - last)


def plan_jcut(edl: dict, edit_dir: Path, cfg: dict) -> list[dict]:
    """Work out each take's video range, audio range and output offsets."""
    fps = cfg["fps"]
    lead = cfg["lead_frames"] / fps
    ranges = edl["ranges"]
    sources = edl["sources"]
    n = len(ranges)

    plan: list[dict] = []
    a_off = v_off = 0.0
    for i, r in enumerate(ranges):
        src = resolve_path(sources[r["source"]], edit_dir)
        start, end = float(r["start"]), float(r["end"])

        tail = 0.0
        avail = trailing_silence(src, start, end) if i < n - 1 else None
        if avail is not None and cfg["tail_trim_frames"]:
            budget = max(0.0, avail - 0.010)          # keep 10ms of room tone
            usable_frames = min(cfg["tail_trim_frames"], int(budget * fps))
            tail = usable_frames / fps

        a_in, a_out = start, end - tail
        v_in = start + (lead if i > 0 else 0.0)
        v_out = a_out
        plan.append({
            "i": i, "src": src, "range": r,
            "a_in": a_in, "a_out": a_out, "a_off": a_off,
            "v_in": v_in, "v_out": v_out, "v_off": v_off,
            "tail_frames": round(tail * fps),
            "silence_avail_ms": round(avail * 1000) if avail is not None else None,
        })
        v_off += v_out - v_in
        a_off += (a_out - a_in) - lead
    return plan


def assemble_jcut(plan: list[dict], out_path: Path, edit_dir: Path) -> None:
    """Concat the video track, sum the offset audio tracks, mux them together."""
    work = edit_dir / "clips_graded"
    vlist = edit_dir / "_concat_jcut.txt"
    vlist.write_text("".join(f"file '{p['video_path'].resolve()}'\n" for p in plan))

    video_only = work / "_jcut_video.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(vlist),
         "-c", "copy", str(video_only)], quiet=True)
    vlist.unlink(missing_ok=True)

    inputs: list[str] = []
    labels = ""
    refs = ""
    for k, p in enumerate(plan):
        inputs += ["-i", str(p["audio_path"])]
        # delay in SAMPLES: adelay's integer-millisecond form leaves the mix a
        # fraction short of the video, and a downstream -shortest then amputates
        # whole frames of picture.
        smp = int(round(p["a_off"] * 48000))
        labels += f"[{k}:a]adelay={smp}S|{smp}S[d{k}];"
        refs += f"[d{k}]"

    audio_only = work / "_jcut_audio.wav"
    run(["ffmpeg", "-y", *inputs, "-filter_complex",
         f"{labels}{refs}amix=inputs={len(plan)}:normalize=0:duration=longest,"
         f"alimiter=limit=0.95[a]", "-map", "[a]", "-c:a", "pcm_s16le",
         str(audio_only)], quiet=True)

    total = sum(p["v_out"] - p["v_in"] for p in plan)
    # NO -shortest here: a sub-millisecond audio shortfall would truncate video.
    run(["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio_only),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-t", f"{total:.6f}", "-movflags", "+faststart", str(out_path)], quiet=True)
    video_only.unlink(missing_ok=True)
    audio_only.unlink(missing_ok=True)


def extract_and_assemble_jcut(
    edl: dict, edit_dir: Path, cfg: dict, preview: bool, draft: bool,
    keep_resolution: bool, jobs: int, base_path: Path,
) -> list[dict]:
    """Extract every take's picture and sound separately, then overlap-assemble."""
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded"))
    clips_dir.mkdir(parents=True, exist_ok=True)

    # Clear EVERY old segment, not just the other mode's. Two ways this folder goes
    # stale and both are invisible downstream, because segments.json is built by
    # globbing it: the butt-join mode writes seg_NN_*.mp4 next to the J-cut's
    # seg_NN_*_v.mp4 (a bare glob sums both), and a re-render with FEWER ranges
    # leaves the higher-numbered segments of the previous cut behind. Measured: a
    # 3-range EDL over a stale 4th segment gave segments.json 9.23s for a 7.57s
    # video — it renders without error and every overlay lands wrong.
    for stale in list(clips_dir.glob("seg_*.mp4")) + list(clips_dir.glob("seg_*.wav")):
        stale.unlink(missing_ok=True)

    plan = plan_jcut(edl, edit_dir, cfg)
    if jobs <= 0:
        jobs = max(1, min(4, (os.cpu_count() or 4) // 3))

    lead_ms = round(cfg["lead_frames"] / cfg["fps"] * 1000)
    print(f"J-cut: áudio entra {cfg['lead_frames']}f ({lead_ms}ms) antes da imagem"
          f"  ({len(plan)} takes, {jobs} paralelos)")

    def work(p: dict) -> dict:
        i, r = p["i"], p["range"]
        seg_filter = (auto_grade_for_clip(p["src"], start=p["v_in"],
                                          duration=p["v_out"] - p["v_in"],
                                          verbose=False)[0]
                      if is_auto else resolved)
        gain_db = float(r.get("gain_db", 0.0) or 0.0)
        vpath = clips_dir / f"seg_{i:02d}_{r['source']}_v.mp4"
        apath = clips_dir / f"seg_{i:02d}_{r['source']}_a.wav"
        extract_segment(p["src"], p["v_in"], p["v_out"] - p["v_in"], seg_filter,
                        vpath, preview=preview, draft=draft,
                        keep_resolution=keep_resolution, streams="v")
        extract_segment(p["src"], p["a_in"], p["a_out"] - p["a_in"], "",
                        apath, preview=preview, draft=draft,
                        keep_resolution=keep_resolution, gain_db=gain_db,
                        streams="a")
        p["video_path"], p["audio_path"] = vpath, apath

        tail_note = ""
        if p["tail_frames"]:
            tail_note = (f"  cauda -{p['tail_frames']}f"
                         f" (de {p['silence_avail_ms']}ms de silêncio)")
        elif p["silence_avail_ms"] is not None:
            tail_note = f"  cauda 0f (só {p['silence_avail_ms']}ms de silêncio)"
        gain_note = f"  gain: {gain_db:+.1f}dB" if abs(gain_db) > 0.05 else ""
        print(f"  [{i:02d}] {r['source']}  v {p['v_in']:7.2f}-{p['v_out']:7.2f}"
              f"  a {p['a_in']:7.2f}-{p['a_out']:7.2f}"
              f"  {r.get('beat') or ''}{tail_note}{gain_note}", flush=True)
        return p

    if jobs == 1 or len(plan) == 1:
        plan = [work(p) for p in plan]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            plan = list(ex.map(work, plan))

    assemble_jcut(plan, base_path, edit_dir)
    return plan


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


# -------- Voice EQ + mastering (spoken-word broadcast chain) ----------------

# A conservative broadcast chain for a single spoken voice. Applied BEFORE
# loudnorm so the normalizer measures the already-mastered signal.
#   1. highpass 80 Hz .......... kill rumble / HVAC / handling / plosive thump
#   2. -2.5 dB @ 200 Hz (Q1.1) .. reduce boxiness / mud
#   3. acompressor ............. even out dynamics, bring the voice forward
#   4. +2.5 dB @ 3.2 kHz (Q1.6) . presence / intelligibility
#   5. high-shelf +3 dB @ 9 kHz . air / brightness
#   6. deesser ................. tame sibilance the presence boost exaggerates
#   7. alimiter ................ safety ceiling before loudnorm
# Every value is a starting point — tune per voice/room if the material asks.
VOICE_MASTER_CHAIN = (
    "highpass=f=80,"
    "equalizer=f=200:t=q:w=1.1:g=-2.5,"
    "acompressor=threshold=-20dB:ratio=3:attack=12:release=200:makeup=3:knee=6,"
    "equalizer=f=3200:t=q:w=1.6:g=2.5,"
    "treble=g=3:f=9000,"
    "deesser=i=0.35,"
    "alimiter=level_in=1:level_out=1:limit=0.95"
)


def apply_voice_master(input_path: Path, output_path: Path) -> None:
    """Run the spoken-word EQ + mastering chain, video copied untouched.

    Runs before loudnorm; loudnorm then normalizes the mastered signal to the
    social target. Audio re-encoded to AAC 192k/48k, video stream copied.
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", VOICE_MASTER_CHAIN,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  voice master: EQ + compression + de-ess → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            # never let audio outlive the video (see snap_ranges_to_frames)
            "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        # never let audio outlive the video (see snap_ranges_to_frames)
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        # keep the Rec.709 tags the segments carry — this pass re-encodes video
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--voice-master",
        action="store_true",
        help="Apply spoken-word EQ + mastering (highpass, mud cut, compression, "
             "presence + air, de-ess, limiter) before loudnorm. Also enabled by "
             'EDL field "voice_master": true.',
    )
    ap.add_argument(
        "--keep-resolution",
        action="store_true",
        help="LONGFORM (16:9 YouTube): keep the source resolution and fps instead of "
             "forcing 1080p @ 24. Grade/voice-master/loudnorm still apply.",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Parallel segment extractions (0 = auto ≈ cores/3, capped at 4).",
    )
    ap.add_argument(
        "--no-jcut",
        action="store_true",
        help="Butt-join the takes instead of overlapping them. The J-cut is the "
             "default Phase-1 cleanup: the next take's audio starts a few frames "
             "before its picture, which removes the pause at every junction. Also "
             'disabled by EDL field "jcut": false.',
    )
    ap.add_argument(
        "--jcut-lead",
        type=int,
        default=None,
        help=f"Frames the audio leads the picture (default {JCUT_LEAD_FRAMES}).",
    )
    ap.add_argument(
        "--jcut-tail-trim",
        type=int,
        default=None,
        help=f"Max frames trimmed off each outgoing take's tail (default "
             f"{JCUT_TAIL_TRIM_FRAMES}). Capped by the silence actually there.",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # Frame-align every range BEFORE extraction, and persist it: the EDL, the
    # preview timeline and segments.json must all describe the same cut as the
    # rendered file, or anything that has to land on a cut lands beside it.
    first_src = next(iter(edl.get("sources", {}).values()), None)
    target_fps = int(shortform_target_fps(Path(first_src))) if (first_src and not args.keep_resolution) else 30
    snapped = snap_ranges_to_frames(edl, target_fps)
    if snapped:
        edl["total_duration_s"] = round(sum(r["end"] - r["start"] for r in edl["ranges"]), 3)
        edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2))
        print(f"  frame-aligned {snapped} range(s) to {target_fps}fps → edl.json updated")

    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name

    jcut = None if args.no_jcut else jcut_settings(edl, target_fps)
    if jcut:
        if args.jcut_lead is not None:
            jcut["lead_frames"] = max(0, args.jcut_lead)
        if args.jcut_tail_trim is not None:
            jcut["tail_trim_frames"] = max(0, args.jcut_tail_trim)

    if jcut:
        # 1+2. Picture and sound extracted from different ranges, then overlapped.
        plan = extract_and_assemble_jcut(
            edl, edit_dir, jcut, preview=args.preview, draft=args.draft,
            keep_resolution=args.keep_resolution, jobs=args.jobs,
            base_path=base_path,
        )
        # Persist the real output timeline: everything downstream (preview
        # timeline, segments.json, Phase-2 overlays) indexes off these, and the
        # J-cut timeline is SHORTER than the sum of the ranges.
        edl["jcut_timeline"] = [
            {"beat": p["range"].get("beat"), "source": p["range"]["source"],
             "video_start_in_output": round(p["v_off"], 6),
             "video_duration": round(p["v_out"] - p["v_in"], 6),
             "audio_start_in_output": round(p["a_off"], 6),
             "audio_duration": round(p["a_out"] - p["a_in"], 6),
             "tail_trim_frames": p["tail_frames"]}
            for p in plan
        ]
        edl["total_duration_s"] = round(
            sum(p["v_out"] - p["v_in"] for p in plan), 3)
        edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2))
        print(f"  timeline J-cut: {edl['total_duration_s']}s → edl.json atualizado")
    else:
        # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
        segment_paths = extract_all_segments(
            edl, edit_dir, preview=args.preview, draft=args.draft,
            keep_resolution=args.keep_resolution, jobs=args.jobs,
        )
        # 2. Concat → base
        concat_segments(segment_paths, base_path, edit_dir)
        # Drop any J-cut timeline from a PREVIOUS render — and persist that, or the
        # stale block outlives the render it described and everything downstream
        # (verify_cut, the preview lanes, Phase-2 offsets) keeps trusting it.
        if edl.pop("jcut_timeline", None) is not None:
            edl["total_duration_s"] = round(
                sum(r["end"] - r["start"] for r in edl["ranges"]), 3)
            edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2))
            print("  J-cut desligado → jcut_timeline removido do edl.json")

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    voice_master = args.voice_master or bool(edl.get("voice_master"))

    if args.no_loudnorm and not voice_master:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # Composite to a temp file, then optional voice master → optional loudnorm.
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        temps = [tmp_composite]

        norm_input = tmp_composite
        if voice_master:
            print("voice mastering → EQ + compression + de-ess (spoken word)")
            voiced = out_path.with_suffix(".voiced.mp4")
            apply_voice_master(tmp_composite, voiced)
            norm_input = voiced
            temps.append(voiced)

        if args.no_loudnorm:
            # Voice master requested but loudnorm skipped: the mastered file is final.
            norm_input.replace(out_path)
            temps = [t for t in temps if t != norm_input]
        else:
            print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
            apply_loudnorm_two_pass(norm_input, out_path, preview=args.draft)

        for t in temps:
            t.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()

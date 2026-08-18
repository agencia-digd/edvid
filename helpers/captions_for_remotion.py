"""Emit a @remotion/captions Caption[] JSON for the Remotion (Phase 2) project.

Two modes:
  --transcript <cut.json>   PREFERRED. Transcribe the FINAL cut.mp4 first
      (`transcribe.py cut.mp4`), then feed that transcript here. Its word times
      are already on the output timeline and free of the source's stretch/dead
      -air artifacts, so the first word of every segment (e.g. the hook) is
      captioned with correct timing. This is the robust default.
  <edl.json>                Fallback: map per-source word times through the EDL
      offsets. Subject to Whisper stretch at segment edges.

Each spoken word becomes one Caption (word-level) so the word-highlight /
karaoke component can drive per-word timing.

Caption shape (from @remotion/captions): { text, startMs, endMs, timestampMs, confidence }

Usage:
    python helpers/captions_for_remotion.py --transcript <edit>/transcripts/cut.json -o captions.json
    python helpers/captions_for_remotion.py <edl.json> -o captions.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def captions_from_transcript(transcript_path: Path) -> list[dict]:
    """Words already on the output timeline (transcript of the final cut)."""
    words = [w for w in json.loads(transcript_path.read_text()).get("words", [])
             if w.get("type") == "word" and w.get("start") is not None]
    caps: list[dict] = []
    for w in words:
        t = float(w["start"])
        e = float(w.get("end") or w["start"])
        if e <= t:
            e = t + 0.12
        text = (w.get("text") or "").strip()
        if not text:
            continue
        caps.append({
            "text": text,
            "startMs": round(t * 1000),
            "endMs": round(e * 1000),
            "timestampMs": round((t + e) / 2 * 1000),
            "confidence": None,
        })
    caps.sort(key=lambda c: c["startMs"])
    return caps


def build_captions(edl: dict, edit_dir: Path) -> list[dict]:
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]
    caps: list[dict] = []
    off = 0.0  # output-timeline offset (seconds)

    for r in edl["ranges"]:
        src = r["source"]
        a, b = float(r["start"]), float(r["end"])
        dur = b - a
        tr_path = transcripts_dir / f"{src}.json"
        if not tr_path.exists():
            off += dur
            continue
        words = [w for w in json.loads(tr_path.read_text()).get("words", [])
                 if w.get("type") == "word" and w.get("start") is not None]
        seg = [w for w in words if (a - 0.08) <= w["start"] < b]
        seg.sort(key=lambda w: w["start"])
        for w in seg:
            t = max(0.0, w["start"] - a) + off
            e = min(dur, max(0.0, (w.get("end") or w["start"]) - a)) + off
            if e <= t:
                e = t + 0.12
            text = (w.get("text") or "").strip()
            if not text:
                continue
            caps.append({
                "text": text,
                "startMs": round(t * 1000),
                "endMs": round(e * 1000),
                "timestampMs": round((t + e) / 2 * 1000),
                "confidence": None,
            })
        off += dur

    caps.sort(key=lambda c: c["startMs"])
    return caps


def main() -> None:
    ap = argparse.ArgumentParser(description="→ @remotion/captions Caption[] JSON")
    ap.add_argument("edl", type=Path, nargs="?", help="edl.json (fallback mode)")
    ap.add_argument("--transcript", type=Path, default=None,
                    help="Transcript of the final cut.mp4 (preferred mode)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output captions.json path")
    args = ap.parse_args()

    if args.transcript:
        caps = captions_from_transcript(args.transcript.resolve())
    elif args.edl:
        edl_path = args.edl.resolve()
        caps = build_captions(json.loads(edl_path.read_text()), edl_path.parent)
    else:
        ap.error("provide --transcript <cut.json> or an edl.json")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.output} — {len(caps)} word captions")


if __name__ == "__main__":
    main()

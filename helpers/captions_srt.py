"""Emit a broadcast-style `.srt` from a transcript of the FINAL cut — for
YouTube CC (longform). NOT burned into the video: the user uploads it as a
subtitle track and YouTube shows it as optional closed captions.

Transcribe the final cut first (`transcribe.py cut.mp4`), then feed that
transcript here so the word times are already on the output timeline.

Grouping: word-level times → readable cues.
  - ≤ ~42 chars/line, ≤ 2 lines (balanced wrap)
  - break on sentence punctuation, on a silence gap, at the line/char cap, or
    at the max cue duration
  - min ~1.0s / max ~6.0s per cue; sentence case preserved (Whisper casing)

Usage:
    python helpers/captions_srt.py --transcript <edit>/transcripts/cut.json -o <edit>/captions.srt
    python helpers/captions_srt.py --captions <edit>/remotion/public/captions.json -o <edit>/captions.srt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_words(transcript: Path | None, captions: Path | None) -> list[dict]:
    """Return [{text, start, end}] in seconds, from a transcript or a captions.json."""
    if transcript:
        raw = json.loads(transcript.read_text()).get("words", [])
        out = []
        for w in raw:
            if w.get("type") != "word" or w.get("start") is None:
                continue
            t = float(w["start"]); e = float(w.get("end") or w["start"])
            text = (w.get("text") or "").strip()
            if text:
                out.append({"text": text, "start": t, "end": max(e, t + 0.08)})
        return out
    # captions.json (word-level Caption[])
    data = json.loads(captions.read_text())
    return [{"text": (c.get("text") or "").strip(),
             "start": c["startMs"] / 1000, "end": c["endMs"] / 1000}
            for c in data if (c.get("text") or "").strip()]


ENDERS = ".!?…"


def group_cues(words: list[dict], max_chars: int, max_lines: int,
               max_dur: float, min_dur: float, gap: float) -> list[dict]:
    cues: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = " ".join(w["text"] for w in cur)
        start = cur[0]["start"]
        end = max(cur[-1]["end"], start + min_dur)
        cues.append({"start": start, "end": end, "text": text})
        cur.clear()

    cap = max_chars * max_lines
    for i, w in enumerate(words):
        cur.append(w)
        cur_text = " ".join(x["text"] for x in cur)
        dur = w["end"] - cur[0]["start"]
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap_next = (nxt["start"] - w["end"]) if nxt else 999.0
        ends_sentence = w["text"][-1:] in ENDERS
        too_long = len(cur_text) >= cap
        too_far = nxt and (len(cur_text) + 1 + len(nxt["text"])) > cap
        over_time = dur >= max_dur
        if ends_sentence or too_long or too_far or over_time or gap_next >= gap:
            flush()
    flush()

    # keep cues from overlapping (clamp each end to the next start)
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"]:
            cues[i]["end"] = max(cues[i]["start"] + 0.4, cues[i + 1]["start"] - 0.02)
    return cues


def wrap(text: str, max_chars: int, max_lines: int) -> str:
    words = text.split()
    if len(text) <= max_chars or max_lines == 1:
        return text
    # balance into 2 lines near the middle character
    target = len(text) / 2
    best_i, best_d = 1, 1e9
    run = 0
    for i in range(1, len(words)):
        run += len(words[i - 1]) + 1
        d = abs(run - target)
        if d < best_d:
            best_d, best_i = d, i
    return " ".join(words[:best_i]) + "\n" + " ".join(words[best_i:])


def ts(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcript of the final cut → .srt for YouTube CC")
    ap.add_argument("--transcript", type=Path, default=None, help="transcripts/cut.json (preferred)")
    ap.add_argument("--captions", type=Path, default=None, help="word-level captions.json (alt input)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output .srt")
    ap.add_argument("--max-chars", type=int, default=42)
    ap.add_argument("--max-lines", type=int, default=2)
    ap.add_argument("--max-dur", type=float, default=6.0)
    ap.add_argument("--min-dur", type=float, default=1.0)
    ap.add_argument("--gap", type=float, default=0.7, help="silence gap (s) that forces a cue break")
    args = ap.parse_args()

    if not args.transcript and not args.captions:
        ap.error("provide --transcript <cut.json> or --captions <captions.json>")

    words = load_words(args.transcript, args.captions)
    if not words:
        raise SystemExit("no words found in input")
    cues = group_cues(words, args.max_chars, args.max_lines, args.max_dur, args.min_dur, args.gap)

    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{ts(c['start'])} --> {ts(c['end'])}")
        lines.append(wrap(c["text"], args.max_chars, args.max_lines))
        lines.append("")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"{args.output} — {len(cues)} cues from {len(words)} words")


if __name__ == "__main__":
    main()

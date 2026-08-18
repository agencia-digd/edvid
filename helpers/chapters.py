"""Emit a YouTube chapters block from the EDL — for the video description (longform).

Mark chapter starts in the EDL by adding a `"chapter": "Title"` field to the range
that opens each section:

    {"source": "A", "start": 12.0, "end": 40.0, "beat": "INTRO", "chapter": "O que é X"}

The chapter's timestamp is its position on the OUTPUT timeline (sum of prior range
durations), so it lines up with the rendered cut.

YouTube's rules (enforced here): the first stamp must be `00:00`, there must be
≥ 3 chapters, and each must be ≥ 10s. Warnings print if the EDL violates them.

Usage:
    python helpers/chapters.py <edit>/edl.json -o <edit>/chapters.txt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt(sec: float, long: bool) -> str:
    s = int(round(sec))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if long or h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def main() -> None:
    ap = argparse.ArgumentParser(description="EDL → YouTube chapters block")
    ap.add_argument("edl", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    edl = json.loads(args.edl.read_text())
    ranges = edl["ranges"]

    chapters: list[tuple[float, str]] = []
    t = 0.0
    for r in ranges:
        dur = float(r["end"]) - float(r["start"])
        title = (r.get("chapter") or "").strip()
        if title:
            chapters.append((t, title))
        t += dur
    total = t

    if not chapters:
        raise SystemExit(
            'no chapters in the EDL — add a "chapter": "Title" field to the range '
            "that opens each section (see the header of this file)."
        )

    # YouTube requires the first stamp at 00:00.
    if chapters[0][0] > 0.5:
        chapters.insert(0, (0.0, chapters[0][1]))
    chapters[0] = (0.0, chapters[0][1])

    long = total >= 3600
    lines = [f"{fmt(s, long)} {title}" for s, title in chapters]
    out = "\n".join(lines) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out, encoding="utf-8")

    # validate
    warns = []
    if len(chapters) < 3:
        warns.append(f"only {len(chapters)} chapters — YouTube needs ≥ 3 to show them")
    for i, (s, title) in enumerate(chapters):
        nxt = chapters[i + 1][0] if i + 1 < len(chapters) else total
        if nxt - s < 10:
            warns.append(f'chapter "{title}" is {nxt - s:.0f}s — YouTube needs ≥ 10s')

    print(out.rstrip())
    print(f"\n{args.output} — {len(chapters)} chapters")
    for w in warns:
        print(f"  ⚠ {w}")


if __name__ == "__main__":
    main()

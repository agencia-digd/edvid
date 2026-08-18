"""Pick a local Mixkit bed when Treblo is off.

Usage:
    uv run python helpers/pick_bed.py --list
    uv run python helpers/pick_bed.py --mood tech -o remotion/public/trilha.mp3
    uv run python helpers/pick_bed.py --mood dark -o /tmp/trilha.mp3 --print-json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
CATALOG = SKILL_ROOT / "assets" / "music" / "catalog.json"


def load_catalog() -> dict:
    if not CATALOG.exists():
        sys.exit(f"catalog missing: {CATALOG}")
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def list_beds(cat: dict) -> None:
    print("beds (Mixkit Free License — comercial OK, atribuição não obrigatória)")
    for b in cat["beds"]:
        print(f"  {b['id']:6}  {b['title']} — {b['artist']}")
        print(f"          {b['use_when']}")


def pick(cat: dict, mood: str) -> dict:
    mood = (mood or "").strip().lower()
    for b in cat["beds"]:
        if b["id"] == mood or b["mood"] == mood:
            return b
    known = ", ".join(b["id"] for b in cat["beds"])
    sys.exit(f"mood desconhecido {mood!r}. use: {known}")


def copy_bed(bed: dict, dest: Path) -> Path:
    src = SKILL_ROOT / "assets" / "music" / bed["file"]
    if not src.exists():
        sys.exit(f"arquivo da cama sumiu: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="pick a local Mixkit soundtrack bed")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mood", help="tense | punch | warm | cta | dark | tech")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--print-json", action="store_true")
    args = ap.parse_args()

    cat = load_catalog()
    if args.list or not args.mood:
        list_beds(cat)
        if not args.mood:
            return

    bed = pick(cat, args.mood)
    dest = None
    if args.output:
        dest = copy_bed(bed, args.output.resolve())
        print(f"pick_bed {bed['id']} → {dest}  ({bed['title']} / {bed['artist']})")
    else:
        print(f"pick_bed {bed['id']}  {bed['title']} / {bed['artist']}")
        print(f"  file {SKILL_ROOT / 'assets' / 'music' / bed['file']}")
    if args.print_json:
        print(json.dumps(bed, ensure_ascii=False))


if __name__ == "__main__":
    main()

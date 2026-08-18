"""QA + glossary rewrite for Phase-2 captions.

Whisper invents brand names (DGD) and near-homophones (área/arte). Run AFTER
`captions_for_remotion.py` and BEFORE `caption_style.py`. The helper rewrites
`captions.json` and the cut transcript from a glossary, drops leftover phrases
when asked, and reports leftover risks (never-highlight words painted orange).

Usage:
    uv run python helpers/caption_qa.py \\
        --captions remotion/public/captions.json \\
        --transcript transcripts/cut.json \\
        --glossary assets/brand/digd/glossary.json \\
        --write
    uv run python helpers/caption_qa.py ... --drop-sequence "Que a DGD não é"
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOSSARY = SKILL_ROOT / "assets" / "brand" / "digd" / "glossary.json"


def strip_p(t: str) -> str:
    return t.strip(" .,!?;:…\"'-")


def norm(t: str) -> str:
    t = strip_p(t).lower()
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def split_punct(text: str) -> tuple[str, str, str]:
    raw = text or ""
    i = 0
    j = len(raw)
    while i < j and raw[i] in " \t\"'“”‘’":
        i += 1
    while j > i and raw[j - 1] in " \t.,!?;:…\"'“”‘’":
        j -= 1
    return raw[:i], raw[i:j], raw[j:]


def load_glossary(path: Path | None) -> dict:
    if path is None:
        path = DEFAULT_GLOSSARY if DEFAULT_GLOSSARY.exists() else None
    if path is None:
        return {"replacements": [], "emph": [], "never_highlight": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("replacements", [])
    data.setdefault("emph", [])
    data.setdefault("never_highlight", [])
    return data


def _word_items(words: list[dict]) -> list[tuple[int, dict]]:
    return [(i, w) for i, w in enumerate(words) if w.get("type", "word") == "word"]


def apply_replacements(words: list[dict], replacements: list[dict]) -> list[str]:
    """Mutate word tokens in place. Returns human-readable change lines."""
    changes: list[str] = []
    indexed = _word_items(words)
    for pos, (idx, w) in enumerate(indexed):
        lead, core, tail = split_punct(w.get("text") or "")
        if not core:
            continue
        prev_cores = [
            split_punct(indexed[j][1].get("text") or "")[1]
            for j in range(max(0, pos - 3), pos)
        ]
        prev_norm = {norm(p) for p in prev_cores}
        for rep in replacements:
            src = rep.get("from") or ""
            if core != src and norm(core) != norm(src):
                continue
            need = [norm(x) for x in (rep.get("prev_any") or [])]
            if need and not (prev_norm & set(need)):
                continue
            dest = rep.get("to") or ""
            new = f"{lead}{dest}{tail}"
            old = w["text"]
            if new == old:
                continue
            w["text"] = new
            t = w.get("start")
            stamp = f"@{t:.2f}s" if isinstance(t, (int, float)) else ""
            changes.append(f"  {old!r} → {new!r} {stamp}".rstrip())
            break
    return changes


def drop_sequence(words: list[dict], sequence: list[str]) -> int:
    """Remove a consecutive run of word tokens matching `sequence`. Returns count dropped."""
    if not sequence:
        return 0
    want = [norm(s) for s in sequence]
    indexed = _word_items(words)
    drop_idx: set[int] = set()
    cores = [norm(split_punct(w.get("text") or "")[1]) for _, w in indexed]
    n = len(want)
    for i in range(len(cores) - n + 1):
        if cores[i:i + n] == want:
            for j in range(i, i + n):
                drop_idx.add(indexed[j][0])
    if not drop_idx:
        return 0
    # also drop spacing tokens immediately after a dropped word, and a leading
    # spacing that would now sit before the next surviving word at the join
    extra: set[int] = set()
    for i in sorted(drop_idx):
        if i + 1 < len(words) and words[i + 1].get("type") == "spacing":
            extra.add(i + 1)
    drop_idx |= extra
    kept = [w for i, w in enumerate(words) if i not in drop_idx]
    words[:] = kept
    return n


def captions_from_words(words: list[dict]) -> list[dict]:
    caps: list[dict] = []
    for w in words:
        if w.get("type", "word") != "word":
            continue
        if w.get("start") is None:
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        t = float(w["start"])
        e = float(w.get("end") or t)
        if e <= t:
            e = t + 0.12
        caps.append({
            "text": text,
            "startMs": round(t * 1000),
            "endMs": round(e * 1000),
            "timestampMs": round((t + e) / 2 * 1000),
            "confidence": None,
        })
    caps.sort(key=lambda c: c["startMs"])
    return caps


def rebuild_transcript_text(words: list[dict]) -> str:
    parts: list[str] = []
    for w in words:
        parts.append(w.get("text") or "")
    return "".join(parts).strip()


def scan_cues(cues: list[dict], never: set[str]) -> list[str]:
    """Flag orange-serif (style 2) lines whose visible words are all never-highlight."""
    flags: list[str] = []
    for cue in cues:
        styles = cue.get("lineStyles") or []
        lines = cue.get("lines") or []
        for i, ln in enumerate(lines):
            if i >= len(styles) or styles[i] != 2:
                continue
            texts = [norm(x.get("text") or "") for x in ln]
            if texts and all(t in never or not t for t in texts):
                shown = " ".join(x.get("text") or "" for x in ln)
                flags.append(
                    f"  cue {cue.get('i')} @ {cue.get('startMs', 0)/1000:.2f}s "
                    f"realçou {shown!r} (conectivo)"
                )
    return flags


def run(
    transcript: Path,
    captions: Path | None,
    glossary: dict,
    drop_sequences: list[list[str]],
    cues_path: Path | None,
    write: bool,
) -> int:
    data = json.loads(transcript.read_text(encoding="utf-8"))
    words: list[dict] = list(data.get("words") or [])
    if not words:
        print("caption_qa: transcript sem words", file=sys.stderr)
        return 2

    report: list[str] = []
    dropped_total = 0
    for seq in drop_sequences:
        n = drop_sequence(words, seq)
        if n:
            dropped_total += n
            report.append(f"DROP {' '.join(seq)!r} ({n} palavras)")
        else:
            report.append(f"DROP miss {' '.join(seq)!r}")

    changes = apply_replacements(words, glossary.get("replacements") or [])
    if changes:
        report.append(f"REWRITE {len(changes)}")
        report.extend(changes)
    else:
        report.append("REWRITE nenhum")

    caps = captions_from_words(words)
    data["words"] = words
    data["text"] = rebuild_transcript_text(words)

    leftover = []
    brand_needles = {"dgd", "digid"}
    for w in words:
        if w.get("type", "word") != "word":
            continue
        core = norm(split_punct(w.get("text") or "")[1])
        if core in brand_needles:
            leftover.append(w.get("text") or "")
    if leftover:
        report.append(f"WARN ainda restam {leftover}")

    if cues_path and cues_path.exists():
        cues = json.loads(cues_path.read_text(encoding="utf-8"))
        never = {norm(x) for x in (glossary.get("never_highlight") or [])}
        flags = scan_cues(cues, never)
        if flags:
            report.append("HIGHLIGHT")
            report.extend(flags)
        else:
            report.append("HIGHLIGHT limpo")

    if write:
        transcript.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if captions:
            captions.parent.mkdir(parents=True, exist_ok=True)
            captions.write_text(json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8")
        report.append(f"WROTE {transcript}")
        if captions:
            report.append(f"WROTE {captions} ({len(caps)} words)")
    else:
        report.append("dry-run (passe --write pra gravar)")

    print("caption_qa")
    for line in report:
        print(line)
    return 1 if leftover else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="QA + glossary rewrite for Phase-2 captions")
    ap.add_argument("--transcript", type=Path, required=True, help="transcripts/cut.json")
    ap.add_argument("--captions", type=Path, help="remotion/public/captions.json (rewritten from transcript)")
    ap.add_argument("--glossary", type=Path, default=None, help="defaults to assets/brand/digd/glossary.json")
    ap.add_argument("--cues", type=Path, help="optional caption-cues.json to scan for bad highlights")
    ap.add_argument(
        "--drop-sequence",
        action="append",
        default=[],
        help='frase pra remover (ex: "Que a DGD não é"). Repetir a flag pra várias.',
    )
    ap.add_argument("--write", action="store_true", help="grava transcript + captions")
    args = ap.parse_args()

    glossary = load_glossary(args.glossary.resolve() if args.glossary else None)
    seqs = [s.split() for s in args.drop_sequence if s.strip()]
    raise SystemExit(run(
        transcript=args.transcript.resolve(),
        captions=args.captions.resolve() if args.captions else None,
        glossary=glossary,
        drop_sequences=seqs,
        cues_path=args.cues.resolve() if args.cues else None,
        write=args.write,
    ))


if __name__ == "__main__":
    main()

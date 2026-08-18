"""Emit caption-cues.json for the STACKED caption style (shortform Phase 2).

The stacked style (src/StackedCaptions.tsx) needs more than a flat word list:
words are grouped into short "cues", each cue into lines, and every line gets a
semantic style (bold-italic / small-regular / orange-serif accent / bold). Solo
punch words are flagged so the template fires a click, and emphasis words are
circled (green pencil) and fire a scratch.

Input is the transcript of the FINAL cut (same as captions_for_remotion.py) so
word times are already on the output timeline.

Usage:
    python helpers/caption_style.py --transcript <edit>/transcripts/cut.json \
        -o <edit>/remotion/public/caption-cues.json
    # optional: --lang pt (default) tunes the accent/negation word lists
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

# ---- Portuguese defaults (accent lands on content words, never on connectives) ----
EMPH = {
    "referencia", "tempo", "desculpa", "favorita", "gratis", "gratuito", "limite",
    "irrecusavel", "irresistivel", "ilimitados", "zero", "garantia", "transformar",
    "transforma", "lucro", "colossal", "obrigatoria", "nunca", "hoje", "agora",
    "inception", "segify", "impossivel", "tudo", "reconhecido", "faturamento",
    "frustrava", "desconhecido", "147", "3.000", "mil", "24", "15", "50", "100",
    "200", "400", "500", "250", "12", "ia", "inteligencia", "artificial",
    "correria", "gavetinha", "dinheiro", "clientes", "editar", "editor", "edicao",
    "vender", "faturar", "conteudo", "videos", "video", "gravar", "postar",
    "instagram", "whatsapp", "comunidade", "cursos",
    "arte", "sistema", "funil", "crm", "posts", "operacao",
    "corretora", "agencia", "digd",
}
NEG = {"nao", "nunca", "nada", "nem", "jamais"}
STOP = {
    "mas", "e", "de", "a", "o", "os", "as", "que", "do", "da", "dos", "das", "para",
    "pra", "com", "em", "no", "na", "um", "uma", "se", "por", "ao", "aos", "eu",
    "voce", "vai", "foi", "ser", "ter", "ja", "so", "ate", "como", "isso", "esse",
    "essa", "aqui", "ali", "la", "meu", "minha", "seu", "sua", "te", "me",
}
ENDBREAK = (".", ",", "!", "?", "…", ";", ":")
PAUSE_MS = 400
MAX_WORDS = 4

# A solo word is a design beat — it drops everything else off the screen to hold
# one word. That only reads if the word is actually ON screen long enough. Below
# this it renders as a one-frame flash the viewer registers as a glitch, and the
# stack it interrupted looks like it dropped a word. Fast connective speech
# routinely produces 150ms words, so this fires often; those words belong inside
# a neighbouring stack, not alone on screen.
MIN_SOLO_MS = 340


def strip_p(t: str) -> str:
    return t.strip(" .,!?;:…\"'-")


def norm(t: str) -> str:
    t = strip_p(t).lower()
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def is_short(t: str) -> bool:
    return len(strip_p(t)) <= 3


def load_words(transcript_path: Path) -> list[dict]:
    """Words on the output timeline → [{text, startMs, endMs}], abbreviations fused."""
    raw = [
        w for w in json.loads(transcript_path.read_text()).get("words", [])
        if w.get("type") == "word" and w.get("start") is not None
    ]
    words: list[dict] = []
    for w in raw:
        t = float(w["start"])
        e = float(w.get("end") or w["start"])
        if e <= t:
            e = t + 0.12
        text = (w.get("text") or "").strip()
        if not text:
            continue
        words.append({"text": text, "startMs": round(t * 1000), "endMs": round(e * 1000)})
    words.sort(key=lambda x: x["startMs"])
    # fuse abbreviation fragments ("B", ".O", ".s" -> "B.O.s")
    fused: list[dict] = []
    for w in words:
        if fused and w["text"].startswith("."):
            fused[-1]["text"] += w["text"]
            fused[-1]["endMs"] = w["endMs"]
        else:
            fused.append(dict(w))
    return fused


def is_degenerate(w: dict) -> bool:
    t = norm(w["text"])
    if t in EMPH:
        return False
    return len(strip_p(w["text"])) <= 2 or (t in STOP and len(strip_p(w["text"])) <= 3)


def too_brief(w: dict) -> bool:
    """Spoken too fast to survive as a solo cue, whatever the word is.

    Duration, not spelling — `is_degenerate` rejects words that are too SMALL to
    carry a beat; this rejects words that are too QUICK. An emphatic, meaningful
    word said in 160ms still cannot be read alone on screen.
    """
    return (w["endMs"] - w["startMs"]) < MIN_SOLO_MS


def unsolo(w: dict) -> bool:
    """Should this one-word cue be folded into a neighbour instead of standing alone?"""
    return is_degenerate(w) or too_brief(w)


def group_cues(words: list[dict]) -> list[list[dict]]:
    cues: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = (
            nxt is None
            or (nxt["startMs"] - w["endMs"] > PAUSE_MS)
            or w["text"].endswith(ENDBREAK)
            or len(cur) >= MAX_WORDS
        )
        if brk:
            cues.append(cur)
            cur = []
    # merge unstandable one-word cues backwards, then forwards — either too small
    # to carry a beat ("do", "B") or spoken too fast to be read alone
    merged: list[list[dict]] = []
    for cw in cues:
        if (
            len(cw) == 1
            and unsolo(cw[0])
            and merged
            and len(merged[-1]) < MAX_WORDS
            and cw[0]["startMs"] - merged[-1][-1]["endMs"] <= PAUSE_MS
        ):
            merged[-1].append(cw[0])
        else:
            merged.append(cw)
    fixed: list[list[dict]] = []
    i = 0
    while i < len(merged):
        cw = merged[i]
        if (
            len(cw) == 1
            and unsolo(cw[0])
            and i + 1 < len(merged)
            and len(merged[i + 1]) < MAX_WORDS
            and merged[i + 1][0]["startMs"] - cw[0]["endMs"] <= PAUSE_MS
        ):
            merged[i + 1].insert(0, cw[0])
            i += 1
            continue
        fixed.append(cw)
        i += 1
    # late-word migration: a word starting too close to a gapless cue's end never
    # becomes readable — hand it to the next cue (one word per boundary, no cascade)
    for k in range(len(fixed) - 1):
        cw, nxt = fixed[k], fixed[k + 1]
        if len(cw) > 1:
            gap = nxt[0]["startMs"] - cw[-1]["endMs"]
            remaining_ok = len(cw) > 2 or not is_degenerate(cw[0])
            if gap <= PAUSE_MS and cw[-1]["startMs"] > nxt[0]["startMs"] - 260 and remaining_ok:
                nxt.insert(0, cw.pop())
    return fixed


def build_lines(ws: list[dict]) -> list[list[dict]]:
    """No short word (<=3 chars) alone except the top line; 1-letter never alone."""
    lines: list[list[dict]] = []
    i = 0
    while i < len(ws):
        w = ws[i]
        first = len(lines) == 0
        lonely = is_short(w["text"]) and not first
        if first and len(strip_p(w["text"])) <= 1:
            lonely = True
        if lonely:
            if i + 1 < len(ws):
                line = [w, ws[i + 1]]
                i += 2
                while i < len(ws) and is_short(ws[i]["text"]) and len(line) < 3:
                    line.append(ws[i])
                    i += 1
                lines.append(line)
            else:
                lines[-1].append(w)
                i += 1
        else:
            lines.append([w])
            i += 1
    return lines


def line_styles(lines: list[list[dict]], offset: int):
    """0=bold-italic 1=regular(small) 2=serif-orange 3=bold; plus emph/boost flags."""
    n = len(lines)
    serif_i = None
    for i, ln in enumerate(lines):
        if any(norm(w["text"]) in EMPH for w in ln):
            serif_i = i
            break
    if serif_i is None and n >= 3:
        best = (-1, 0)
        for i, ln in enumerate(lines):
            # A line that OPENS on a connective ("um pouquinho") must not take
            # the orange serif — the first visible word is the one the eye reads
            # as the highlight. Measured on the dig.D test: "um" painted orange
            # on the CTA because "pouquinho" won on length.
            first = norm(ln[0]["text"]) if ln else ""
            if first in STOP or first in NEG:
                continue
            for w in ln:
                t = norm(w["text"])
                if t in STOP or t in NEG:
                    continue
                if len(t) > best[1]:
                    best = (i, len(t))
        if best[0] >= 0:
            serif_i = best[0]
    styles: list[int] = []
    emph = [False] * n
    alt = [0, 3, 1]
    ai = offset % 3
    for i, ln in enumerate(lines):
        if i == serif_i:
            styles.append(2)
            emph[i] = any(norm(w["text"]) in EMPH for w in ln)
            continue
        txts = [norm(w["text"]) for w in ln]
        if any(t in NEG for t in txts):
            styles.append(0 if i % 2 == 0 else 3)
        elif all(t in STOP for t in txts) and not (i == 0 and len(ln) == 1):
            styles.append(1)
        else:
            styles.append(alt[ai % 3])
            ai += 1
    boost = [False] * n
    if len(lines[0]) == 1 and len(strip_p(lines[0][0]["text"])) <= 3:
        boost[0] = True
        if styles[0] in (1, 2):
            styles[0] = 0
    return styles, boost, emph


def build_cues(words: list[dict]) -> list[dict]:
    groups = group_cues(words)
    out: list[dict] = []
    solo_alt = 0
    for ci, cw in enumerate(groups):
        start = cw[0]["startMs"]
        end = cw[-1]["endMs"]
        if len(cw) == 1:
            if norm(cw[0]["text"]) in EMPH:
                preset = "SOLO_OUTLINE"
            else:
                preset = "SOLO_BIG" if solo_alt % 2 == 0 else "SOLO_OUTLINE"
            solo_alt += 1
            lines = [[cw[0]]]
            styles, boost, emph = [0], [False], [False]
        else:
            preset = "STACK_MIXED"
            lines = build_lines(cw)
            styles, boost, emph = line_styles(lines, ci)
        nxt_start = groups[ci + 1][0]["startMs"] if ci + 1 < len(groups) else None
        gap_after = (nxt_start - end) if nxt_start is not None else 9999
        exit_ = "blur_up" if gap_after > PAUSE_MS else "abrupt"
        out.append({
            "i": ci, "startMs": start, "endMs": end, "preset": preset, "exit": exit_,
            "styleOffset": ci % 4, "lineStyles": styles, "lineBoost": boost, "lineEmph": emph,
            "lines": [[{"text": x["text"], "fromMs": x["startMs"], "toMs": x["endMs"]} for x in ln] for ln in lines],
        })
    # gapless: extend endMs to next cue start when the gap is small
    for k in range(len(out)):
        lastto = max(w["toMs"] for ln in out[k]["lines"] for w in ln)
        if k + 1 < len(out):
            nxt = out[k + 1]["startMs"]
            gap = nxt - lastto
            out[k]["endMs"] = nxt if gap <= 700 else min(lastto + 400, nxt)
        else:
            out[k]["endMs"] = lastto + 500
    return out


def load_glossary_emph(path: Path | None) -> None:
    """Merge brand-pack EMPH words into the module-level set (pt-BR + marca)."""
    if path is None:
        candidate = Path(__file__).resolve().parent.parent / "assets" / "brand" / "digd" / "glossary.json"
        path = candidate if candidate.exists() else None
    if path is None or not path.exists():
        return
    extra = json.loads(path.read_text(encoding="utf-8")).get("emph") or []
    for w in extra:
        EMPH.add(norm(w))


def main() -> None:
    ap = argparse.ArgumentParser(description="→ caption-cues.json (stacked caption style)")
    ap.add_argument("--transcript", type=Path, required=True, help="Transcript of the final cut.mp4")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output caption-cues.json path")
    ap.add_argument("--lang", default="pt", help="Language hint for accent/negation lists (default pt)")
    ap.add_argument("--glossary", type=Path, default=None, help="brand glossary (adds EMPH words)")
    args = ap.parse_args()

    load_glossary_emph(args.glossary.resolve() if args.glossary else None)
    words = load_words(args.transcript.resolve())
    cues = build_cues(words)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    solos = sum(1 for c in cues if c["preset"] != "STACK_MIXED")
    circled = sum(1 for c in cues if c["preset"] == "SOLO_OUTLINE")
    print(f"{args.output} — {len(cues)} cues ({solos} solo, {circled} circled) from {len(words)} words")


if __name__ == "__main__":
    main()

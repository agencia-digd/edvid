#!/usr/bin/env python3
"""Emit one event per save from the preview UI.

Two files, both written by the UI and neither able to reach the chat on its own:
  - preview_edits.json — timeline adjustments and correction markers
  - preview_style.json — the Fase 1 → Fase 2 gate: editing style, caption style,
    edit elements

Run this under the Monitor tool so every save notifies the session automatically:

    Monitor(command="python3 <skill>/helpers/watch_edits.py '<edit>'",
            description="marcações do preview", persistent=True)

Each stdout line is one notification. Stays quiet while nothing changes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def digest(p: Path) -> str:
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"preview_edits.json ilegível ({e.__class__.__name__}) — peça ao usuário para salvar de novo"

    parts: list[str] = []
    notes = d.get("notes") or []
    for i, n in enumerate(notes, 1):
        start, end = n.get("start", 0), n.get("end", 0)
        fase = n.get("phase", 1)
        parts.append(f'  {i}. [{fmt(start)} → {fmt(end)}] (fase {fase}) {n.get("text", "").strip()}')

    edl = d.get("edl") or {}
    n_ch = len(edl.get("changes") or [])
    n_rm = len(edl.get("removed") or [])
    ed = d.get("editData") or {}

    head = []
    if notes:
        head.append(f"{len(notes)} marcação(ões)")
    if n_ch:
        head.append(f"{n_ch} take(s) reajustado(s)")
    if n_rm:
        head.append(f"{n_rm} take(s) removido(s)")
    if ed:
        head.append("inserts/gancho movidos")
    if not head:
        head.append("ajustes salvos")

    out = [f"AJUSTES DO PREVIEW ({d.get('savedAt', '')}) — {', '.join(head)}. Aplique-os."]
    out += parts
    return "\n".join(out)


def style_digest(p: Path) -> str:
    """The gate choice: what Fase 2 should be built as."""
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return f"preview_style.json ilegível ({e.__class__.__name__}) — peça ao usuário para salvar de novo"

    els = d.get("elementNames") or [
        k for k, v in (d.get("elements") or {}).items() if v
    ]
    head = (
        "TROCA DE ESTILO NO PREVIEW ({}) — REFAÇA a Fase 2 assim:"
        if d.get("rerender")
        else "ESTILO ESCOLHIDO NO PREVIEW ({}) — monte a Fase 2 assim:"
    ).format(d.get("savedAt", ""))
    out = [
        head,
        f'  · tipo de edição: {d.get("editName") or d.get("edit")}',
        f'  · headline: {d.get("headlineName") or d.get("headline")}',
        f'  · legenda: {d.get("captionsName") or d.get("captions")}',
    ]
    # Only worth reporting when the chosen styles actually paint an accent —
    # naming a colour that nothing uses reads as an instruction to go find a
    # place for it.
    accent = d.get("accent")
    if accent:
        if d.get("accentUsed"):
            name = d.get("accentName") or accent
            label = f"{name} ({accent})" if name.lower() != str(accent).lower() else accent
            out.append(f"  · cor de destaque: {label}")
        else:
            out.append("  · cor de destaque: não se aplica (os estilos escolhidos não usam destaque)")
    out.append(f'  · elementos: {", ".join(els) if els else "nenhum"}')
    # everything NOT chosen is an instruction too — it is what must stay out
    off = [k for k, v in (d.get("elements") or {}).items() if not v]
    if off:
        out.append(f'  · fora: {", ".join(off)}')
    if (d.get("note") or "").strip():
        out.append(f'  · observação do usuário: {d["note"].strip()}')
    return "\n".join(out)


def fmt(t: float) -> str:
    m, s = divmod(max(0.0, float(t)), 60)
    return f"{int(m)}:{s:05.2f}"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()
    watched = {
        root / "preview_edits.json": digest,
        root / "preview_style.json": style_digest,
    }
    # a file already sitting there at startup means it is pending — say so once
    last: dict[Path, float | None] = {}
    for target, fn in watched.items():
        last[target] = target.stat().st_mtime if target.exists() else None
        if last[target] is not None:
            print(fn(target), flush=True)

    while True:
        for target, fn in watched.items():
            try:
                cur = target.stat().st_mtime if target.exists() else None
            except OSError:
                cur = None
            if cur is not None and cur != last[target]:
                last[target] = cur
                time.sleep(0.15)  # let the atomic replace settle before reading
                print(fn(target), flush=True)
            elif cur is None:
                last[target] = None  # applied and deleted — re-arm for the next save
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())

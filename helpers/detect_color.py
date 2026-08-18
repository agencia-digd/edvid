#!/usr/bin/env python3
"""Identify a source's colour profile, and size the grade it needs.

Phase 1 used to ASK the user "NORMAL or LOG?". It doesn't have to: the answer is in
the file. This resolves it in two tiers.

  Tier 1 — metadata.  Definitive when present.  HLG and PQ declare themselves in
  `color_transfer`; Apple Log has a reliable signature (ProRes 10-bit 4:2:2 +
  BT.2020 primaries + an EMPTY transfer tag); vendor LOG curves sometimes leave a
  tag naming themselves.

  Tier 2 — image statistics.  Needed because the metadata is often silent: a Sony
  shooting S-Log3 to H.264 frequently declares plain bt709, and ANY transcode
  drops the tags.  A LOG curve lifts the black floor (it never reaches true
  black), compresses the range, and desaturates.  That is measurable.

The failure mode worth knowing: a brightly-lit normal scene with no shadows (a
white-background product shot, a blown window behind a subject) ALSO has a lifted
black floor.
So Tier 2 requires several signals to agree and reports a confidence, rather than
pretending to be certain.  At `low` confidence the caller should ask.

For a LOG source the expansion is built FROM THE MEASUREMENT (the real black floor
and white ceiling of this footage) rather than from a guessed vendor curve — the
one exception is Apple Log, which has an approved preset carrying a hue correction
that only that curve needs.

CLI:
    detect_color.py <video> [--json] [--samples 24]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# TV-range anchors, normalized 0..1
TV_BLACK = 16 / 255
TV_WHITE = 235 / 255

VENDOR_LOG_HINTS = {
    "slog3": "sony_slog3", "s-log3": "sony_slog3", "sgamut": "sony_slog3",
    "s-gamut": "sony_slog3", "vlog": "panasonic_vlog", "v-log": "panasonic_vlog",
    "clog": "canon_clog3", "c-log": "canon_clog3", "logc": "arri_logc",
    "n-log": "nikon_nlog", "nlog": "nikon_nlog", "f-log": "fuji_flog",
    "flog": "fuji_flog", "dlog": "dji_dlog", "d-log": "dji_dlog",
}


def _probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=codec_name,profile,pix_fmt,color_transfer,color_primaries,"
         "color_space,color_range,bits_per_raw_sample",
         "-show_entries", "stream_tags", "-show_entries", "format_tags",
         "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    try:
        d = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    st = (d.get("streams") or [{}])[0]
    tags = {**(d.get("format", {}).get("tags") or {}), **(st.get("tags") or {})}
    st["_tags"] = tags
    return st


def _sample_stats(video: Path, n: int = 24) -> dict[str, float] | None:
    """Per-frame YMIN/YMAX/SATAVG over the whole clip, normalized to 0..1.

    Returns the 10th-percentile YMIN (the clip's realistic black floor — a single
    dead-pixel frame shouldn't decide it) and the 90th-percentile YMAX.
    """
    try:
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)]).decode().strip())
    except Exception:
        dur = 10.0
    fps = max(0.2, min(n / max(dur, 0.1), 8.0))

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as f:
        meta = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(video),
             "-vf", f"fps={fps:.3f},signalstats,metadata=print:file={meta}",
             "-an", "-f", "null", "-"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ymin: list[float] = []
        ymax: list[float] = []
        yavg: list[float] = []
        sat: list[float] = []
        depth = 8
        for line in Path(meta).read_text().splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.rpartition("=")
            try:
                v = float(val)
            except ValueError:
                continue
            if key.endswith("YBITDEPTH"):
                depth = int(v)
            elif key.endswith("YMIN"):
                ymin.append(v)
            elif key.endswith("YMAX"):
                ymax.append(v)
            elif key.endswith("YAVG"):
                yavg.append(v)
            elif key.endswith("SATAVG"):
                sat.append(v)
        if not ymin or not ymax:
            return None

        mx = (2 ** depth) - 1

        def pct(xs: list[float], p: float) -> float:
            s = sorted(xs)
            return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))] / mx

        return {
            "black_floor": pct(ymin, 0.10),
            "white_ceiling": pct(ymax, 0.90),
            "y_mean": (sum(yavg) / len(yavg) / mx) if yavg else 0.5,
            "sat_mean": (sum(sat) / len(sat) / mx) if sat else 0.25,
            "frames": len(ymin),
            "bit_depth": depth,
        }
    except subprocess.CalledProcessError:
        return None
    finally:
        Path(meta).unlink(missing_ok=True)


def _measured_log_expansion(s: dict[str, float]) -> str:
    """Build a LOG expansion from what this footage actually measures.

    colorlevels maps [black_floor, white_ceiling] back onto [0,1]; the eq that
    follows restores the contrast and saturation the log curve gave away.  Bounded
    so a mis-detection degrades to a mild correction instead of a wrecked image.
    """
    bf = max(0.0, min(0.30, s["black_floor"] - 0.01))
    wc = max(0.55, min(1.0, s["white_ceiling"] + 0.01))
    rng = wc - bf
    contrast = max(1.0, min(1.45, 0.72 / max(rng, 0.2)))
    sat = max(1.0, min(1.35, 0.22 / max(s["sat_mean"], 0.06)))
    return (
        f"colorlevels=rimin={bf:.3f}:gimin={bf:.3f}:bimin={bf:.3f}:"
        f"rimax={wc:.3f}:gimax={wc:.3f}:bimax={wc:.3f},"
        f"eq=contrast={contrast:.2f}:saturation={sat:.2f}"
    )


def detect(video: Path, samples: int = 24) -> dict:
    st = _probe(video)
    tags = st.get("_tags", {})
    trc = (st.get("color_transfer") or "").lower()
    prim = (st.get("color_primaries") or "").lower()
    codec = (st.get("codec_name") or "").lower()
    pix = (st.get("pix_fmt") or "").lower()
    blob = " ".join(f"{k}={v}" for k, v in tags.items()).lower()

    ev: list[str] = []
    r = {"profile": None, "confidence": None, "preset": None, "grade": None,
         "evidence": ev, "stats": None, "tier": None}

    # ---- Tier 1: metadata ---------------------------------------------------
    if trc == "arib-std-b67":
        ev.append("color_transfer=arib-std-b67")
        r.update(profile="hlg", confidence="high", tier="metadata", preset="",
                 grade="", note="render.py tonemaps HLG; grade corretivo leve só se preciso")
        return r
    if trc == "smpte2084":
        ev.append("color_transfer=smpte2084")
        r.update(profile="pq", confidence="high", tier="metadata", preset="",
                 grade="", note="render.py tonemaps PQ; grade corretivo leve só se preciso")
        return r

    named = tags.get("com.apple.proapps.logprofile")
    if named:
        ev.append(f"tag com.apple.proapps.logprofile={named}")
        r.update(profile="apple_log", confidence="high", tier="metadata",
                 preset="apple_log", grade="apple_log")
        return r

    for hint, prof in VENDOR_LOG_HINTS.items():
        if hint in blob:
            ev.append(f"tag contém '{hint}'")
            s = _sample_stats(video, samples)
            r.update(profile=prof, confidence="high", tier="metadata",
                     preset=None, grade=_measured_log_expansion(s) if s else "",
                     stats=s, note="expansão medida da própria imagem")
            return r

    # Apple Log's real signature: ProRes 10-bit 4:2:2, BT.2020 primaries, and an
    # EMPTY transfer tag. Nothing in the file spells out "Apple Log" — if you wait
    # for a tag that names it you will never find one.
    if codec == "prores" and "422p10" in pix and prim == "bt2020" and trc in ("", "unknown"):
        ev.append("prores 10-bit 4:2:2 + primaries bt2020 + transfer vazio")
        r.update(profile="apple_log", confidence="high", tier="metadata",
                 preset="apple_log", grade="apple_log")
        return r

    # ---- Tier 2: image statistics -------------------------------------------
    s = _sample_stats(video, samples)
    r["stats"] = s
    r["tier"] = "estatística"
    if not s:
        ev.append("análise de imagem falhou")
        r.update(profile="rec709", confidence="low", preset="", grade="")
        return r

    bf, wc, sat = s["black_floor"], s["white_ceiling"], s["sat_mean"]
    rng = wc - bf

    # Each signal is independent evidence of a log curve.
    score = 0
    if bf > TV_BLACK + 0.045:                      # blacks never land
        score += 2
        ev.append(f"piso de preto alto ({bf:.3f}, TV black={TV_BLACK:.3f})")
    elif bf > TV_BLACK + 0.02:
        score += 1
        ev.append(f"piso de preto levemente alto ({bf:.3f})")
    else:
        ev.append(f"piso de preto normal ({bf:.3f}) — chega ao preto real")

    if wc < 0.80:
        score += 1
        ev.append(f"teto de branco baixo ({wc:.3f})")
    else:
        ev.append(f"teto de branco normal ({wc:.3f})")

    if rng < 0.62:
        score += 2
        ev.append(f"faixa comprimida ({rng:.3f})")
    else:
        ev.append(f"faixa normal ({rng:.3f})")

    # signalstats SATAVG is chroma magnitude in the native bit depth, NOT a 0..1
    # "saturation" — normalized it lands far lower than intuition suggests.
    # Measured on real material: a warm, well-saturated talking head reads 0.052;
    # the same frame pushed to eq=saturation=0.45 reads 0.014.  Thresholds picked
    # off those numbers.  Do not "fix" them upward to look like percentages — that
    # adds points to every clip and manufactures false LOG detections.
    if sat < 0.025:
        score += 2
        ev.append(f"saturação muito baixa ({sat:.3f})")
    elif sat < 0.040:
        score += 1
        ev.append(f"saturação baixa ({sat:.3f})")
    else:
        ev.append(f"saturação normal ({sat:.3f})")

    if score >= 5:
        r.update(profile="log_desconhecido", confidence="high",
                 grade=_measured_log_expansion(s),
                 note="expansão medida da própria imagem")
    elif score >= 3:
        r.update(profile="log_desconhecido", confidence="low",
                 grade=_measured_log_expansion(s),
                 note="ambíguo — cena clara e sem sombras dá a mesma assinatura; confirmar com o usuário")
    else:
        r.update(profile="rec709", confidence="high", preset="", grade="")
    r["score"] = score
    return r


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--samples", type=int, default=24)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    if not a.video.exists():
        sys.exit(f"não encontrado: {a.video}")

    r = detect(a.video, a.samples)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    print(f"perfil: {r['profile']}   (confiança {r['confidence']}, via {r['tier']})")
    for e in r["evidence"]:
        print(f"  · {e}")
    if r.get("stats"):
        s = r["stats"]
        print(f"  amostras: {s['frames']} frames, {s['bit_depth']}-bit")
    if r.get("note"):
        print(f"  nota: {r['note']}")
    g = r.get("grade")
    print(f"grade: {g if g else '(nenhum — imagem sai como gravada)'}")
    if r["confidence"] == "low":
        print("AÇÃO: confiança baixa — pergunte ao usuário antes de aplicar.")


if __name__ == "__main__":
    main()

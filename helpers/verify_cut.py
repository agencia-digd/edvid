"""Numeric self-eval of a rendered cut against its EDL — TEXT ONLY, no images.

Replaces the old "timeline_view every boundary" pass: run this first, then open
a timeline_view ONLY on the junctions it flags (usually 0–2, not all of them).

Checks:
  - duration: rendered vs EDL expectation
  - per-junction audio, three tiny probe windows around each cut point t:
      pre  [t-0.13, t-0.05]  hot (> -6 dBFS) → word may be clipped at segment end
      jct  [t-0.015, t+0.015] hot (> -8 dBFS) → pop / missing 30ms fade
      post [t+0.05, t+0.13]  hot (> -6 dBFS) → word onset may be clipped
  - dead air: silences ≥ --min-silence anywhere in the cut (near-junction noted)
  - black frames (blackdetect) — a cut should never produce black
  - clipping: astats peak + flat factor on the whole render

Usage:
    python helpers/verify_cut.py <edit>/edl.json <edit>/cut.mp4
    python helpers/verify_cut.py edl.json cut.mp4 --min-silence 1.2   # longform
Exit code 0 = clean, 1 = has CHECK flags.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

POP_DB = -8.0     # junction window louder than this → suspected pop
HOT_DB = -6.0     # pre/post window louder than this → suspected clipped word
# A range this far under the median range reads as "I can't hear that bit" on a
# phone speaker. Deliberately looser than voice_levels' 5dB source threshold:
# by this point a gain has been applied and the remaining gap is intentional
# delivery dynamics, which should not be flattened to zero.
LEVEL_DROP_DB = 4.0


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def peak_db(path: Path, start: float, dur: float) -> float:
    """Max volume (dBFS) of a tiny audio window. -99 if silent/empty."""
    if start < 0:
        dur += start
        start = 0.0
    if dur <= 0:
        return -99.0
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", r.stderr)
    return float(m.group(1)) if m else -99.0


def rms_db(path: Path, start: float, dur: float) -> float:
    """Overall RMS (dBFS) of a window. -99 if silent/empty.

    RMS, not peak: this measures how loud a passage FEELS across its length,
    which is what an under-level take fails at. Peak only reports the single
    loudest plosive and a whispered clause can share a peak with a shouted one.
    """
    if start < 0:
        dur += start
        start = 0.0
    if dur <= 0:
        return -99.0
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", str(path), "-vn", "-af", "astats=measure_perchannel=none",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"RMS level dB:\s*(-?[\d.]+|-?inf)", r.stderr)
    if not m:
        return -99.0
    try:
        return float(m.group(1))
    except ValueError:
        return -99.0


def audio_scan(path: Path, min_silence: float) -> tuple[list[tuple[float, float]], float, float]:
    """One decode pass: silences [(start,end)...], overall peak dB, flat factor."""
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-vn",
         "-af", f"silencedetect=noise=-35dB:d={min_silence},astats=measure_perchannel=none",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    err = r.stderr
    silences: list[tuple[float, float]] = []
    cur: float | None = None
    for line in err.splitlines():
        ms = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        me = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if ms:
            cur = float(ms.group(1))
        elif me and cur is not None:
            silences.append((cur, float(me.group(1))))
            cur = None
    peak = -99.0
    mp = re.search(r"Peak level dB:\s*(-?[\d.]+|-inf)", err)
    if mp and mp.group(1) != "-inf":
        peak = float(mp.group(1))
    flat = 0.0
    mf = re.search(r"Flat factor:\s*([\d.]+)", err)
    if mf:
        flat = float(mf.group(1))
    return silences, peak, flat


def black_scan(path: Path) -> list[tuple[float, float]]:
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-an",
         "-vf", "blackdetect=d=0.05:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    out: list[tuple[float, float]] = []
    for m in re.finditer(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", r.stderr):
        out.append((float(m.group(1)), float(m.group(2))))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Numeric verification of a rendered cut vs its EDL")
    ap.add_argument("edl", type=Path)
    ap.add_argument("video", type=Path)
    ap.add_argument("--min-silence", type=float, default=0.5,
                    help="silence length (s) worth flagging (longform: ~1.2)")
    ap.add_argument("--json", action="store_true", help="machine output")
    args = ap.parse_args()

    edl = json.loads(args.edl.read_text())
    ranges = edl["ranges"]

    # Under a J-cut the output is SHORTER than the sum of the ranges and the picture
    # cuts do not fall on the cumulative range boundaries. Reading the timeline off
    # the ranges then probes the wrong instants — mid-word, several frames past the
    # real cut — and reports the loud syllable it finds there as a pop.
    jt = edl.get("jcut_timeline") or []
    jcut = len(jt) == len(ranges) and len(jt) > 0
    if jcut:
        out_spans = [(j["video_start_in_output"], j["video_duration"]) for j in jt]
        expected = sum(j["video_duration"] for j in jt)
        junctions = [j["video_start_in_output"] for j in jt[1:]]
        # audio seams: where the incoming take's sound actually starts
        aud_seams = [(j["audio_start_in_output"], jt[i]["audio_start_in_output"]
                      + jt[i]["audio_duration"]) for i, j in enumerate(jt[1:])]
    else:
        durs = [float(r["end"]) - float(r["start"]) for r in ranges]
        expected = sum(durs)
        junctions = []
        out_spans = []
        t = 0.0
        for d in durs:
            out_spans.append((t, d))
            t += d
        junctions = [s for s, _ in out_spans[1:]]
        aud_seams = []

    actual = probe_duration(args.video)

    with ThreadPoolExecutor(max_workers=8) as ex:
        pre_f = [ex.submit(peak_db, args.video, j - 0.13, 0.08) for j in junctions]
        jct_f = [ex.submit(peak_db, args.video, j - 0.015, 0.03) for j in junctions]
        post_f = [ex.submit(peak_db, args.video, j + 0.05, 0.08) for j in junctions]
        sil_f = ex.submit(audio_scan, args.video, args.min_silence)
        blk_f = ex.submit(black_scan, args.video)
        # Each range's level ON THE OUTPUT TIMELINE, so gains already applied.
        rms_f = [ex.submit(rms_db, args.video, s, d) for s, d in out_spans]
        pres = [f.result() for f in pre_f]
        jcts = [f.result() for f in jct_f]
        posts = [f.result() for f in post_f]
        silences, peak, flat = sil_f.result()
        blacks = blk_f.result()
        rmss = [f.result() for f in rms_f]

    flags = 0
    rows = []
    for i, j in enumerate(junctions):
        verdicts = []
        if jcut:
            # There is no audio cut at a J-cut junction — the takes overlap and the
            # sound runs straight through. Pop/hot probes would just be measuring
            # whatever syllable happens to sit there, so the meaningful test is
            # whether the seam is genuinely continuous: the incoming take's audio
            # must start BEFORE the outgoing take's audio ends.
            a_in, prev_end = aud_seams[i]
            lap = prev_end - a_in
            if lap <= 0:
                verdicts.append(f"GAP-{abs(lap) * 1000:.0f}ms")
            else:
                verdicts.append(f"encavalado {lap * 1000:.0f}ms")
        else:
            if jcts[i] > POP_DB:
                verdicts.append("POP?")
            if pres[i] > HOT_DB:
                verdicts.append("HOT-TAIL")
            if posts[i] > HOT_DB:
                verdicts.append("HOT-HEAD")
        v = " ".join(verdicts) if verdicts else "ok"
        if verdicts and any(x.startswith(("POP", "HOT", "GAP")) for x in verdicts):
            flags += 1
        rows.append((i + 1, j, pres[i], jcts[i], posts[i], v))

    # silences not at the very head/tail, annotated with the nearest junction
    sil_rows = []
    for s, e in silences:
        if e < 0.4 or s > actual - 0.4:
            continue
        near = min(junctions, key=lambda j: abs(j - (s + e) / 2)) if junctions else None
        near_i = junctions.index(near) + 1 if near is not None and abs(near - (s + e) / 2) < 1.5 else None
        sil_rows.append((s, e, near_i))
        flags += 1

    # Range level balance: does any take sit well under the rest of the cut?
    #
    # This is the post-render counterpart to voice_levels.py. voice_levels finds
    # under-level speech in the SOURCE and sizes a gain_db; this checks whether
    # the gain actually landed, by comparing each range against the median of
    # the others in the finished render. Unlike voice_levels' run detector, the
    # reference here is not a threshold the range was selected by, so it does
    # converge — a corrected take stops being flagged.
    valid_rms = [x for x in rmss if x > -90]
    ref_rms = sorted(valid_rms)[len(valid_rms) // 2] if valid_rms else -99.0
    level_rows = []
    for i, x in enumerate(rmss):
        delta = x - ref_rms
        low = x > -90 and delta <= -LEVEL_DROP_DB
        if low:
            flags += 1
        level_rows.append((i, x, delta, "LOW-LEVEL" if low else "ok"))

    dur_ok = abs(actual - expected) <= 0.5
    if not dur_ok:
        flags += 1
    clip_bad = flat > 0.001 or peak >= -0.1
    if clip_bad:
        flags += 1
    flags += len(blacks)

    if args.json:
        print(json.dumps({
            "expected_s": round(expected, 2), "actual_s": round(actual, 2),
            "duration_ok": dur_ok, "peak_db": peak, "flat_factor": flat,
            "junctions": [{"n": n, "t": round(j, 2), "pre_db": p, "jct_db": c,
                           "post_db": q, "verdict": v}
                          for n, j, p, c, q, v in rows],
            "silences": [{"start": round(s, 2), "end": round(e, 2),
                          "near_junction": n} for s, e, n in sil_rows],
            "range_levels": [{"index": i, "rms_db": round(x, 1),
                              "delta_db": round(d, 1), "verdict": v}
                             for i, x, d, v in level_rows],
            "black": [{"start": s, "end": e} for s, e in blacks],
            "flags": flags,
        }, ensure_ascii=False))
        sys.exit(0 if flags == 0 else 1)

    print(f"{args.video.name} vs {args.edl.name} — {len(ranges)} ranges, {len(junctions)} junctions")
    print(f"duration: {actual:.2f}s vs expected {expected:.2f}s (Δ {actual - expected:+.2f}s) "
          f"{'ok' if dur_ok else 'CHECK'}")
    print(f"audio: peak {peak:.1f} dBFS, flat_factor {flat:.2f} "
          f"{'CHECK-CLIPPING' if clip_bad else 'ok'}")
    if jcut:
        print("junções J-cut (o áudio não corta aqui — o teste é a continuidade "
              "do encavalamento; GAP = respiro reaberto):")
        for n, j, p, c, q, v in rows:
            print(f"  #{n:<3} corte de imagem {j:7.3f}s   {v}")
    else:
        print(f"junctions (dBFS peaks — pre[-130..-50ms] jct[±15ms] post[+50..+130ms]; "
              f"pop > {POP_DB:.0f}, hot > {HOT_DB:.0f}):")
        for n, j, p, c, q, v in rows:
            print(f"  #{n:<3} {j:7.2f}s   pre {p:6.1f}   jct {c:6.1f}   post {q:6.1f}   {v}")
    if sil_rows:
        print(f"silences ≥ {args.min_silence:.2f}s:")
        for s, e, n in sil_rows:
            near = f" (near junction #{n})" if n else ""
            print(f"  {s:7.2f}–{e:.2f}s ({e - s:.2f}s){near}  CHECK-DEADAIR")
    else:
        print(f"silences ≥ {args.min_silence:.2f}s: none")
    print(f"black frames: {', '.join(f'{s:.2f}–{e:.2f}s' for s, e in blacks) or 'none'}"
          f"{'  CHECK' if blacks else ''}")
    n_low = sum(1 for _, _, _, v in level_rows if v != "ok")
    print(f"range levels (RMS vs median range, low < {-LEVEL_DROP_DB:.0f} dB): "
          f"{'balanced' if n_low == 0 else f'{n_low} under-level'}")
    for i, x, d, v in level_rows:
        if v != "ok":
            beat = ranges[i].get("beat", "")
            g = float(ranges[i].get("gain_db", 0.0) or 0.0)
            gtxt = f", gain_db {g:+.1f} applied" if g else ", no gain_db set"
            print(f"  [{i:02d}] {x:6.1f} dBFS  {d:+5.1f} dB  {beat}  {v}{gtxt}"
                  f"  → raise gain_db (voice_levels.py sizes it)")
    if flags == 0:
        print("VERDICT: clean — no visual inspection needed")
    else:
        print(f"VERDICT: {flags} flag(s) — inspect ONLY the flagged spots with timeline_view")
    sys.exit(0 if flags == 0 else 1)


if __name__ == "__main__":
    main()

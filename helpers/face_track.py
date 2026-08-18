"""Face / eye-line tracking pre-pass for the Phase-2 dynamic camera.

Detects the largest frontal face per frame (OpenCV Haar), takes the eye-line
point (~42% down the face box), fills gaps, and smooths the path. Outputs a
normalized per-frame point that Remotion uses to keep the eyes at a fixed
target while zoomed in (a gentle follow, no black edges).

Output JSON: {fps, width, height, count, points: [[cx,cy], ...]}  (cx,cy in 0..1)

Usage:
    python helpers/face_track.py cut.mp4 -o remotion/public/track.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2


def detect(video: Path) -> tuple[float, int, int, list]:
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cascade = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))

    DW = 480  # detect on a downscaled frame for speed
    scale = DW / W if W else 1.0
    raw: list[tuple[float, float] | None] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, (DW, int(H * scale)))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = (x + w / 2) / DW
            cy = (y + h * 0.42) / (H * scale)
            raw.append((cx, cy))
        else:
            raw.append(None)
    cap.release()
    return fps, W, H, raw


def fill_and_smooth(raw: list, win: int) -> list[list[float]]:
    # default center if nothing ever detected
    known = [p for p in raw if p is not None] or [(0.5, 0.42)]
    # forward/backward fill
    filled: list[tuple[float, float]] = []
    last = known[0]
    for p in raw:
        if p is not None:
            last = p
        filled.append(last)
    # back-fill leading Nones
    first = next((p for p in raw if p is not None), known[0])
    for i, p in enumerate(raw):
        if p is None:
            filled[i] = first
        else:
            break
    # moving-average smooth
    n = len(filled)
    out: list[list[float]] = []
    for i in range(n):
        a = max(0, i - win)
        b = min(n, i + win + 1)
        seg = filled[a:b]
        cx = sum(s[0] for s in seg) / len(seg)
        cy = sum(s[1] for s in seg) / len(seg)
        out.append([round(cx, 4), round(cy, 4)])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Face/eye tracking pre-pass (OpenCV)")
    ap.add_argument("video", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--smooth", type=float, default=0.5,
                    help="smoothing window in seconds (default 0.5)")
    args = ap.parse_args()

    fps, W, H, raw = detect(args.video.resolve())
    win = max(1, int(fps * args.smooth))
    points = fill_and_smooth(raw, win)
    detected = sum(1 for p in raw if p is not None)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "fps": fps, "width": W, "height": H,
        "count": len(points), "detected": detected,
        "points": points,
    }))
    print(f"{args.output} — {len(points)} frames, {detected} with a face "
          f"({100*detected//max(1,len(points))}%)")


if __name__ == "__main__":
    main()

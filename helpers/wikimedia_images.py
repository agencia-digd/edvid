"""Wikimedia Commons image search — freely-licensed images of people, places,
and things the transcript names. No API key. Cleaner licensing than raw web
search; great for PERSONALITIES and PLACES. (Brand LOGOS are often non-free and
absent from Commons — use google_images.py for those.)

Usage:
    python helpers/wikimedia_images.py "Neymar" --out-dir <dir> --count 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "edvid/1.0 (short-form editor; contact via skill)"}


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40]


def search(query: str, count: int, width: int) -> list[dict]:
    params = {
        "action": "query", "format": "json",
        "generator": "search", "gsrsearch": query,
        "gsrnamespace": "6",           # File namespace
        "gsrlimit": str(max(1, min(count * 3, 30))),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "iiurlwidth": str(width),
    }
    r = requests.get(API, params=params, headers=UA, timeout=60)
    r.raise_for_status()
    pages = (r.json().get("query", {}) or {}).get("pages", {})
    out = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        if not mime.startswith("image/") or mime == "image/svg+xml":
            continue
        meta = ii.get("extmetadata", {}) or {}
        out.append({
            "url": ii.get("thumburl") or ii.get("url"),
            "page": ii.get("descriptionurl", ""),
            "license": (meta.get("LicenseShortName", {}) or {}).get("value", "?"),
            "artist": (meta.get("Artist", {}) or {}).get("value", "?"),
        })
    # Return the full candidate pool (not just `count`) so the caller can skip
    # any whose thumbnail fails to generate and fall through to the next one.
    return out


# JPEG (FFD8FF), PNG (89 50 4E 47), GIF (GIF8), WEBP (RIFF....WEBP)
def is_image_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    return (
        data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:4] == b"GIF8"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Download freely-licensed images from Wikimedia Commons")
    ap.add_argument("query")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--width", type=int, default=1200)
    args = ap.parse_args()

    results = search(args.query, args.count, args.width)
    if not results:
        sys.exit(f"no Commons results for: {args.query}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(args.query)
    saved = 0
    import re
    # Walk the candidate pool, keeping only real image bytes, until we have
    # `count` valid files. Wikimedia's thumb server sometimes returns an HTML
    # error page instead of the image — those are detected and skipped.
    for it in results:
        if saved >= args.count:
            break
        if not it["url"]:
            continue
        try:
            data = requests.get(it["url"], headers=UA, timeout=90).content
        except Exception as e:
            print(f"  x {e}")
            continue
        if not is_image_bytes(data):
            print(f"  x thumbnail failed (not an image), skipping: {it['page']}")
            continue
        dest = args.out_dir / f"{slug}-{saved + 1}.jpg"
        dest.write_bytes(data)
        saved += 1
        artist = re.sub("<[^>]+>", "", it["artist"])[:50]
        print(f"  + {dest}  ({it['license']} — {artist}) {it['page']}")
    print(f"downloaded {saved}/{args.count} → {args.out_dir}")
    if saved < args.count:
        print(f"  note: only {saved} valid image(s) found for '{args.query}' "
              f"(some thumbnails failed or the query was too narrow)")
    if saved:
        print("attribution: keep the license + author above (Wikimedia Commons).")


if __name__ == "__main__":
    main()

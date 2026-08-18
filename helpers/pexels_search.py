"""Search Pexels and download images into a Remotion project's public/ folder.

PHASE 2 helper. Needs PEXELS_API_KEY (env or .env at the edvid repo root).
Get a free key at https://www.pexels.com/api/.

Prints each downloaded file's local path and photographer credit (keep the
credits for attribution).

Usage:
    python helpers/pexels_search.py "hollywood film set" \
        --out-dir <edit>/remotion/public/pexels --count 3 --orientation portrait
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

SEARCH_URL = "https://api.pexels.com/v1/search"


def load_api_key() -> str:
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "PEXELS_API_KEY":
                    return v.strip().strip('"').strip("'")
    v = os.environ.get("PEXELS_API_KEY", "")
    if not v:
        sys.exit("PEXELS_API_KEY not found in .env or environment "
                 "(get one at https://www.pexels.com/api/)")
    return v


def search(query: str, api_key: str, count: int, orientation: str | None) -> list[dict]:
    params: dict[str, str | int] = {"query": query, "per_page": max(1, min(count, 80))}
    if orientation:
        params["orientation"] = orientation  # landscape | portrait | square
    resp = requests.get(SEARCH_URL, headers={"Authorization": api_key},
                        params=params, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Pexels returned {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("photos", [])


def download(url: str, dest: Path) -> None:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40]


def main() -> None:
    ap = argparse.ArgumentParser(description="Download Pexels images into a Remotion public/ folder")
    ap.add_argument("query", help="Search query")
    ap.add_argument("--out-dir", type=Path, required=True, help="Destination folder (e.g. remotion/public/pexels)")
    ap.add_argument("--count", type=int, default=3, help="How many images to download (default 3)")
    ap.add_argument("--orientation", choices=["landscape", "portrait", "square"], default=None)
    ap.add_argument("--size", choices=["original", "large2x", "large", "medium"], default="large2x",
                    help="Pexels rendition to download (default large2x)")
    args = ap.parse_args()

    api_key = load_api_key()
    photos = search(args.query, api_key, args.count, args.orientation)
    if not photos:
        sys.exit(f"no results for: {args.query}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(args.query)
    saved = 0
    for i, p in enumerate(photos[: args.count]):
        src = p.get("src", {})
        url = src.get(args.size) or src.get("large") or src.get("original")
        if not url:
            continue
        ext = ".jpg"
        dest = args.out_dir / f"{slug}-{i+1}{ext}"
        try:
            download(url, dest)
        except Exception as e:
            print(f"  x failed {p.get('id')}: {e}")
            continue
        saved += 1
        credit = p.get("photographer", "?")
        print(f"  + {dest}  (photo: {credit}, {p.get('url','')})")

    print(f"downloaded {saved}/{args.count} → {args.out_dir}")
    if saved:
        print("attribution: images from Pexels — keep the photographer credits above.")


if __name__ == "__main__":
    main()

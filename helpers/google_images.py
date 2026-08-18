"""Google Custom Search image lookup — for famous brands / people / things the
transcript names that Pexels can't provide (logos, celebrities, specific places).

Needs GOOGLE_API_KEY and GOOGLE_CSE_ID (a Programmable Search Engine configured
for image search over the whole web). See the skill for how to obtain them.

RIGHTS: web images are often copyrighted/trademarked. Pass --rights to filter to
reusable licenses when you can; prefer Wikimedia/official press images for
commercial use. Keep the source page URL the helper prints for attribution.

Usage:
    python helpers/google_images.py "A24 studio logo" --out-dir remotion/public/web --count 3
    python helpers/google_images.py "<query>" --out-dir <dir> --rights cc
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

URL = "https://www.googleapis.com/customsearch/v1"
RIGHTS = {
    "cc": "cc_publicdomain|cc_attribute|cc_sharealike",
    "commercial": "cc_publicdomain|cc_attribute|cc_sharealike|cc_noncommercial",
}


def load_keys() -> tuple[str, str]:
    vals: dict[str, str] = {}
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() in ("GOOGLE_API_KEY", "GOOGLE_CSE_ID"):
                        vals[k.strip()] = v.strip().strip('"').strip("'")
    key = vals.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    cse = vals.get("GOOGLE_CSE_ID") or os.environ.get("GOOGLE_CSE_ID", "")
    if not key or not cse:
        sys.exit("GOOGLE_API_KEY and/or GOOGLE_CSE_ID not found in .env or environment "
                 "(see the skill for how to get them)")
    return key, cse


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40]


def main() -> None:
    ap = argparse.ArgumentParser(description="Download images via Google Custom Search")
    ap.add_argument("query")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--rights", choices=["cc", "commercial"], default=None,
                    help="filter to reusable licenses (recommended for brands/people)")
    ap.add_argument("--img-size", default="large", help="icon|small|medium|large|xlarge|xxlarge|huge")
    args = ap.parse_args()

    key, cse = load_keys()
    params = {
        "key": key, "cx": cse, "q": args.query,
        "searchType": "image", "num": max(1, min(args.count, 10)),
        "imgSize": args.img_size, "safe": "active",
    }
    if args.rights:
        params["rights"] = RIGHTS[args.rights]

    r = requests.get(URL, params=params, timeout=60)
    if r.status_code >= 400:
        sys.exit(f"Google CSE {r.status_code}: {r.text[:300]}")
    items = r.json().get("items", [])
    if not items:
        sys.exit(f"no results for: {args.query}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(args.query)
    saved = 0
    for i, it in enumerate(items[: args.count]):
        link = it.get("link")
        if not link:
            continue
        try:
            data = requests.get(link, timeout=60, headers={"User-Agent": "Mozilla/5.0"}).content
        except Exception as e:
            print(f"  x {link[:60]}: {e}")
            continue
        dest = args.out_dir / f"{slug}-{i+1}.jpg"
        dest.write_bytes(data)
        saved += 1
        page = (it.get("image", {}) or {}).get("contextLink", "")
        print(f"  + {dest}  (source: {page})")
    print(f"downloaded {saved}/{args.count} → {args.out_dir}")
    if saved:
        print("RIGHTS: verify licensing before publishing; keep source URLs for attribution.")


if __name__ == "__main__":
    main()

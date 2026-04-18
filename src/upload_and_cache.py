"""
Upload the two input PDFs to LBDC and cache the HTML locally.

Run once. Subsequent extraction/comparison reads from cache/, no API calls.
If an input PDF changes, delete its cached .html file to re-upload.

Usage:
    python src/upload_and_cache.py

Writes:
    cache/enacted_25-26.html
    cache/executive_26-27.html
"""

from pathlib import Path

from lbdc import LBDCClient


ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
CACHE = ROOT / "cache"

JOBS = [
    ("enacted_25-26.pdf", "enacted_25-26.html"),
    ("executive_26-27.pdf", "executive_26-27.html"),
]


def main():
    CACHE.mkdir(exist_ok=True)
    client = LBDCClient()

    for pdf_name, html_name in JOBS:
        pdf_path = INPUTS / pdf_name
        html_path = CACHE / html_name

        if html_path.exists():
            print(f"[=] Cached: {html_path.name} ({html_path.stat().st_size:,} bytes) — skipping")
            continue

        if not pdf_path.exists():
            print(f"[!] Missing input: {pdf_path}")
            continue

        html = client.upload_pdf(str(pdf_path))
        html_path.write_text(html, encoding="utf-8")
        print(f"[+] Saved: {html_path}  ({len(html):,} chars)")


if __name__ == "__main__":
    main()

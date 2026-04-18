"""
Generate the tracker PDF: 26-27 exec reapprop section with tracked `Insert NNNA`
markers placed inline at each insert's anchor position.

For each insert in outputs/insert_plan.json:
  Find the upper anchor's last <p> on its exec page, tracked-insert a new
  line "Insert {label}" right after it. For inserts with no upper anchor
  (the first ones in doc order), place the label at the top of the first
  exec page.

Processing order: document order DESCENDING (bottom-up) so that an insertion
does not shift the <p> indices of subsequent inserts.

Output: outputs/tracker.pdf
"""

import json
import re
from pathlib import Path
from typing import List, Dict, Optional

from lbdc import LBDCClient, LBDCDocument


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUTPUTS = ROOT / "outputs"

LINE_NUM_RE = re.compile(r"^\s{0,3}(\d{1,3})\s+")


def line_num_of(text: str) -> Optional[int]:
    m = LINE_NUM_RE.match(text)
    return int(m.group(1)) if m else None


def find_p_index_for_line(doc: LBDCDocument, page_idx: int, target_line: int) -> int:
    """Find the <p> index within a page whose visible line number matches target_line.
    If target_line == 0, returns 0 (insert at very top). If not found, returns -1."""
    pages = doc.get_pages()
    if page_idx >= len(pages):
        return -1
    ps = pages[page_idx].find_all("p")
    if target_line == 0:
        # Insert at very top — before the first <p>. We return 0 so insert_line
        # places our label after the first page-header <p>. Good enough.
        return 0
    for i, p in enumerate(ps):
        ln = line_num_of(p.get_text())
        if ln == target_line:
            return i
    # Fallback: find last <p> with a line number <= target (in case exact match missing)
    best = -1
    for i, p in enumerate(ps):
        ln = line_num_of(p.get_text())
        if ln is not None and ln <= target_line:
            best = i
    return best


def main():
    plan = json.loads((OUTPUTS / "insert_plan.json").read_text())
    html = (CACHE / "executive_26-27.html").read_text()

    doc = LBDCDocument(html, user_color="blue")
    print(f"[*] Exec 26-27 loaded: {len(doc.get_pages())} pages, "
          f"{sum(len(doc.get_lines(p)) for p in range(len(doc.get_pages())))} total <p> tags")

    # Build the insertion operations: (page_idx, after_p_idx, label)
    ops = []
    for ins in plan:
        page_idx = ins["anchor_upper"]["exec_page_html"]
        line_num = ins["anchor_upper"]["exec_line"]
        after_idx = find_p_index_for_line(doc, page_idx, line_num)
        if after_idx < 0:
            print(f"[!] Could not locate anchor for {ins['label']} "
                  f"(page {page_idx} line {line_num}) — skipping")
            continue
        ops.append((page_idx, after_idx, ins["label"]))

    # Sort DESCENDING by (page_idx, after_idx) so insertions don't shift earlier indices
    ops.sort(key=lambda x: (x[0], x[1]), reverse=True)

    for page_idx, after_idx, label in ops:
        ok = doc.insert_line(after_idx, f"Insert {label}", page=page_idx)
        if not ok:
            print(f"[!] insert_line failed for {label}")

    print(f"[*] Applied {len(ops)} insert labels")

    client = LBDCClient()
    pdf_bytes = client.generate_pdf(doc.to_html())
    out = OUTPUTS / "tracker.pdf"
    out.write_bytes(pdf_bytes)
    print(f"[+] Saved: {out.relative_to(ROOT)}  ({len(pdf_bytes):,} bytes)")

    # Also save the edited HTML for debugging
    (OUTPUTS / "tracker.html").write_text(doc.to_html(), encoding="utf-8")
    print(f"[+] Saved: outputs/tracker.html")


if __name__ == "__main__":
    main()

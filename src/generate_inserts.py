"""
Generate insert PDFs by editing cached 25-26 HTML and hitting LBDC /generate-pdf/.

For each insert in outputs/insert_plan.json:
  1. Slice cached 25-26 HTML to the insert's source_enacted_page_range_html
  2. On those pages, strike every <p> EXCEPT:
       - survivor bill_language lines (matched by visible line_num range)
       - chapter-year headers that are "needs_chapter_header" per plan
       - fund headers that are "needs_fund_header" per plan
       - blank lines (no-op — nothing to strike)
  3. For each survivor whose new_reapprop_amount != old_reapprop_amount,
     tracked-replace the "(re. $OLD)" string with "(re. $NEW)".
  4. Tracked-insert "Insert {label}" as a new line right before the first
     survivor's first <p>.
  5. POST the resulting HTML to /generate-pdf/, save the PDF.

Usage:
  python src/generate_inserts.py              # generate all
  python src/generate_inserts.py 268A 269A    # generate just these
  python src/generate_inserts.py --pilot      # generate only the first insert
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Optional, Set

from bs4 import BeautifulSoup

from lbdc import LBDCClient, LBDCDocument


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUTPUTS = ROOT / "outputs"
INSERTS_DIR = OUTPUTS / "inserts"


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

LINE_NUM_RE = re.compile(r"^\s{0,3}(\d{1,3})\s+")
CHAPTER_YEAR_RE = re.compile(
    r"^(?:By|The\s+appropriations?\s+made\s+by)\s+chapter\s+\d+"
    r".*?of\s+the\s+laws\s+of\s+(\d{4})"
    r"(?:.*?as\s+(?:amended|added)\s+by\s+chapter\s+\d+.*?of\s+the\s+laws\s+of\s+(\d{4}))?",
    re.IGNORECASE,
)
FUND_TOP_RE = re.compile(
    r"^(General Fund|Special Revenue Funds - Federal|Special Revenue Funds - Other|"
    r"Capital Projects Funds|Enterprise Funds|Fiduciary Funds)$"
)


def slice_html_pages(full_html: str, start_html_idx: int, end_html_idx: int) -> str:
    """Extract <div class="page"> elements for the given HTML page index range (inclusive)."""
    soup = BeautifulSoup(full_html, "lxml")
    pages = soup.find_all("div", class_="page")
    selected = pages[start_html_idx:end_html_idx + 1]
    return "".join(str(p) for p in selected)


def line_num_of(p_text: str) -> Optional[int]:
    m = LINE_NUM_RE.match(p_text)
    return int(m.group(1)) if m else None


def is_chapter_year_header(p_text: str) -> Optional[int]:
    """Return the chapter year if this <p> starts a chapter-year header, else None."""
    # Strip line number prefix
    m = LINE_NUM_RE.match(p_text)
    if not m:
        return None
    after = p_text[m.end():].strip()
    cm = CHAPTER_YEAR_RE.match(after)
    return int(cm.group(1)) if cm else None


def is_fund_top(p_text: str) -> bool:
    m = LINE_NUM_RE.match(p_text)
    if not m:
        return False
    after = p_text[m.end():].strip()
    return bool(FUND_TOP_RE.match(after))


# ---------------------------------------------------------------------------
# Single-insert generator
# ---------------------------------------------------------------------------

def format_amount(n: int) -> str:
    return f"{n:,}"


def _page_lines_with_nums(page) -> List:
    """Return list of (p_tag, visible_line_num or None, stripped_text, is_blank) in order."""
    out = []
    for p in page.find_all("p"):
        text = p.get_text()
        is_blank = not text.strip()
        ln = line_num_of(text) if not is_blank else None
        out.append((p, ln, text, is_blank))
    return out


def _find_chapter_year_header_lines(page_lines: List, survivor_first_line: int) -> List[int]:
    """
    Given the lines on the page containing the survivor, walk BACKWARD from
    the survivor's first line to find the chapter-year header that introduces
    it. Return the visible line numbers of the header (1-2 lines).
    """
    # Find survivor's p_idx on this page
    surv_idx = None
    for i, (p, ln, text, blank) in enumerate(page_lines):
        if ln == survivor_first_line:
            surv_idx = i
            break
    if surv_idx is None:
        return []

    # Walk backward for the nearest chapter-year-start line
    header_start_idx = None
    for i in range(surv_idx - 1, -1, -1):
        p, ln, text, blank = page_lines[i]
        if blank:
            continue
        if ln is None:
            continue
        if is_chapter_year_header(text) is not None:
            header_start_idx = i
            break
        # If we hit another body line (a preceding reapprop's content), stop —
        # this survivor shares the chapter-year header with items before it
        # that are struck. We still need the header.
    if header_start_idx is None:
        return []

    result = [page_lines[header_start_idx][1]]  # visible line num
    # Check for continuation line if header didn't end with ":"
    first_text = page_lines[header_start_idx][2].rstrip()
    if not first_text.endswith(":"):
        for j in range(header_start_idx + 1, surv_idx):
            p, ln, text, blank = page_lines[j]
            if blank or ln is None:
                continue
            # Continuation line: add and stop
            result.append(ln)
            break
    return result


def _find_fund_header_lines(page_lines: List, survivor_first_line: int) -> List[int]:
    """Walk backward from survivor to find fund header (1-3 lines)."""
    surv_idx = None
    for i, (p, ln, text, blank) in enumerate(page_lines):
        if ln == survivor_first_line:
            surv_idx = i
            break
    if surv_idx is None:
        return []

    fund_lines: List[int] = []
    # Walk backward until we find a fund-top line (General Fund / Special Revenue Funds - *)
    # Collect it + any preceding related lines (actually we want the TOP line and lines
    # AFTER it up to the survivor)
    top_idx = None
    for i in range(surv_idx - 1, -1, -1):
        p, ln, text, blank = page_lines[i]
        if blank or ln is None:
            continue
        if is_fund_top(text):
            top_idx = i
            break
    if top_idx is None:
        return []

    # From top_idx forward, collect numbered non-chapter-year lines until we hit
    # a chapter-year or body line (or 3 lines max)
    for i in range(top_idx, surv_idx):
        p, ln, text, blank = page_lines[i]
        if blank or ln is None:
            continue
        if is_chapter_year_header(text) is not None:
            break
        fund_lines.append(ln)
        if len(fund_lines) >= 3:
            break
    return fund_lines


def apply_insert_edits(doc: LBDCDocument, insert: dict) -> None:
    """Apply strikes + amount replacements + label insertion to the sliced LBDCDocument."""
    source_html_start = insert["source_enacted_page_range_html"][0]

    # Compute keep_lines: set of (page_offset, visible_line_num) to keep unstruck
    keep_lines: Set = set()
    pages = doc.get_pages()
    pages_lines_cache = [_page_lines_with_nums(page) for page in pages]

    for s in insert["survivors"]:
        # Survivor's own body lines
        first_pg_off = s["first_page"] - source_html_start
        last_pg_off = s["last_page"] - source_html_start
        if first_pg_off == last_pg_off:
            for ln in range(s["first_line"], s["last_line"] + 1):
                keep_lines.add((first_pg_off, ln))
        else:
            # Survivor spans pages — keep from first_line to end of first page,
            # then 1..last_line on last page
            # Find max line_num on first page
            max_first = max((ln for _, ln, _, _ in pages_lines_cache[first_pg_off] if ln), default=0)
            for ln in range(s["first_line"], max_first + 1):
                keep_lines.add((first_pg_off, ln))
            for ln in range(1, s["last_line"] + 1):
                keep_lines.add((last_pg_off, ln))

        # If needs_chapter_header, keep the chapter-year lines preceding this survivor
        if s["needs_chapter_header"]:
            chyr_lines = _find_chapter_year_header_lines(
                pages_lines_cache[first_pg_off], s["first_line"]
            )
            for ln in chyr_lines:
                keep_lines.add((first_pg_off, ln))

        # If needs_fund_header, keep the fund header lines preceding this survivor
        if s["needs_fund_header"]:
            fund_lines = _find_fund_header_lines(
                pages_lines_cache[first_pg_off], s["first_line"]
            )
            for ln in fund_lines:
                keep_lines.add((first_pg_off, ln))

    # Walk pages and strike everything not in keep_lines — except:
    # - blank lines (nothing to strike)
    # - page-header lines (ln is None): left AS-IS per user's rule — the page
    #   number / agency / bill title at the top of each page are ignored, not
    #   struck.
    for pg_off, page_lines in enumerate(pages_lines_cache):
        for p, ln, text, blank in page_lines:
            if blank:
                continue
            if ln is None:
                continue  # page header — leave alone
            if (pg_off, ln) not in keep_lines:
                _strike_p_in_place(p, doc)

    # (re. $X) handling — two paths depending on source:
    #
    #   REAPPROP-sourced survivor: the source line already contains
    #     `(re. $OLD)`. Replace with `(re. $NEW)` if the amount changed.
    #
    #   APPROPRIATION-sourced survivor: the source line has NO `(re. $X)`
    #     suffix — it ends with the appropriation amount. We append
    #     ` ... (re. $NEW)` as a tracked <ins> at the END of the survivor's
    #     last body line, turning the approp into a reapprop with the new
    #     SFS-rounded amount.
    for s in insert["survivors"]:
        new_amt = int(s["new_reapprop_amount"])
        source = s.get("source", "reapprop")
        page_off = s["last_page"] - source_html_start
        if source == "appropriation":
            # Find the <p> with the survivor's last visible line number
            last_line = s["last_line"]
            target_p_idx = None
            for i, (p, ln, _t, blank) in enumerate(pages_lines_cache[page_off]):
                if ln == last_line:
                    target_p_idx = i
                    break
            if target_p_idx is not None:
                doc.append_to_line_tracked(
                    target_p_idx,
                    f" ... (re. ${format_amount(new_amt)})",
                    page=page_off,
                )
        else:
            old_amt = int(s["old_reapprop_amount"])
            if old_amt == new_amt:
                continue
            old_txt = f"(re. ${format_amount(old_amt)})"
            new_txt = f"(re. ${format_amount(new_amt)})"
            ok = doc.replace_text_tracked(old_txt, new_txt, page=page_off)
            if not ok:
                # Try variant without $
                alt_old = f"(re. {format_amount(old_amt)})"
                doc.replace_text_tracked(alt_old, new_txt, page=page_off)

    # Insert label right BEFORE the earliest kept line in the insert.
    # That's either the first survivor's first line, or (if needs_chapter_header
    # or needs_fund_header) the first kept structural header.
    first_surv = insert["survivors"][0]
    first_pg_off = first_surv["first_page"] - source_html_start
    first_surv_line = first_surv["first_line"]

    # Earliest kept visible line on first survivor's page
    same_page_keeps = sorted(ln for (pg, ln) in keep_lines if pg == first_pg_off)
    target_first_line = same_page_keeps[0] if same_page_keeps else first_surv_line

    pages = doc.get_pages()
    target_page = pages[first_pg_off]
    ps = target_page.find_all("p")
    target_before_idx = None
    for i, p in enumerate(ps):
        ln = line_num_of(p.get_text())
        if ln == target_first_line:
            target_before_idx = i - 1
            break
    # Skip back past any blank <p> so the label sits directly against the kept block
    while target_before_idx is not None and target_before_idx >= 0:
        prev_text = ps[target_before_idx].get_text()
        if prev_text.strip():
            break
        target_before_idx -= 1
    if target_before_idx is not None and target_before_idx >= 0:
        doc.insert_line(target_before_idx, f"Insert {insert['label']}", page=first_pg_off)


def _strike_p_in_place(p_tag, doc: LBDCDocument) -> None:
    """Strike the entire content of a <p> tag by wrapping its text in <del>."""
    text = p_tag.get_text()
    if not text.strip():
        return
    del_tag = doc._make_del(text)
    p_tag.clear()
    p_tag.append(del_tag)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_one(client: LBDCClient, plan: List[dict], label: str, full_html: str) -> Optional[Path]:
    ins = next((x for x in plan if x.get("label") == label), None)
    if ins is None:
        print(f"[!] Insert {label!r} not in plan")
        return None
    start, end = ins["source_enacted_page_range_html"]
    sliced = slice_html_pages(full_html, start, end)
    doc = LBDCDocument(sliced, user_color="blue")
    print(f"\n[*] {ins['label']}  src pages HTML {start}..{end}  PDF {ins['source_enacted_page_range_pdf'][0]}..{ins['source_enacted_page_range_pdf'][1]}  survivors={len(ins['survivors'])}")
    apply_insert_edits(doc, ins)
    pdf_bytes = client.generate_pdf(doc.to_html())
    INSERTS_DIR.mkdir(exist_ok=True, parents=True)
    out = INSERTS_DIR / f"Insert_{ins['label']}.pdf"
    out.write_bytes(pdf_bytes)
    print(f"[+] Saved: {out.relative_to(ROOT)}  ({len(pdf_bytes):,} bytes)")
    # Also save the edited HTML for debugging
    html_out = INSERTS_DIR / f"Insert_{ins['label']}.html"
    html_out.write_text(doc.to_html(), encoding="utf-8")
    return out


def main():
    args = sys.argv[1:]
    plan = json.loads((OUTPUTS / "insert_plan.json").read_text())

    # Load both enacted sources; each insert picks based on survivors[0].source.
    html_by_source = {
        "reapprop": (CACHE / "enacted_25-26.html").read_text(),
    }
    approps_path = CACHE / "enacted_25-26_approps.html"
    if approps_path.exists():
        html_by_source["appropriation"] = approps_path.read_text()

    if "--pilot" in args:
        labels = [plan[0]["label"]]
    elif args:
        labels = args
    else:
        labels = [ins["label"] for ins in plan]

    client = LBDCClient()
    for label in labels:
        try:
            ins = next((x for x in plan if x.get("label") == label), None)
            if ins is None:
                print(f"[!] Insert {label!r} not in plan")
                continue
            source = ins["survivors"][0].get("source", "reapprop")
            full_html = html_by_source.get(source)
            if full_html is None:
                print(f"[!] No cached HTML for source={source!r}; skipping {label}")
                continue
            run_one(client, plan, label, full_html)
        except Exception as e:
            print(f"[!] {label}: {e}")


if __name__ == "__main__":
    main()

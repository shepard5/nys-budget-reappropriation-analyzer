"""
Extract 25-26 appropriations (pp 264-314 of the ATL bill) — the "new spending"
section. Any item here is implicitly a chapter year 2025 item.

Key differences from src/extract.py (which handles reapprops):
  - Terminator: line ending with `.... <amount>` only; NO `(re. $X)` suffix.
  - Chapter year: implicit 2025, no "By chapter 53, ... of the laws of YYYY:"
    headers within the appropriations section (they come via the chyr 2025
    reapprops in the 26-27 bill).
  - Program header is on ONE line WITH a trailing amount:
    "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM ..... 229,925,000"
  - Omit lines (false-positive dots-amount patterns):
      * Agency header schedule table (top of each agency, before the first
        PROGRAM line, with "APPROPRIATIONS  REAPPROPRIATIONS" columns)
      * Program account subtotals: "Program account subtotal .......... X"
      * Any "All Funds", "----------------", "================" lines
  - Some items appear WITHOUT a (XXXXX) approp_id (bare dollar amounts —
    e.g., Rockland Independent Living Center $50K). Captured with approp_id=""

Output: outputs/enacted_approps.csv — one row per appropriation.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup

# Reuse the line-walker from extract.py + shared regexes from patterns.py
import sys
sys.path.insert(0, str(Path(__file__).parent))
from extract import walk_html, Line
from patterns import (
    LINE_NUM_RE,
    FUND_TOP_RE,
    PROGRAM_WITH_AMOUNT_RE,
    PROGRAM_PLAIN_RE,
    PROGRAM_CONT_RE,
    PROGRAM_CAPS_LINE_RE,
    APPROP_ID_RE,
    TERM_AMOUNT_RE,
    SCHEDULE_ROW_RE,
    SKIP_LINE_RE,
    BODY_LINE_START_RE,
    parse_int_amount,
    looks_like_agency_header,
    section_of_title,
)


ROOT = Path(__file__).resolve().parent.parent
APPROPS_CHAPTER_YEAR = 2025


# Indent of body content (after the line number + its trailing whitespace).
# Used to detect wrapped fund-name lines, which are indented more than the
# line that starts them.
_LINE_NUM_WITH_SEP_RE = re.compile(r"^(\s{0,3}\d{1,3}\s+)")


def _body_indent(raw: str) -> int:
    m = _LINE_NUM_WITH_SEP_RE.match(raw)
    return len(m.group(1)) if m else 0


@dataclass
class Appropriation:
    agency: str = ""
    program: str = ""
    fund: str = ""
    chapter_year: int = APPROPS_CHAPTER_YEAR
    amending_year: int = 0
    approp_id: str = ""
    approp_amount: int = 0
    bill_language: str = ""
    first_page_idx: int = 0
    first_line_num: int = 0
    last_page_idx: int = 0
    last_line_num: int = 0
    fund_page_idx: int = -1


# Structural regexes imported from patterns.py above.


@dataclass
class ExtractApprops:
    approps: List[Appropriation] = field(default_factory=list)
    n_lines: int = 0
    n_body: int = 0
    warnings: List[str] = field(default_factory=list)


def extract(html: str) -> ExtractApprops:
    lines = walk_html(html)
    result = ExtractApprops(
        approps=[],
        n_lines=len(lines),
        n_body=sum(1 for L in lines if L.line_num is not None),
    )

    agency: str = ""
    program: str = ""
    fund_parts: List[str] = []
    fund_page_idx: int = -1
    buf_start_idx: Optional[int] = None
    # Section — "appropriation" or "reapprop". We only emit appropriations
    # records in the "appropriation" section of each agency.
    section: Optional[str] = None
    seen_first_program_on_page = False  # legacy flag, no longer relied on

    i = 0
    while i < len(lines):
        L = lines[i]
        if L.is_blank:
            i += 1
            continue
        if L.line_num is None:
            t_hdr = L.text.strip()
            sec = section_of_title(t_hdr)
            if sec is not None:
                if sec != section:
                    section = sec
                    program = ""
                    fund_parts = []
                    fund_page_idx = -1
                    buf_start_idx = None
            elif t_hdr and looks_like_agency_header(t_hdr):
                new_agency = re.sub(r"\s+", " ", t_hdr)
                if new_agency != agency:
                    agency = new_agency
                    program = ""
                    fund_parts = []
                    fund_page_idx = -1
                    buf_start_idx = None
                    section = None
            i += 1
            continue

        # Only emit appropriations in the appropriation section. In reapprop
        # pages, skip body lines so the terminator regex doesn't match `(re. $X)`
        # lines as false-positive appropriations.
        if section != "appropriation":
            i += 1
            continue

        t = L.text.strip()

        # Skip schedule/subtotal lines entirely
        if SCHEDULE_ROW_RE.search(t) or SKIP_LINE_RE.match(t):
            # If in the middle of buffering body text, the buffer is ruined —
            # reset. (Shouldn't happen — these lines only appear outside items.)
            buf_start_idx = None
            i += 1
            continue

        # Program header (with amount) — signal that real content follows.
        # Also handle continuation form: previous line ends in all-caps words
        # (no PROGRAM), current line is "PROGRAM ...... <amount>"
        m = PROGRAM_WITH_AMOUNT_RE.match(t) or PROGRAM_PLAIN_RE.match(t)
        if m:
            program = m.group(1)
            fund_parts = []
            fund_page_idx = -1
            buf_start_idx = None
            i += 1
            continue
        if PROGRAM_CONT_RE.match(t):
            # Back up to the previous non-blank numbered line
            k = i - 1
            while k >= 0:
                Lk = lines[k]
                if Lk.is_blank or Lk.line_num is None:
                    k -= 1
                    continue
                if PROGRAM_CAPS_LINE_RE.match(Lk.text.strip()):
                    # Normalize whitespace — source PDF has double spaces
                    raw_name = Lk.text.strip() + " PROGRAM"
                    program = re.sub(r"\s+", " ", raw_name)
                    fund_parts = []
                    fund_page_idx = -1
                    buf_start_idx = None
                break
            i += 1
            continue

        # Fund top-level
        if FUND_TOP_RE.match(t):
            # Normalize whitespace when storing fund parts — PDF justification
            # in the approps section sometimes produces double-spaces which
            # would otherwise break fund-key equality against the reapprop
            # section's single-spaced strings.
            fund_parts = [re.sub(r"\s+", " ", t)]
            fund_page_idx = L.page_idx
            # Track the body-indent of the most recent fund-part line so we
            # can detect wraps: a subsequent line with HIGHER body indent is
            # a continuation of the previous part, not a new part. (Used
            # when a long fund name like "New York State Local Government
            # Records Management Improvement Fund" wraps across two lines.)
            last_part_indent = _body_indent(L.raw_text)
            j = i + 1
            while j < len(lines) and len(fund_parts) < 3:
                Lj = lines[j]
                if Lj.is_blank or Lj.line_num is None:
                    j += 1
                    continue
                tj = Lj.text.strip()
                if PROGRAM_WITH_AMOUNT_RE.match(tj) or PROGRAM_PLAIN_RE.match(tj):
                    break
                if FUND_TOP_RE.match(tj):
                    break
                # Body line signal
                if BODY_LINE_START_RE.match(tj):
                    break
                if TERM_AMOUNT_RE.search(tj):
                    break
                # Caps-line continuation of a multi-line program name — stop,
                # this line belongs to a program header, not the fund
                if PROGRAM_CAPS_LINE_RE.match(tj):
                    break
                tj_indent = _body_indent(Lj.raw_text)
                tj_norm = re.sub(r"\s+", " ", tj)
                if tj_indent > last_part_indent:
                    # Wrap — append to the previous fund part instead of
                    # starting a new one.
                    fund_parts[-1] = f"{fund_parts[-1]} {tj_norm}"
                else:
                    fund_parts.append(tj_norm)
                    last_part_indent = tj_indent
                j += 1
            buf_start_idx = None
            i = j
            continue

        # Body line — start or continue buffering an appropriation
        if not program or not fund_parts:
            # Still in agency header area before any program is declared, OR
            # between a program header and first fund. Skip.
            i += 1
            continue

        if buf_start_idx is None:
            buf_start_idx = i

        # Terminator: dots + amount at end of line
        tm = TERM_AMOUNT_RE.search(t)
        if tm:
            approp_amount = parse_int_amount(tm.group(1))

            # Approp ID: search buffered lines
            approp_id = ""
            for k in range(buf_start_idx, i + 1):
                Lk = lines[k]
                if Lk.is_blank or Lk.line_num is None:
                    continue
                am = APPROP_ID_RE.search(Lk.text)
                if am:
                    approp_id = am.group(1)
                    break

            # Bill language
            bill_lines: List[str] = []
            for k in range(buf_start_idx, i + 1):
                Lk = lines[k]
                if Lk.is_blank or Lk.line_num is None:
                    continue
                bill_lines.append(f"{Lk.line_num:>2} {Lk.text}")
            bill_language = "\n".join(bill_lines)

            ap = Appropriation(
                agency=agency,
                program=program,
                fund="; ".join(fund_parts),
                chapter_year=APPROPS_CHAPTER_YEAR,
                amending_year=0,
                approp_id=approp_id,
                approp_amount=approp_amount,
                bill_language=bill_language,
                first_page_idx=lines[buf_start_idx].page_idx,
                first_line_num=lines[buf_start_idx].line_num or 0,
                last_page_idx=L.page_idx,
                last_line_num=L.line_num or 0,
                fund_page_idx=fund_page_idx,
            )
            result.approps.append(ap)
            buf_start_idx = None

        i += 1

    return result


def main():
    import json
    cache = ROOT / "cache"
    outputs = ROOT / "outputs"
    outputs.mkdir(exist_ok=True)

    # Prefer the appropriations-only slice (Education scope) if present;
    # otherwise fall back to the full enacted HTML (full-ATL scope), in which
    # case the extractor filters internally to approps pages.
    approps_only = cache / "enacted_25-26_approps.html"
    full_bill = cache / "enacted_25-26.html"
    if approps_only.exists():
        html_path = approps_only
    elif full_bill.exists():
        html_path = full_bill
    else:
        print(f"[!] Missing {approps_only} and {full_bill} — "
              f"run upload_and_cache.py first")
        return

    html = html_path.read_text()
    r = extract(html)
    print(f"\n{'='*72}")
    print(f"{html_path.name}")
    print(f"{'='*72}")
    print(f"  lines total : {r.n_lines}")
    print(f"  body lines  : {r.n_body}")
    print(f"  approps     : {len(r.approps)}")

    try:
        import pandas as pd
        rows = [{
            "agency": a.agency,
            "program": a.program,
            "fund": a.fund,
            "chapter_year": a.chapter_year,
            "amending_year": a.amending_year,
            "approp_id": a.approp_id,
            "approp_amount": a.approp_amount,
            # For an appropriation, the reapprop_amount equals the full amount —
            # the item has not been drawn down yet. Downstream compare treats
            # it as "continued" if exec chyr-2025 reapprop has same amount,
            # "modified" if exec has less.
            "reapprop_amount": a.approp_amount,
            "first_page": a.first_page_idx,
            "first_line": a.first_line_num,
            "last_page": a.last_page_idx,
            "last_line": a.last_line_num,
            "bill_language": a.bill_language,
            "source": "appropriation",
            "chyr_page": -1,  # appropriations section has no chyr header
            "fund_page": a.fund_page_idx,
        } for a in r.approps]
        df = pd.DataFrame(rows)
        df.to_csv(outputs / "enacted_approps.csv", index=False)
        print(f"  wrote       : outputs/enacted_approps.csv")

        # Distribution summary
        print(f"\n  Items by (program, fund):")
        for (prog, fund), n in df.groupby(["program", "fund"]).size().items():
            print(f"    {n:>3}  {prog[:30]:30s}  {fund[:60]}")
        n_nanid = (df.approp_id == "").sum()
        print(f"\n  Items without approp_id: {n_nanid}")
    except ImportError:
        pass


if __name__ == "__main__":
    main()

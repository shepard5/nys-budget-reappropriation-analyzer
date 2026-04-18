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

# Reuse helpers from the reapprop extractor.
import sys
sys.path.insert(0, str(Path(__file__).parent))
from extract import walk_html, Line, LINE_NUM_RE, FUND_TOP_RE, parse_int_amount


ROOT = Path(__file__).resolve().parent.parent
APPROPS_CHAPTER_YEAR = 2025


@dataclass
class Appropriation:
    program: str
    fund: str
    chapter_year: int = APPROPS_CHAPTER_YEAR
    amending_year: int = 0
    approp_id: str = ""
    approp_amount: int = 0
    bill_language: str = ""
    first_page_idx: int = 0
    first_line_num: int = 0
    last_page_idx: int = 0
    last_line_num: int = 0


# Program header with trailing amount, all on one line
PROGRAM_WITH_AMOUNT_RE = re.compile(
    r"^([A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM)\s*\.{2,}\s*\$?[\d,]+\s*$"
)
PROGRAM_PLAIN_RE = re.compile(
    r"^([A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM)\s*$"
)
# Continuation-line variant: previous line ends in all-caps words (no PROGRAM),
# current line starts with "PROGRAM ....... <amount>"
PROGRAM_CONT_RE = re.compile(
    r"^PROGRAM\s*\.{2,}\s*\$?[\d,]+\s*$"
)
PROGRAM_CAPS_LINE_RE = re.compile(
    r"^[A-Z][A-Z0-9 ,&'\-/]*[A-Z]\s*$"
)

# Approp ID: `(XXXXX)` — 5 digits in parens
APPROP_ID_RE = re.compile(r"\((\d{5})\)")

# Terminator anchors. On a body line, the appropriation ends when a
# dollar-amount appears with no further text. Patterns:
#   "... (21713) ............................................... 72,100,000"
#   "... (56145) ................................. 500,000"
#   "BRIDGES ......................................... 50,000"          ← no id
#   "(23411) ... 1,843,000"                                              ← inline
# Capture the final number preceded by dots.
TERM_AMOUNT_RE = re.compile(r"\.{2,}\s*\$?([\d,]+)\s*$")

# Schedule-table rows have TWO dollar amounts (APPROPRIATIONS + REAPPROPRIATIONS
# columns), preceded by dots. Skip them.
SCHEDULE_ROW_RE = re.compile(
    r"\.{2,}\s*\$?[\d,]+\s+\$?[\d,]+\s*$"
)

# Subtotal / separator lines — skip entirely.
SKIP_LINE_RE = re.compile(
    r"^(Program account subtotal\b|All Funds\b|[-=]{5,}$|SCHEDULE\b)"
)


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

    program: str = ""
    fund_parts: List[str] = []
    buf_start_idx: Optional[int] = None
    seen_first_program_on_page = False  # resets per agency first-page; used to skip header schedule

    i = 0
    while i < len(lines):
        L = lines[i]
        if L.is_blank:
            i += 1
            continue
        if L.line_num is None:
            # Unnumbered headers (page number, agency name, bill title) — skip
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
                    buf_start_idx = None
                break
            i += 1
            continue

        # Fund top-level
        if FUND_TOP_RE.match(t):
            fund_parts = [t]
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
                if re.match(
                    r"^(For\b|By\b|The\s+appropriations?\s+made\s+by\b|"
                    r"Notwithstanding\b|Provided\b|Of\s+the\b|Aid\s+to\b)",
                    tj, re.IGNORECASE,
                ):
                    break
                if TERM_AMOUNT_RE.search(tj):
                    break
                # Caps-line continuation of a multi-line program name — stop,
                # this line belongs to a program header, not the fund
                if PROGRAM_CAPS_LINE_RE.match(tj):
                    break
                fund_parts.append(tj)
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

    html_path = cache / "enacted_25-26_approps.html"
    if not html_path.exists():
        print(f"[!] Missing {html_path} — run upload_and_cache.py first")
        return

    html = html_path.read_text()
    r = extract(html)
    print(f"\n{'='*72}")
    print(f"enacted_25-26_approps.html")
    print(f"{'='*72}")
    print(f"  lines total : {r.n_lines}")
    print(f"  body lines  : {r.n_body}")
    print(f"  approps     : {len(r.approps)}")

    try:
        import pandas as pd
        rows = [{
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

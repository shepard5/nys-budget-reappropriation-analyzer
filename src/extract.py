"""
Reappropriation extractor — parses LBDC editor HTML into structured records.

Design:
  1. Walk <div class="page"> → <p> line elements, capturing (page_idx, p_idx,
     visible_line_num, text, is_blank).
  2. Track structural context as we walk: program, fund, chapter_year, amending_year.
  3. Reapprop terminator = line containing `(re. $<amount>)` or `(re. <amount>)`.
     When found, gather bill_language back to the previous terminator/marker,
     extract approp_id and original amount, emit a Reappropriation record.

Ground-truth validation: per-(program, fund) reapprop counts in
BUDGET BREAKDOWN.xlsx Sheet1. Discrepancies indicate extractor bugs.

Omit (known false positives for `dots + amount` pattern, though none of them
contain `(re.` so our primary anchor already handles this):
  - Agency header schedule ("General Fund .......... <big number>")
  - Program header subtotals ("ADULT CAREER ... ..... 229,925,000")
  - "Program account subtotal .................. 98,596,000"
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Line:
    """One <p> element. line_num is the visible numbering (None if unnumbered)."""
    page_idx: int            # 0-based page index in HTML
    p_idx: int               # 0-based <p> index within page
    global_p_idx: int        # monotonic across all pages
    line_num: Optional[int]  # visible line number (None for headers/blanks)
    text: str                # text with the line-number prefix stripped
    raw_text: str            # text as-is (with leading line num)
    is_blank: bool


@dataclass
class Reappropriation:
    # Structural context
    program: str
    fund: str                # joined with "; "  e.g. "General Fund; Local Assistance Account - 10000"
    chapter_year: int
    amending_year: int       # 0 if not amended

    # Identity
    approp_id: str           # "21713" (may be empty if extractor couldn't find one)
    approp_amount: int       # original appropriation amount
    reapprop_amount: int     # (re. $X) amount

    # Payload
    bill_language: str       # verbatim text joined across lines
    first_page_idx: int      # 0-based page where this reapprop starts
    first_line_num: int      # visible line number where it starts
    last_page_idx: int
    last_line_num: int
    global_p_start: int
    global_p_end: int

    # Uniqueness key
    def composite_key(self) -> Tuple[str, str, str, int, int]:
        return (self.program, self.fund, self.approp_id, self.chapter_year, self.amending_year)


@dataclass
class ExtractResult:
    reapprops: List[Reappropriation]
    # Diagnostics
    n_lines: int
    n_blank: int
    n_header: int
    n_body: int
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# LINE WALKING
# =============================================================================

LINE_NUM_RE = re.compile(r"^\s{0,3}(\d{1,3})\s+(.*)$")
BLANK_STYLE_RE = re.compile(r"min-height")


def walk_html(html: str) -> List[Line]:
    soup = BeautifulSoup(html, "lxml")
    pages = soup.find_all("div", class_="page")
    lines: List[Line] = []
    g = 0
    for page_idx, page in enumerate(pages):
        for p_idx, p in enumerate(page.find_all("p")):
            raw = p.get_text()
            text_stripped = raw.rstrip()
            is_blank = (not text_stripped.strip()) or bool(
                BLANK_STYLE_RE.search(p.get("style", ""))
            ) and not text_stripped.strip()

            # Parse leading line number if present
            m = LINE_NUM_RE.match(raw)
            if m and not is_blank:
                line_num = int(m.group(1))
                text = m.group(2).rstrip()
            else:
                line_num = None
                text = text_stripped.lstrip()

            lines.append(Line(
                page_idx=page_idx,
                p_idx=p_idx,
                global_p_idx=g,
                line_num=line_num,
                text=text,
                raw_text=raw,
                is_blank=is_blank,
            ))
            g += 1
    return lines


# =============================================================================
# STRUCTURAL MARKERS
# =============================================================================

# "PROGRAM" all-caps line — the last word must be PROGRAM
PROGRAM_RE = re.compile(r"^[A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM$")

# Top-level fund header — first line of a fund block
FUND_TOP_RE = re.compile(
    r"^(General Fund|Special Revenue Funds - Federal|Special Revenue Funds - Other|"
    r"Capital Projects Funds|Enterprise Funds|Fiduciary Funds)$"
)

# Chapter-year header — two forms:
#   Form A: "By chapter N, section M, [of part X,] of the laws of YYYY[, as {amended|added} by chapter ... of the laws of ZZZZ]:"
#   Form B: "The appropriation[s] made by chapter N, section M, of the laws of YYYY,"  (followed on next line
#           by "[is|are] hereby amended and reappropriated to read:")
# Chapter/section numbers vary. We capture base year + optional amending year.
CHAPTER_YEAR_RE = re.compile(
    r"^(?:By|The\s+appropriations?\s+made\s+by)\s+chapter\s+\d+"
    r".*?of\s+the\s+laws\s+of\s+(\d{4})"
    r"(?:.*?as\s+(?:amended|added)\s+by\s+chapter\s+\d+.*?of\s+the\s+laws\s+of\s+(\d{4}))?",
    re.IGNORECASE,
)

# Reapprop terminator: `(re. $<amount>)` or `(re. <amount>)`
RE_AMOUNT_RE = re.compile(r"\(re\.\s*\$?([\d,]+)\s*\)")

# Approp ID: `(XXXXX)` — 5 digits in parens
APPROP_ID_RE = re.compile(r"\((\d{5})\)")

# Original amount pattern — dots then number (e.g., "..... 54,000,000")
# We want the LAST such match on the terminator line before "(re.", and if not
# there, check the previous non-blank line.
DOTS_AMOUNT_RE = re.compile(r"\.{2,}\s*\$?([\d,]+)")


def parse_int_amount(s: str) -> int:
    return int(s.replace(",", "").replace("$", ""))


# =============================================================================
# MAIN EXTRACTOR
# =============================================================================

def extract(html: str) -> ExtractResult:
    lines = walk_html(html)
    result = ExtractResult(
        reapprops=[],
        n_lines=len(lines),
        n_blank=sum(1 for L in lines if L.is_blank),
        n_header=sum(1 for L in lines if L.line_num is None and not L.is_blank),
        n_body=sum(1 for L in lines if L.line_num is not None),
    )

    # Structural state
    program: str = ""
    fund_parts: List[str] = []       # accumulated 1-3 lines that form the fund
    chapter_year: int = 0
    amending_year: int = 0

    # Buffer for the current reapprop being accumulated
    buf_start_idx: Optional[int] = None

    # Index walk
    i = 0
    while i < len(lines):
        L = lines[i]

        # Skip blanks — they're separators, keep buffer intact
        if L.is_blank:
            i += 1
            continue

        # Skip unnumbered headers (page number, agency, bill title)
        if L.line_num is None:
            i += 1
            continue

        t = L.text.strip()

        # --- Structural detection ---

        # Program header (all caps ending in PROGRAM)
        if PROGRAM_RE.match(t):
            program = t
            fund_parts = []
            chapter_year = 0
            amending_year = 0
            buf_start_idx = None
            i += 1
            continue

        # Top-level fund line — start a new fund block.
        # Sub-fund lines follow immediately (possibly across a page break),
        # separated from a chapter-year header by a blank.
        if FUND_TOP_RE.match(t):
            fund_parts = [t]
            j = i + 1
            while j < len(lines) and len(fund_parts) < 3:
                Lj = lines[j]
                # Blanks and page-header lines (line_num=None) do NOT terminate
                # the fund-header run — sub-fund parts legitimately span pages.
                if Lj.is_blank or Lj.line_num is None:
                    j += 1
                    continue
                tj = Lj.text.strip()
                # Structural terminators
                if CHAPTER_YEAR_RE.match(tj):
                    break
                if PROGRAM_RE.match(tj) or FUND_TOP_RE.match(tj):
                    break
                # Body content (reapprop text) — stop collecting
                if re.match(r"^(For\b|By\b|The\s+appropriations?\s+made\s+by\b)", tj, re.IGNORECASE):
                    break
                if RE_AMOUNT_RE.search(tj):
                    break
                # Otherwise, it's a continuation of the fund header
                fund_parts.append(tj)
                j += 1
            chapter_year = 0
            amending_year = 0
            buf_start_idx = None
            i = j
            continue

        # Chapter year header (may span 2 lines if ends without ":")
        m = CHAPTER_YEAR_RE.match(t)
        if m:
            chapter_year = int(m.group(1))
            amending_year = int(m.group(2)) if m.group(2) else 0
            buf_start_idx = None
            # If this header line didn't terminate with ":", look for a
            # continuation line (e.g. "section 1, of the laws of 2024:" or
            # "hereby amended and reappropriated to read:") and consume it too.
            consumed_to = i
            if not t.rstrip().endswith(":"):
                j = i + 1
                while j < len(lines):
                    Lj = lines[j]
                    if Lj.is_blank or Lj.line_num is None:
                        j += 1
                        continue
                    tj = Lj.text.strip()
                    is_continuation = (
                        tj.endswith(":") or
                        bool(re.match(
                            r"^(section\s+\d+|part\s+[A-Z]+|hereby|is\s+hereby|"
                            r"are\s+hereby|and\s+)", tj, re.IGNORECASE))
                    )
                    if is_continuation:
                        if amending_year == 0:
                            am = re.search(r"of\s+the\s+laws\s+of\s+(\d{4})", tj)
                            if am:
                                amending_year = int(am.group(1))
                        consumed_to = j
                    break
            i = consumed_to + 1
            continue

        # --- Body line: part of a reapprop ---
        if buf_start_idx is None:
            buf_start_idx = i

        # Check if this line is a reapprop terminator
        re_m = RE_AMOUNT_RE.search(t)
        if re_m and program and fund_parts:
            # chapter_year can be 0 (rare — a reapprop with no "By chapter..."
            # header between the fund change and the item; seen once, for
            # Teen Health Education pg 450). Still emit; downstream handles.
            if chapter_year == 0:
                result.warnings.append(
                    f"reapprop with chapter_year=0 at page {L.page_idx} line {L.line_num}: "
                    f"{t[:80]}"
                )
            reapprop_amount = parse_int_amount(re_m.group(1))

            # Find original approp amount. Three formats to handle:
            #   A) inline: "... (21771) ... 922,000 .... (re. $922,000)"
            #      → same line before "(re.", dots-then-number pattern
            #   B) two-line: line ends "(21713) .........." then next line starts
            #      "54,000,000 ........ (re. $X)" — before_re has number-then-dots
            #   C) multi-line split: amount on an even earlier body line
            before_re = t[:re_m.start()]
            approp_amount = 0

            # A) dots-then-number (prefer last, skips noise before it)
            dots_matches = list(DOTS_AMOUNT_RE.finditer(before_re))
            if dots_matches:
                approp_amount = parse_int_amount(dots_matches[-1].group(1))

            # B) number-then-dots (leading-amount terminator-line format)
            if approp_amount == 0:
                num_dots = re.search(r"\$?([\d,]+)\s+\.{2,}", before_re)
                if num_dots:
                    approp_amount = parse_int_amount(num_dots.group(1))

            # C) look back in prior buffered lines
            if approp_amount == 0:
                k = i - 1
                while k >= buf_start_idx:
                    Lk = lines[k]
                    if Lk.is_blank or Lk.line_num is None:
                        k -= 1
                        continue
                    prev_t = Lk.text
                    dm = list(DOTS_AMOUNT_RE.finditer(prev_t))
                    if dm:
                        approp_amount = parse_int_amount(dm[-1].group(1))
                        break
                    num_dots = re.search(r"\$?([\d,]+)\s+\.{2,}", prev_t)
                    if num_dots:
                        approp_amount = parse_int_amount(num_dots.group(1))
                        break
                    k -= 1

            # Find approp ID — search buffered lines for "(XXXXX)"
            approp_id = ""
            for k in range(buf_start_idx, i + 1):
                Lk = lines[k]
                if Lk.is_blank or Lk.line_num is None:
                    continue
                am = APPROP_ID_RE.search(Lk.text)
                if am:
                    approp_id = am.group(1)
                    break

            # Build bill_language by joining buffered numbered lines
            bill_lines: List[str] = []
            for k in range(buf_start_idx, i + 1):
                Lk = lines[k]
                if Lk.is_blank or Lk.line_num is None:
                    continue
                bill_lines.append(f"{Lk.line_num:>2} {Lk.text}")
            bill_language = "\n".join(bill_lines)

            # Emit
            rr = Reappropriation(
                program=program,
                fund="; ".join(fund_parts),
                chapter_year=chapter_year,
                amending_year=amending_year,
                approp_id=approp_id,
                approp_amount=approp_amount,
                reapprop_amount=reapprop_amount,
                bill_language=bill_language,
                first_page_idx=lines[buf_start_idx].page_idx,
                first_line_num=lines[buf_start_idx].line_num or 0,
                last_page_idx=L.page_idx,
                last_line_num=L.line_num or 0,
                global_p_start=lines[buf_start_idx].global_p_idx,
                global_p_end=L.global_p_idx,
            )
            result.reapprops.append(rr)

            buf_start_idx = None
            i += 1
            continue

        # Not a terminator — keep buffering
        i += 1

    return result


# =============================================================================
# CLI
# =============================================================================

def main():
    import sys
    import json
    root = Path(__file__).resolve().parent.parent
    cache = root / "cache"
    outputs = root / "outputs"
    outputs.mkdir(exist_ok=True)

    jobs = [
        ("enacted_25-26.html", "enacted_reapprops.csv"),
        ("executive_26-27.html", "executive_reapprops.csv"),
    ]

    try:
        import pandas as pd
    except ImportError:
        pd = None

    for html_name, csv_name in jobs:
        html_path = cache / html_name
        if not html_path.exists():
            print(f"[!] Missing: {html_path} — run upload_and_cache.py first")
            continue
        html = html_path.read_text()
        result = extract(html)
        print(f"\n{'='*72}")
        print(f"{html_name}")
        print(f"{'='*72}")
        print(f"  lines total : {result.n_lines}")
        print(f"  body lines  : {result.n_body}")
        print(f"  blank lines : {result.n_blank}")
        print(f"  header lines: {result.n_header}")
        print(f"  reapprops   : {len(result.reapprops)}")
        if pd is not None:
            rows = [{
                "program": r.program,
                "fund": r.fund,
                "chapter_year": r.chapter_year,
                "amending_year": r.amending_year,
                "approp_id": r.approp_id,
                "approp_amount": r.approp_amount,
                "reapprop_amount": r.reapprop_amount,
                "first_page": r.first_page_idx,
                "first_line": r.first_line_num,
                "last_page": r.last_page_idx,
                "last_line": r.last_line_num,
                "bill_language": r.bill_language,
            } for r in result.reapprops]
            df = pd.DataFrame(rows)
            df.to_csv(outputs / csv_name, index=False)
            print(f"  wrote       : outputs/{csv_name}  ({len(df)} rows)")


if __name__ == "__main__":
    main()

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

from patterns import (
    LINE_NUM_RE,
    PROGRAM_RE,
    FUND_TOP_RE,
    CHAPTER_YEAR_RE,
    CHAPTER_YEAR_CONT_RE,
    RE_AMOUNT_RE,
    DOTS_AMOUNT_RE,
    APPROP_ID_RE,
    BODY_LINE_START_RE,
    SKIP_LINE_RE,
    SUBSCHEDULE_LABEL_RE,
    parse_int_amount,
    looks_like_agency_header,
    section_of_title,
)


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
    agency: str              # e.g. "EDUCATION DEPARTMENT" (from page header)
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

    # Page where the governing chapter-year header was emitted (0-based HTML
    # page index). Needed by insert_plan to extend the source-slice backward
    # when the chyr header lives on a prior page. -1 if none.
    chyr_page_idx: int = -1
    # Page where the governing fund-top line was emitted. Same rationale:
    # multi-line fund headers can start several pages before the survivor.
    fund_page_idx: int = -1

    # Uniqueness key
    def composite_key(self) -> Tuple[str, str, str, str, int, int]:
        return (self.agency, self.program, self.fund, self.approp_id,
                self.chapter_year, self.amending_year)


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

BLANK_STYLE_RE = re.compile(r"min-height")

# Indent of body content (after line number + trailing whitespace). Used to
# detect wrapped fund-name lines which are indented more than the line that
# starts them.
_LINE_NUM_WITH_SEP_RE = re.compile(r"^(\s{0,3}\d{1,3}\s+)")


def _body_indent(raw: str) -> int:
    m = _LINE_NUM_WITH_SEP_RE.match(raw)
    return len(m.group(1)) if m else 0


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

# Structural regexes (PROGRAM_RE, FUND_TOP_RE, CHAPTER_YEAR_RE, etc.) and
# parse_int_amount() are imported from patterns.py above.


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
    agency: str = ""
    program: str = ""
    fund_parts: List[str] = []       # accumulated 1-3 lines that form the fund
    chapter_year: int = 0
    amending_year: int = 0
    chyr_page_idx: int = -1  # HTML page index where the current chyr header sits
    fund_page_idx: int = -1  # HTML page index where the current fund-top sits
    # When the prior reapprop had a "sub-schedule ... Total of sub-schedule"
    # attached, we skip those body lines so the NEXT reapprop's first_line
    # isn't pulled back to the top of the sub-schedule. Enter on sub-schedule
    # label, exit on Total-of-subschedule (+ trailing separator) OR any
    # structural header.
    in_subschedule: bool = False
    # Bill section — one of None, "appropriation", "reapprop". Detected from
    # the bill-title page-header line on each page. This extractor only emits
    # reapprop records in the reapprop section; it processes the appropriations
    # section inertly for state tracking but drops its body content.
    section: Optional[str] = None

    # Buffer for the current reapprop being accumulated
    buf_start_idx: Optional[int] = None

    # Page-header agency accumulation. Many agencies span TWO header lines —
    # e.g. "DEPARTMENT OF FAMILY ASSISTANCE" followed by "OFFICE OF CHILDREN
    # AND FAMILY SERVICES". Processing each line independently with
    # `if new_agency != agency: reset state` flip-flops the agency every page
    # break and nukes program/fund/chyr state, causing hundreds of pages of
    # reapprops to silently drop. Instead we collect agency-header candidates
    # within the current page's header block and commit the LAST one (the
    # most specific sub-agency) when the first body line of the page arrives.
    pending_agency: str = agency
    current_page_for_hdr: int = -1

    # Index walk
    i = 0
    while i < len(lines):
        L = lines[i]

        # Skip blanks — they're separators, keep buffer intact
        if L.is_blank:
            i += 1
            continue

        # Reset per-page header accumulation when the page index changes.
        if L.page_idx != current_page_for_hdr:
            current_page_for_hdr = L.page_idx
            # Keep pending_agency carrying the last known agency; individual
            # agency-header lines on the new page will overwrite as seen.

        # Unnumbered lines are page-header content (page number, agency name,
        # bill title). Check if it's an agency name or a section-title line
        # and update state; otherwise skip.
        if L.line_num is None:
            t_hdr = L.text.strip()
            sec = section_of_title(t_hdr)
            if sec is not None:
                if sec != section:
                    # New section — reset per-section state (but keep agency).
                    section = sec
                    program = ""
                    fund_parts = []
                    chapter_year = 0
                    amending_year = 0
                    chyr_page_idx = -1
                    fund_page_idx = -1
                    in_subschedule = False
                    buf_start_idx = None
            elif t_hdr and looks_like_agency_header(t_hdr):
                # Accumulate — the LAST agency-header line on the page wins
                # when we commit at the first body line below.
                pending_agency = re.sub(r"\s+", " ", t_hdr)
            i += 1
            continue

        # First body line on this page — commit the pending agency. Only
        # reset downstream state when the committed agency actually changes.
        if pending_agency != agency:
            agency = pending_agency
            program = ""
            fund_parts = []
            chapter_year = 0
            amending_year = 0
            chyr_page_idx = -1
            fund_page_idx = -1
            in_subschedule = False
            buf_start_idx = None
            section = None

        # If we're not in the reapprop section, DON'T emit reapprops. We still
        # let structural detection updates run so that state is cleanly set
        # when we enter the reapprop section.
        if section != "reapprop":
            i += 1
            continue

        t = L.text.strip()

        # --- Structural detection ---

        # Program header (all caps ending in PROGRAM)
        if PROGRAM_RE.match(t):
            program = t
            fund_parts = []
            fund_page_idx = -1
            chapter_year = 0
            amending_year = 0
            chyr_page_idx = -1
            in_subschedule = False
            buf_start_idx = None
            i += 1
            continue

        # Top-level fund line — start a new fund block.
        # Sub-fund lines follow immediately (possibly across a page break),
        # separated from a chapter-year header by a blank.
        if FUND_TOP_RE.match(t):
            fund_parts = [re.sub(r"\s+", " ", t)]
            fund_page_idx = L.page_idx
            in_subschedule = False
            last_part_indent = _body_indent(L.raw_text)
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
                if BODY_LINE_START_RE.match(tj):
                    break
                if RE_AMOUNT_RE.search(tj):
                    break
                # Otherwise, it's a continuation of the fund header. Collapse
                # internal whitespace so the same fund name always produces the
                # same string (PDF justification sometimes inserts extra spaces).
                # If the body indent is HIGHER than the previous fund-part line,
                # treat it as a wrap continuation and append to the previous
                # part instead of starting a new one.
                tj_indent = _body_indent(Lj.raw_text)
                tj_norm = re.sub(r"\s+", " ", tj)
                if tj_indent > last_part_indent:
                    fund_parts[-1] = f"{fund_parts[-1]} {tj_norm}"
                else:
                    fund_parts.append(tj_norm)
                    last_part_indent = tj_indent
                j += 1
            # Don't reset chapter_year here — in the Agriculture member-item
            # section and others, the bill puts CHYR → FUND → reapprops, so
            # the just-set chyr is still the governing one. If a new CHYR
            # appears later it will update. (When the section is structured
            # FUND → CHYR_A → CHYR_B, chapter_year was already 0 from the
            # section/agency/program reset, so nothing to preserve.)
            buf_start_idx = None
            i = j
            continue

        # Chapter year header (may span 2 lines if ends without ":")
        m = CHAPTER_YEAR_RE.match(t)
        if m:
            chapter_year = int(m.group(1))
            amending_year = int(m.group(2)) if m.group(2) else 0
            chyr_page_idx = L.page_idx
            in_subschedule = False
            buf_start_idx = None
            # If this header line didn't terminate with ":", look for a
            # continuation line (e.g. "section 1, of the laws of 2024:" or
            # "hereby amended and reappropriated to read:") and consume it too.
            consumed_to = i
            if not t.rstrip().endswith(":"):
                # Chyr header spans additional line(s) until a line ending
                # with ":". Some OCFS/OTDA chyrs span 3 lines, e.g.:
                #   "The appropriation made by chapter 53, section 1, of the
                #    laws of 2024, as"
                #   "supplemented by interchanges in accordance with state
                #    finance law,"
                #   "is hereby amended and reappropriated to read:"
                # Consume up to 3 continuation lines, stopping on ":".
                j = i + 1
                consumed = 0
                while j < len(lines) and consumed < 3:
                    Lj = lines[j]
                    if Lj.is_blank or Lj.line_num is None:
                        j += 1
                        continue
                    tj = Lj.text.strip()
                    # Safeguard: don't consume lines that clearly start a
                    # reapprop body ("For ...", "Notwithstanding ...",
                    # "Provided ...") or a new structural element.
                    if (BODY_LINE_START_RE.match(tj)
                            or PROGRAM_RE.match(tj)
                            or FUND_TOP_RE.match(tj)
                            or CHAPTER_YEAR_RE.match(tj)):
                        break
                    if amending_year == 0:
                        am = re.search(r"of\s+the\s+laws\s+of\s+(\d{4})", tj)
                        if am:
                            amending_year = int(am.group(1))
                    consumed_to = j
                    consumed += 1
                    if tj.endswith(":"):
                        break  # end of header
                    j += 1
            i = consumed_to + 1
            continue

        # --- Body line: part of a reapprop ---
        # Sub-schedule attribution: a reapprop's sub-schedule lives AFTER its
        # (re. $X) terminator, so by the time the extractor sees those lines
        # it's no longer buffering. Without guard-rails, the FIRST body line
        # of the sub-schedule starts a fresh buffer — pulling the NEXT
        # reapprop's first_line back onto the sub-schedule and causing the
        # insert generator to treat allocation rows as survivor body.
        #
        # We enter "in_subschedule" when we see the "sub-schedule" label, and
        # stay in it until we see the closing "Total of sub-schedule" + its
        # trailing separator — OR any structural reset (handled above).
        if buf_start_idx is None:
            if SUBSCHEDULE_LABEL_RE.match(t):
                in_subschedule = True
                i += 1
                continue
            if in_subschedule:
                # Detect close: line starts with "Total of sub-schedule"
                # (the actual amount follows on the same line). After the
                # total, also swallow a trailing separator line if present.
                if re.match(r"^Total of sub-schedule\b", t, re.IGNORECASE):
                    in_subschedule = False
                    # Also skip trailing "------" separator(s) on following
                    # non-blank body lines.
                    j = i + 1
                    while j < len(lines):
                        Lj = lines[j]
                        if Lj.is_blank or Lj.line_num is None:
                            j += 1
                            continue
                        tj = Lj.text.strip()
                        if re.match(r"^[-=]{5,}\s*$", tj):
                            j += 1
                            continue
                        break
                    i = j
                    continue
                # Any body line inside the sub-schedule block: skip.
                i += 1
                continue
            # Structural noise outside a sub-schedule (separator, subtotal,
            # stray "Program account subtotal" etc.) — don't start a buffer.
            if SKIP_LINE_RE.match(t):
                i += 1
                continue
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
                agency=agency,
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
                chyr_page_idx=chyr_page_idx,
                fund_page_idx=fund_page_idx,
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
            # Mark reapprop-sourced records so downstream can pick the right
            # source HTML (enacted_25-26.html for reapprops, _approps.html for
            # appropriations) when generating insert PDFs.
            source_tag = "reapprop" if html_name == "enacted_25-26.html" else "executive"
            rows = [{
                "agency": r.agency,
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
                "source": source_tag,
                "chyr_page": r.chyr_page_idx,
                "fund_page": r.fund_page_idx,
            } for r in result.reapprops]
            df = pd.DataFrame(rows)
            df.to_csv(outputs / csv_name, index=False)
            print(f"  wrote       : outputs/{csv_name}  ({len(df)} rows)")


if __name__ == "__main__":
    main()

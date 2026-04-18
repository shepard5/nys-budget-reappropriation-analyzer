"""
Shared regex patterns and structural detectors used across the pipeline.

Consolidating these in one place prevents drift between the reapprop
extractor (`extract.py`), the appropriations extractor (`extract_approps.py`),
and the insert-PDF generator (`generate_inserts.py`), all of which need to
recognize the same bill structure.

A note on the LBDC HTML line format:
    `<p>  LN   text...</p>`
where LN is 1–3 digits, padded with 0–3 leading spaces. Blank separator
lines use `<p style="min-height:Xpx"> </p>`. Everything else is a page
header (page number, agency name, bill title) with no leading digit.
"""

import re

# ──────────────────────────────────────────────────────────────────────────
# Line-number prefix — the "N  text" at the start of every body line
# ──────────────────────────────────────────────────────────────────────────
LINE_NUM_RE = re.compile(r"^\s{0,3}(\d{1,3})\s+(.*)$")


def line_num_of(text: str):
    """Return the visible line number if the text starts with one, else None."""
    m = LINE_NUM_RE.match(text)
    return int(m.group(1)) if m else None


# ──────────────────────────────────────────────────────────────────────────
# Program header
# ──────────────────────────────────────────────────────────────────────────
# Reapprops section: program name stands alone on one line, all caps ending
# in "PROGRAM".
PROGRAM_RE = re.compile(r"^[A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM$")

# Appropriations section: program name is followed by a total amount on the
# same line, e.g. "ADULT CAREER ... PROGRAM ..... 229,925,000".
PROGRAM_WITH_AMOUNT_RE = re.compile(
    r"^([A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM)\s*\.{2,}\s*\$?[\d,]+\s*$"
)
# Appropriations section: program with NO trailing amount (rare — probably
# never happens, kept for safety).
PROGRAM_PLAIN_RE = re.compile(
    r"^([A-Z][A-Z0-9 ,&'\-/]*\s+PROGRAM)\s*$"
)
# Appropriations section: program name spans two lines and continues with
# "PROGRAM ........ <amount>" on the second line. Pair with PROGRAM_CAPS_LINE_RE
# to detect the preceding line.
PROGRAM_CONT_RE = re.compile(r"^PROGRAM\s*\.{2,}\s*\$?[\d,]+\s*$")
PROGRAM_CAPS_LINE_RE = re.compile(r"^[A-Z][A-Z0-9 ,&'\-/]*[A-Z]\s*$")


# ──────────────────────────────────────────────────────────────────────────
# Fund top-level families (the first line of a fund-header block)
# ──────────────────────────────────────────────────────────────────────────
FUND_TOP_RE = re.compile(
    r"^(General Fund|Special Revenue Funds - Federal|Special Revenue Funds - Other|"
    r"Capital Projects Funds|Enterprise Funds|Fiduciary Funds)$"
)


def is_fund_top(p_text: str) -> bool:
    """True if the body-line text (after stripping the line-number prefix) is a
    top-level fund family header."""
    m = LINE_NUM_RE.match(p_text)
    if not m:
        return False
    return bool(FUND_TOP_RE.match(p_text[m.end():].strip()))


# ──────────────────────────────────────────────────────────────────────────
# Chapter-year header — two forms, possibly multi-line
# ──────────────────────────────────────────────────────────────────────────
# Form A: "By chapter N, section M, [of part X,] of the laws of YYYY[, as
#          {amended|added} by chapter ... of the laws of ZZZZ]:"
# Form B: "The appropriation[s] made by chapter N, section M, of the laws of
#          YYYY," (continues on next line with "[is|are] hereby amended and
#          reappropriated to read:")
#
# Chapter and section numbers vary across years — don't anchor on 53 or 1.
# We capture the enacted year (YYYY) and any amending year (ZZZZ).
CHAPTER_YEAR_RE = re.compile(
    r"^(?:By|The\s+appropriations?\s+made\s+by)\s+chapter\s+\d+"
    r".*?of\s+the\s+laws\s+of\s+(\d{4})"
    r"(?:.*?as\s+(?:amended|added)\s+by\s+chapter\s+\d+.*?of\s+the\s+laws\s+of\s+(\d{4}))?",
    re.IGNORECASE,
)


def is_chapter_year_header(p_text: str):
    """Return chapter_year (int) if this body line starts a chapter-year
    header, else None. Useful where the caller needs just the year."""
    m = LINE_NUM_RE.match(p_text)
    if not m:
        return None
    cm = CHAPTER_YEAR_RE.match(p_text[m.end():].strip())
    return int(cm.group(1)) if cm else None


# Continuation-of-header patterns — used by the reapprop extractor to detect
# a second line that belongs to a chapter-year header that didn't end with ":"
CHAPTER_YEAR_CONT_RE = re.compile(
    r"^(section\s+\d+|part\s+[A-Z]+|hereby|is\s+hereby|are\s+hereby|and\s+)",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────
# Reapprop / appropriation terminators
# ──────────────────────────────────────────────────────────────────────────
# Reapprop ends with "(re. $X)" or "(re. X)"
RE_AMOUNT_RE = re.compile(r"\(re\.\s*\$?([\d,]+)\s*\)")

# Appropriation ends with "....<amount>" (no re. suffix) at end of line
TERM_AMOUNT_RE = re.compile(r"\.{2,}\s*\$?([\d,]+)\s*$")

# Dots followed by an amount — used to find original appropriation amount
# embedded in bill language before the "(re." terminator
DOTS_AMOUNT_RE = re.compile(r"\.{2,}\s*\$?([\d,]+)")


# ──────────────────────────────────────────────────────────────────────────
# Approp ID — parenthesized 5-digit number
# ──────────────────────────────────────────────────────────────────────────
APPROP_ID_RE = re.compile(r"\((\d{5})\)")


# ──────────────────────────────────────────────────────────────────────────
# Appropriations-section omissions (false-positive dots-amount patterns)
# ──────────────────────────────────────────────────────────────────────────
# Schedule-table rows have TWO dollar amounts (APPROPRIATIONS + REAPPROPRIATIONS
# columns), separated by whitespace.
SCHEDULE_ROW_RE = re.compile(r"\.{2,}\s*\$?[\d,]+\s+\$?[\d,]+\s*$")

# Subtotal / separator / schedule-marker lines to skip entirely
SKIP_LINE_RE = re.compile(
    r"^(Program account subtotal\b|All Funds\b|[-=]{5,}$|SCHEDULE\b)"
)


# Body-line starters used to stop fund-header accumulation — any line starting
# with these cannot be part of a fund header.
BODY_LINE_START_RE = re.compile(
    r"^(For\b|By\b|The\s+appropriations?\s+made\s+by\b|"
    r"Notwithstanding\b|Provided\b|Of\s+the\b|Aid\s+to\b)",
    re.IGNORECASE,
)


def parse_int_amount(s: str) -> int:
    """Normalize "$X,XXX,XXX" or "X,XXX,XXX" to int."""
    return int(s.replace(",", "").replace("$", ""))


# ──────────────────────────────────────────────────────────────────────────
# Agency header detection — for multi-agency bills (full ATL, etc.)
# ──────────────────────────────────────────────────────────────────────────
# Each page's unnumbered header block typically looks like:
#    <page_number> <bill_code>
#    <AGENCY NAME>
#    AID TO LOCALITIES [- REAPPROPRIATIONS] YYYY-YY
# The agency line is all-caps text that is NOT the bill title or the
# STATE OF NEW YORK preamble. Agencies contain recognizable tokens like
# DEPARTMENT / OFFICE / DIVISION / AUTHORITY / CORPORATION / UNIVERSITY /
# MISCELLANEOUS / COUNCIL / BOARD / COMMISSION / STATE (in "STATE EDUCATION
# DEPARTMENT" etc). We use inclusion-by-keyword + exclusion-by-bill-title.
_AGENCY_KEYWORDS = re.compile(
    r"\b(DEPARTMENT|OFFICE|DIVISION|AUTHORITY|CORPORATION|UNIVERSITY|"
    r"MISCELLANEOUS|COUNCIL|BOARD|COMMISSION|ASSEMBLY|SENATE|SERVICES|"
    r"COURT|JUDICIARY|GOVERNOR|SECRETARY|COMPTROLLER|ATTORNEY|EXECUTIVE|"
    r"LEGISLATURE|AGENCY|PROGRAM)\b"
)
_BILL_TITLE_TOKENS = re.compile(
    r"\b(AID TO|STATE OPERATIONS|CAPITAL PROJECTS|DEBT SERVICE|"
    r"APPROPRIATIONS|REAPPROPRIATIONS|STATE OF NEW YORK|"
    r"IN SENATE|IN ASSEMBLY|SECTION|BILL NUMBER)\b"
)


def looks_like_agency_header(text: str) -> bool:
    """True if an unnumbered page-header line appears to name an agency
    (vs a page number, bill title, STATE OF NEW YORK preamble, etc.)."""
    t = text.strip()
    if not t:
        return False
    # Must be primarily all-caps
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    if sum(1 for c in letters if c.isupper()) / len(letters) < 0.85:
        return False
    # Exclude bill-title tokens
    if _BILL_TITLE_TOKENS.search(t):
        return False
    # Must contain at least one agency-like keyword
    return bool(_AGENCY_KEYWORDS.search(t))


# ──────────────────────────────────────────────────────────────────────────
# Section detection — appropriations vs reapproprations
# ──────────────────────────────────────────────────────────────────────────
# Bill title on every page reveals the section:
#   "AID TO LOCALITIES   YYYY-YY"                 → appropriation
#   "AID TO LOCALITIES - REAPPROPRIATIONS   YYYY-YY" → reapprop
_SECTION_TITLE_RE = re.compile(r"^AID TO LOCALITIES", re.IGNORECASE)


def section_of_title(text: str):
    """Return 'reapprop' or 'appropriation' if the text is a bill-title page
    header, else None."""
    t = text.strip()
    if not _SECTION_TITLE_RE.match(t):
        return None
    return "reapprop" if "REAPPROPRIATIONS" in t.upper() else "appropriation"

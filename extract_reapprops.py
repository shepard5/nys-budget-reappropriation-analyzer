"""
NYS Budget Reappropriation Extractor v8
========================================
Extracts every reappropriation from Education ATL budget PDFs.

Core principle: Every reappropriation ends with ". (re. $X)" or ".. (re. $X)" etc.
That pattern is the ONE reliable anchor. We find every occurrence, then walk backwards
to collect all the attributes (chapter year, fund, program, approp ID, amounts, language).

Validation target from BUDGET BREAKDOWN.xlsx:
  - 25-26 Enacted: 592 reappropriations
  - 26-27 Executive: 362 reappropriations
"""

import pdfplumber
import pandas as pd
import re
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple
from pathlib import Path


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Reappropriation:
    """A single reappropriation extracted from a budget PDF."""
    # Hierarchical location
    program: str              # e.g. "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM"
    fund: str                 # e.g. "General Fund; Local Assistance Account - 10000"
    chapter_year: int         # e.g. 2024

    # Amounts
    reapprop_amount: int      # The (re. $X) amount
    approp_amount: Optional[int] = None  # The original appropriation amount, if present

    # Identification
    approp_id: Optional[str] = None  # 5-digit ID like "21713", None if not present

    # Language/text
    bill_language: str = ""   # Full text of the reappropriation

    # Location in PDF
    page_number: int = 0      # Page number as printed in PDF
    line_number_start: Optional[int] = None  # First line number
    line_number_end: Optional[int] = None    # Line number of the (re. $X) line

    # Chapter law citation
    chapter_citation: str = ""  # Full "By chapter X, section Y, of the laws of YYYY..." text

    # PDF source
    source_file: str = ""


@dataclass
class ParsingState:
    """Tracks hierarchical state as we parse through the PDF."""
    program: str = ""
    fund_lines: List[str] = field(default_factory=list)  # Accumulates fund header lines
    fund: str = ""
    chapter_year: int = 0
    chapter_citation: str = ""

    # For tracking page continuity
    pending_buffer: List[str] = field(default_factory=list)  # Lines accumulated before next (re.$)
    pending_line_start: Optional[int] = None
    pending_page: int = 0


# ============================================================================
# REGEX PATTERNS
# ============================================================================

# The ONE anchor pattern: finds (re. $X,XXX) or (re. $X,XXX,XXX) etc.
# This MUST capture every reappropriation terminator in the document
RE_REAPPROP = re.compile(r'\(re\.\s*\$\s*([\d,]+)\s*\)')

# Chapter year header: "By chapter X, section Y, of the laws of YYYY"
# The year at the END is what matters (the "of the laws of YYYY" part)
# But it can span multiple lines, and can have "as amended by..." after
RE_CHAPTER_START = re.compile(r'^By\s+chapter\s+\d+', re.IGNORECASE)
RE_CHAPTER_YEAR = re.compile(r'of\s+the\s+laws\s+of\s+(\d{4})')

# Executive-specific: "The appropriation made by chapter X, section Y, of the laws of YYYY, is"
# "hereby amended and reappropriated to read:"
RE_AMENDED_REAPPROP_START = re.compile(r'^The\s+appropriation\s+made\s+by\s+chapter\s+\d+', re.IGNORECASE)
RE_AMENDED_REAPPROP_YEAR = re.compile(r'of\s+the\s+laws\s+of\s+(\d{4})')

# Appropriation ID: (XXXXX) - five digits in parens
RE_APPROP_ID = re.compile(r'\((\d{5})\)')

# Dollar amounts with dots leader: amount ... or amount .....
# Handles formats like:
#   54,000,000 ....................................... (re. $47,038,000)
#   (21713) ... 54,000,000 ........................... (re. $47,038,000)
# The original approp amount appears BEFORE the (re. $X)
RE_AMOUNT = re.compile(r'([\d,]+)\s*\.+\s*\(re\.')

# Also catch amounts that appear earlier in the text with dot leaders
# Like: (21713) ... 15,160,000 ........................... (re. $15,160,000)
RE_AMOUNT_WITH_DOTS = re.compile(r'([\d,]+)\s*\.{2,}')

# Program headers - all caps, specific known programs
KNOWN_PROGRAMS = [
    "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM",
    "CULTURAL EDUCATION PROGRAM",
    "OFFICE OF HIGHER EDUCATION AND THE PROFESSIONS PROGRAM",
    "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION PROGRAM",
]

# Fund type top-level patterns
RE_FUND_TOP = re.compile(
    r'^(General Fund|Special Revenue Funds\s*-\s*Federal|Special Revenue Funds\s*-\s*Other)$',
    re.IGNORECASE
)

# Account line pattern: "XXXX Account - NNNNN"
RE_ACCOUNT_LINE = re.compile(r'Account\s*-\s*\d{4,5}|Fund\s*-\s*\d{4,5}', re.IGNORECASE)

# Line number prefix: "1 text..." through "50 text..."
RE_LINE_NUM = re.compile(r'^(\d{1,2})\s+(.+)$')

# Page header pattern (to skip): "315 12553-09-5" or "EDUCATION DEPARTMENT" or "AID TO LOCALITIES..."
RE_PAGE_HEADER = re.compile(r'^\d{3}\s+\d{5}-\d{2}-\d$')
RE_DEPT_HEADER = re.compile(r'^EDUCATION DEPARTMENT$')
RE_ATL_HEADER = re.compile(r'^AID TO LOCALITIES\s*-\s*REAPPROPRIATIONS\s+\d{4}-\d{2}$')


# ============================================================================
# PARSING HELPERS
# ============================================================================

def parse_amount(amount_str: str) -> int:
    """Convert a comma-formatted dollar amount string to int."""
    return int(amount_str.replace(',', ''))


def extract_line_number(line: str) -> Tuple[Optional[int], str]:
    """Extract line number prefix if present. Returns (line_num, remaining_text)."""
    m = RE_LINE_NUM.match(line)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 56:  # Valid bill line numbers
            return num, m.group(2)
    return None, line


def is_header_line(line: str) -> bool:
    """Check if a line is a page header (not bill content)."""
    return bool(RE_PAGE_HEADER.match(line) or RE_DEPT_HEADER.match(line) or RE_ATL_HEADER.match(line))


def is_fund_header_line(text: str) -> bool:
    """Check if text is part of a fund header block."""
    text_stripped = text.strip()
    if RE_FUND_TOP.match(text_stripped):
        return True
    # Sub-fund names like "Federal Education Fund", "Vocational Rehabilitation Fund"
    if re.match(r'^[A-Z][A-Za-z\s\-]+Fund$', text_stripped):
        return True
    # Account lines like "Local Assistance Account - 10000"
    if RE_ACCOUNT_LINE.search(text_stripped) and not text_stripped.startswith('By ') and not text_stripped.startswith('For '):
        return True
    return False


def build_fund_string(fund_lines: List[str]) -> str:
    """Build a semicolon-separated fund string from accumulated fund header lines."""
    return "; ".join(line.strip() for line in fund_lines if line.strip())


def find_approp_id(text: str) -> Optional[str]:
    """Find the 5-digit appropriation ID in text. Returns last one found."""
    matches = RE_APPROP_ID.findall(text)
    if matches:
        return matches[-1]  # Last one is usually the real approp ID
    return None


def find_approp_amount(text: str) -> Optional[int]:
    """Find the original appropriation amount from the text.

    In the budget bill format, the approp amount appears with dot leaders
    before the (re. $X) marker. We need the LARGEST amount that appears
    with dot leaders, which is the original appropriation amount (not the
    reapprop amount).

    Examples:
        (21713) ... 54,000,000 ....... (re. $47,038,000)  -> 54,000,000
        750,000 ...................... (re. $750,000)        -> 750,000  (only if same line)
        15,160,000 .................. (re. $15,160,000)     -> 15,160,000
    """
    # Find all amounts followed by dots in the full text
    amounts = RE_AMOUNT_WITH_DOTS.findall(text)
    if amounts:
        # The approp amount is typically the one right before (re. $)
        # But with multi-line text, we want the one closest to (re.$)
        # Find the amount that's on the same line as or just before (re.$)
        re_match = RE_REAPPROP.search(text)
        if re_match:
            # Look at the text just before (re. $...)
            before_re = text[:re_match.start()]
            # Find the last amount with dots before (re.)
            before_amounts = RE_AMOUNT_WITH_DOTS.findall(before_re)
            if before_amounts:
                return parse_amount(before_amounts[-1])

    # Fallback: look at the direct pattern "amount....(re."
    m = RE_AMOUNT.search(text)
    if m:
        return parse_amount(m.group(1))

    return None


# ============================================================================
# MAIN EXTRACTION
# ============================================================================

def extract_reappropriations(pdf_path: str, page_offset: int) -> List[Reappropriation]:
    """
    Extract all reappropriations from a budget PDF.

    Args:
        pdf_path: Path to the PDF file
        page_offset: The page number printed on the first page of the PDF
                    (e.g., 315 for the 25-26 enacted, 268 for 26-27 executive)

    Returns:
        List of Reappropriation objects
    """
    pdf = pdfplumber.open(pdf_path)
    source_file = Path(pdf_path).name

    all_reapprops: List[Reappropriation] = []
    state = ParsingState()

    # First pass: extract raw text from every page, with page numbers
    pages_text = []
    for idx, page in enumerate(pdf.pages):
        page_num = page_offset + idx
        text = page.extract_text()
        if text:
            pages_text.append((page_num, text))

    pdf.close()

    # Second pass: parse line by line through all pages
    # We process ALL lines in document order, maintaining state

    for page_num, page_text in pages_text:
        raw_lines = page_text.split('\n')

        # Skip header lines (first 3 lines are always page header)
        content_lines = []
        for raw_line in raw_lines:
            if is_header_line(raw_line):
                continue
            content_lines.append(raw_line)

        for raw_line in content_lines:
            line_num, line_text = extract_line_number(raw_line)
            line_text_stripped = line_text.strip()

            if not line_text_stripped:
                continue

            # --------------------------------------------------------
            # CHECK 1: Is this a program header?
            # --------------------------------------------------------
            for prog in KNOWN_PROGRAMS:
                if line_text_stripped == prog:
                    # Flush any pending buffer before changing program
                    # (shouldn't happen normally, but safety)
                    state.program = prog
                    state.fund_lines = []
                    state.fund = ""
                    state.chapter_year = 0
                    state.chapter_citation = ""
                    break

            if line_text_stripped in KNOWN_PROGRAMS:
                continue  # Don't add program header to buffer

            # --------------------------------------------------------
            # CHECK 2: Is this a fund header line?
            # --------------------------------------------------------
            if is_fund_header_line(line_text_stripped):
                # If this is a new top-level fund type, reset fund accumulator
                if RE_FUND_TOP.match(line_text_stripped):
                    state.fund_lines = [line_text_stripped]
                else:
                    state.fund_lines.append(line_text_stripped)

                # If we see an account line, the fund header is complete
                if RE_ACCOUNT_LINE.search(line_text_stripped):
                    state.fund = build_fund_string(state.fund_lines)
                    state.chapter_year = 0  # Reset chapter year for new fund
                    state.chapter_citation = ""
                continue  # Don't add fund headers to buffer

            # --------------------------------------------------------
            # CHECK 3: Is this a chapter year header?
            # Standard: "By chapter 53, section 1, of the laws of YYYY..."
            # Executive amended: "The appropriation made by chapter 53, section 1, of the laws of YYYY, is"
            # --------------------------------------------------------
            is_chapter_header = RE_CHAPTER_START.match(line_text_stripped)
            is_amended_header = RE_AMENDED_REAPPROP_START.match(line_text_stripped)

            if is_chapter_header or is_amended_header:
                # This is the start of a chapter citation
                # It may span multiple lines before the colon
                # Extract year from this line if possible
                year_match = RE_CHAPTER_YEAR.search(line_text_stripped)
                if year_match:
                    state.chapter_year = int(year_match.group(1))
                    state.chapter_citation = line_text_stripped
                else:
                    # Year might be on a continuation line - start accumulating
                    state.chapter_citation = line_text_stripped
                # If it already ends with ":" it's complete, otherwise continue accumulating
                if not line_text_stripped.rstrip().endswith(':'):
                    continue
                else:
                    continue

            # --------------------------------------------------------
            # CHECK 3b: Is this a continuation of a chapter citation?
            # Chapter citations can span 2-3 lines:
            #   "By chapter 53, section 1, of the laws of 2023, as amended by chapter 53,"
            #   "section 1, of the laws of 2024:"
            # Also handles executive amended format:
            #   "The appropriation made by chapter 53, section 1, of the laws of 2025, is"
            #   "hereby amended and reappropriated to read:"
            # We know we're in a chapter citation if the citation doesn't end with ":"
            # --------------------------------------------------------
            if state.chapter_citation and not state.chapter_citation.rstrip().endswith(':'):
                state.chapter_citation += " " + line_text_stripped
                year_match = RE_CHAPTER_YEAR.search(state.chapter_citation)
                if year_match:
                    # Get the FIRST year - that's the original chapter year
                    state.chapter_year = int(year_match.group(1))
                # If line ends with ":", the citation is complete
                continue

            # --------------------------------------------------------
            # CHECK 4: Does this line contain a (re. $X) marker?
            # This is THE anchor - every reappropriation ends here.
            # --------------------------------------------------------
            re_matches = list(RE_REAPPROP.finditer(line_text_stripped))

            if re_matches:
                # There could be multiple (re. $) on the same line (rare but possible)
                # Usually just one per line
                for re_match in re_matches:
                    reapprop_amount = parse_amount(re_match.group(1))

                    # Build full text of this reappropriation:
                    # pending_buffer + current line up through (re. $X)
                    full_text_parts = list(state.pending_buffer)
                    # Add current line text
                    full_text_parts.append(line_text_stripped)
                    full_text = "\n".join(full_text_parts)

                    # Extract attributes from full text
                    approp_id = find_approp_id(full_text)
                    approp_amount = find_approp_amount(full_text)

                    reapprop = Reappropriation(
                        program=state.program,
                        fund=state.fund,
                        chapter_year=state.chapter_year,
                        reapprop_amount=reapprop_amount,
                        approp_amount=approp_amount,
                        approp_id=approp_id,
                        bill_language=full_text,
                        page_number=page_num,
                        line_number_start=state.pending_line_start,
                        line_number_end=line_num,
                        chapter_citation=state.chapter_citation,
                        source_file=source_file,
                    )

                    all_reapprops.append(reapprop)

                    # Reset the buffer after capturing
                    state.pending_buffer = []
                    state.pending_line_start = None
                    state.pending_page = page_num

            else:
                # No (re. $) on this line — it's bill language being accumulated
                # for the next reappropriation
                state.pending_buffer.append(line_text_stripped)
                if state.pending_line_start is None and line_num is not None:
                    state.pending_line_start = line_num
                if line_num is not None:
                    state.pending_page = page_num

    return all_reapprops


# ============================================================================
# VALIDATION
# ============================================================================

def validate_against_breakdown(reapprops: List[Reappropriation], breakdown_path: str, year: int):
    """Validate extraction counts against the BUDGET BREAKDOWN.xlsx Sheet1."""
    df = pd.read_excel(breakdown_path, sheet_name='Sheet1')
    expected = df[df['YEAR'] == year]

    print(f"\n{'='*80}")
    print(f"VALIDATION: {year} ({'25-26 Enacted' if year == 2025 else '26-27 Executive'})")
    print(f"{'='*80}")

    total_expected = int(expected['NUMBER_APPROPRIATIONS'].sum())
    total_actual = len(reapprops)

    print(f"\nTotal expected: {total_expected}")
    print(f"Total extracted: {total_actual}")
    print(f"{'✓ MATCH' if total_actual == total_expected else '✗ MISMATCH: delta = ' + str(total_actual - total_expected)}")

    # Validate per fund section
    print(f"\n{'Program':<55} {'Fund':<40} {'Expected':>8} {'Actual':>8} {'Status'}")
    print("-" * 160)

    all_match = True
    for _, row in expected.iterrows():
        prog = row['PROGRAM']
        fund_excel = row['FUND']
        exp_count = int(row['NUMBER_APPROPRIATIONS'])
        page_start = int(row['PAGE_START'])

        # Match fund: Excel uses semicolons, our extracted fund also uses semicolons
        # But need to handle slight formatting differences
        actual_count = sum(1 for r in reapprops
                         if r.program == prog and _fund_matches(r.fund, fund_excel))

        status = "✓" if actual_count == exp_count else f"✗ (delta={actual_count - exp_count})"
        if actual_count != exp_count:
            all_match = False

        print(f"{prog[:55]:<55} {str(fund_excel)[:40]:<40} {exp_count:>8} {actual_count:>8} {status}")

    print(f"\n{'ALL SECTIONS MATCH' if all_match else 'MISMATCHES FOUND - see above'}")
    return all_match


def _fund_matches(extracted_fund: str, excel_fund: str) -> bool:
    """Check if an extracted fund string matches the Excel fund string.

    Excel uses semicolons as separators. Our extracted fund should match
    after normalizing whitespace.
    """
    # Normalize both
    def normalize(s):
        s = str(s).strip()
        s = re.sub(r'\s+', ' ', s)
        # Remove leading/trailing spaces around semicolons
        s = re.sub(r'\s*;\s*', '; ', s)
        return s.lower()

    return normalize(extracted_fund) == normalize(excel_fund)


def validate_page_counts(reapprops: List[Reappropriation], breakdown_path: str, year: int):
    """Validate per-page counts against Sheet2/Sheet4 data."""
    if year == 2025:
        # Sheet4 has page-by-page for 25-26 PreK-12 Gen Fund (pages 331-424)
        df4 = pd.read_excel(breakdown_path, sheet_name='Sheet4')
        print(f"\n{'='*80}")
        print("PAGE-BY-PAGE VALIDATION (Sheet4 - 25-26 PreK-12 Gen Fund subset)")
        print(f"{'='*80}")

        mismatches = []
        for _, row in df4.iterrows():
            page = int(row['page'])
            expected = int(row['num_reapprops'])
            actual = sum(1 for r in reapprops if r.page_number == page)
            if actual != expected:
                mismatches.append((page, expected, actual))

        if mismatches:
            print(f"\nMismatches on {len(mismatches)} pages:")
            for page, exp, act in sorted(mismatches):
                print(f"  Page {page}: expected {exp}, got {act} (delta={act-exp})")
        else:
            print(f"\n✓ All {len(df4)} pages match!")


# ============================================================================
# OUTPUT
# ============================================================================

def to_dataframe(reapprops: List[Reappropriation]) -> pd.DataFrame:
    """Convert list of Reappropriation objects to a DataFrame."""
    records = [asdict(r) for r in reapprops]
    df = pd.DataFrame(records)
    return df


# ============================================================================
# COMPARISON ENGINE
# ============================================================================

@dataclass
class ComparisonResult:
    """Result of comparing enacted vs executive reappropriations."""
    continued: List[Tuple[Reappropriation, Reappropriation]]      # matched, in both
    modified: List[Tuple[Reappropriation, Reappropriation]]        # matched but amounts differ
    dropped: List[Reappropriation]                                  # in enacted, not in executive
    new_in_exec: List[Reappropriation]                             # in executive, not in enacted (new chapter year)


def compare_budgets(enacted: List[Reappropriation], executive: List[Reappropriation]) -> ComparisonResult:
    """
    Compare enacted vs executive reappropriations to find drops.

    Matching strategy (multi-pass):
      Pass 1: EXACT match on (program, fund, chapter_year, approp_id)
              - If both have approp_id, this is the gold standard
      Pass 2: Match on (program, fund, chapter_year, approp_id) ignoring fund differences
              - Catches items that moved between sub-funds
      Pass 3: For items WITHOUT approp_id, match on (program, fund, chapter_year, approp_amount)
              - Best we can do for bare-amount items
      Pass 4: Text similarity fallback for remaining unmatched items
              - Uses normalized bill language Jaccard similarity

    Within each pass, if the reapprop_amount is the same → "continued"
    If different → "modified" (amount changed in executive)

    Anything in enacted not matched by any pass → "dropped"
    Anything in executive not matched → "new_in_exec" (typically new 2025 chapter year)
    """

    continued = []
    modified = []
    dropped = []
    new_in_exec = []

    # Track which items have been matched
    enacted_matched = set()    # indices into enacted list
    exec_matched = set()       # indices into executive list

    # Build lookup indices for executive
    # Key: (program, fund_normalized, chapter_year, approp_id) -> list of (index, reapprop)
    exec_by_key = {}
    for i, r in enumerate(executive):
        key = (r.program, _normalize_fund(r.fund), r.chapter_year, r.approp_id)
        exec_by_key.setdefault(key, []).append((i, r))

    # Pass 1: Exact match on (program, fund, chapter_year, approp_id)
    for ei, enacted_r in enumerate(enacted):
        if ei in enacted_matched:
            continue
        if not enacted_r.approp_id:
            continue  # Skip items without ID for this pass

        key = (enacted_r.program, _normalize_fund(enacted_r.fund), enacted_r.chapter_year, enacted_r.approp_id)
        candidates = exec_by_key.get(key, [])

        # Find best unmatched candidate
        best_xi = None
        best_xr = None
        for xi, xr in candidates:
            if xi not in exec_matched:
                best_xi = xi
                best_xr = xr
                break  # Take first available

        if best_xr:
            enacted_matched.add(ei)
            exec_matched.add(best_xi)
            if enacted_r.reapprop_amount == best_xr.reapprop_amount:
                continued.append((enacted_r, best_xr))
            else:
                modified.append((enacted_r, best_xr))

    # Pass 2: Match on (program, chapter_year, approp_id) ignoring fund
    exec_by_key2 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, r.chapter_year, r.approp_id)
        exec_by_key2.setdefault(key, []).append((i, r))

    for ei, enacted_r in enumerate(enacted):
        if ei in enacted_matched:
            continue
        if not enacted_r.approp_id:
            continue

        key = (enacted_r.program, enacted_r.chapter_year, enacted_r.approp_id)
        candidates = exec_by_key2.get(key, [])

        best_xi = None
        best_xr = None
        for xi, xr in candidates:
            if xi not in exec_matched:
                best_xi = xi
                best_xr = xr
                break

        if best_xr:
            enacted_matched.add(ei)
            exec_matched.add(best_xi)
            if enacted_r.reapprop_amount == best_xr.reapprop_amount:
                continued.append((enacted_r, best_xr))
            else:
                modified.append((enacted_r, best_xr))

    # Pass 3: For items WITHOUT approp_id, match on (program, fund, chapter_year, approp_amount)
    exec_by_key3 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, _normalize_fund(r.fund), r.chapter_year, r.approp_amount)
        exec_by_key3.setdefault(key, []).append((i, r))

    for ei, enacted_r in enumerate(enacted):
        if ei in enacted_matched:
            continue
        if enacted_r.approp_id:
            continue  # This pass is for items WITHOUT ID

        key = (enacted_r.program, _normalize_fund(enacted_r.fund), enacted_r.chapter_year, enacted_r.approp_amount)
        candidates = exec_by_key3.get(key, [])

        best_xi = None
        best_xr = None
        best_sim = 0
        for xi, xr in candidates:
            if xi not in exec_matched:
                sim = _text_similarity(enacted_r.bill_language, xr.bill_language)
                if sim > best_sim:
                    best_sim = sim
                    best_xi = xi
                    best_xr = xr

        if best_xr and best_sim > 0.3:
            enacted_matched.add(ei)
            exec_matched.add(best_xi)
            if enacted_r.reapprop_amount == best_xr.reapprop_amount:
                continued.append((enacted_r, best_xr))
            else:
                modified.append((enacted_r, best_xr))

    # Pass 4: Text similarity fallback for remaining items WITH approp IDs
    # Match on (program, chapter_year) with high text similarity threshold
    exec_by_key4 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, r.chapter_year)
        exec_by_key4.setdefault(key, []).append((i, r))

    for ei, enacted_r in enumerate(enacted):
        if ei in enacted_matched:
            continue

        key = (enacted_r.program, enacted_r.chapter_year)
        candidates = exec_by_key4.get(key, [])

        best_xi = None
        best_xr = None
        best_sim = 0
        for xi, xr in candidates:
            if xi not in exec_matched:
                sim = _text_similarity(enacted_r.bill_language, xr.bill_language)
                if sim > best_sim:
                    best_sim = sim
                    best_xi = xi
                    best_xr = xr

        if best_xr and best_sim > 0.6:
            enacted_matched.add(ei)
            exec_matched.add(best_xi)
            if enacted_r.reapprop_amount == best_xr.reapprop_amount:
                continued.append((enacted_r, best_xr))
            else:
                modified.append((enacted_r, best_xr))

    # Everything left in enacted = dropped
    for ei, enacted_r in enumerate(enacted):
        if ei not in enacted_matched:
            dropped.append(enacted_r)

    # Everything left in executive = new
    for xi, exec_r in enumerate(executive):
        if xi not in exec_matched:
            new_in_exec.append(exec_r)

    return ComparisonResult(
        continued=continued,
        modified=modified,
        dropped=dropped,
        new_in_exec=new_in_exec,
    )


def _normalize_fund(fund: str) -> str:
    """Normalize fund string for matching."""
    s = str(fund).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*;\s*', '; ', s)
    return s


def _normalize_text_for_similarity(text: str) -> str:
    """Normalize bill language for text similarity comparison."""
    text = text.lower()
    # Remove line numbers
    text = re.sub(r'^\d{1,2}\s+', '', text, flags=re.MULTILINE)
    # Remove dollar amounts
    text = re.sub(r'\$?\d{1,3}(?:,\d{3})*', '', text)
    # Remove dot leaders
    text = re.sub(r'\.{2,}', '', text)
    # Remove (re. $...)
    text = re.sub(r'\(re\.\s*\$[^)]*\)', '', text)
    # Remove approp IDs
    text = re.sub(r'\(\d{5}\)', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _text_similarity(text1: str, text2: str) -> float:
    """Jaccard similarity on normalized word tokens."""
    t1 = _normalize_text_for_similarity(text1)
    t2 = _normalize_text_for_similarity(text2)

    words1 = set(w for w in t1.split() if len(w) >= 3)
    words2 = set(w for w in t2.split() if len(w) >= 3)

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


def print_comparison_report(result: ComparisonResult):
    """Print a detailed comparison report."""
    print(f"\n{'='*80}")
    print("COMPARISON: 25-26 Enacted vs 26-27 Executive")
    print(f"{'='*80}")

    print(f"\n  Continued (same amount):     {len(result.continued):>5}")
    print(f"  Modified (amount changed):   {len(result.modified):>5}")
    print(f"  DROPPED (not in executive):  {len(result.dropped):>5}")
    print(f"  New in executive (2025 ChYr): {len(result.new_in_exec):>4}")

    total_accounted = len(result.continued) + len(result.modified) + len(result.dropped)
    print(f"\n  Total enacted accounted for: {total_accounted} (should be 592)")

    # Dropped by program
    print(f"\n{'='*80}")
    print("DROPPED ITEMS BY PROGRAM")
    print(f"{'='*80}")
    from collections import Counter
    prog_counts = Counter(r.program for r in result.dropped)
    for prog, count in prog_counts.most_common():
        print(f"  {prog}: {count}")

    # Dropped by fund
    print(f"\n{'='*80}")
    print("DROPPED ITEMS BY FUND")
    print(f"{'='*80}")
    fund_counts = Counter(r.fund for r in result.dropped)
    for fund, count in fund_counts.most_common():
        print(f"  {fund[:70]}: {count}")

    # Dropped dollar amounts
    total_dropped = sum(r.reapprop_amount for r in result.dropped)
    print(f"\nTotal dropped reapprop amount: ${total_dropped:,.0f}")

    # New in exec
    if result.new_in_exec:
        print(f"\n{'='*80}")
        print("NEW IN EXECUTIVE (not in enacted — typically chapter year 2025)")
        print(f"{'='*80}")
        for r in result.new_in_exec[:10]:
            print(f"  ChYr {r.chapter_year} | ID {r.approp_id} | ${r.reapprop_amount:,.0f} | {r.bill_language[:80]}")
        if len(result.new_in_exec) > 10:
            print(f"  ... and {len(result.new_in_exec) - 10} more")


# ============================================================================
# MAIN
# ============================================================================

def main():
    base_dir = Path("/Users/samscott/Desktop/REAPPROPS")

    enacted_pdf = base_dir / "25-26 ATL Reapprops - S03003D_A03003D_pg315-450-lbdc_pdf_editor-2026-02-23-1808.pdf"
    exec_pdf = base_dir / "26-27 ATL Reapprops - S09003A_A10003A_pg268-352-lbdc_pdf_editor-2026-02-23-1808.pdf"
    breakdown = base_dir / "BUDGET BREAKDOWN.xlsx"

    print("=" * 80)
    print("NYS Budget Reappropriation Extractor v8")
    print("=" * 80)

    # Extract 25-26 Enacted
    print("\n>>> Extracting 25-26 Enacted...")
    enacted_reapprops = extract_reappropriations(str(enacted_pdf), page_offset=315)
    print(f"    Extracted {len(enacted_reapprops)} reappropriations")

    # Extract 26-27 Executive
    print("\n>>> Extracting 26-27 Executive...")
    exec_reapprops = extract_reappropriations(str(exec_pdf), page_offset=268)
    print(f"    Extracted {len(exec_reapprops)} reappropriations")

    # Validate
    validate_against_breakdown(enacted_reapprops, str(breakdown), 2025)
    validate_against_breakdown(exec_reapprops, str(breakdown), 2026)

    # Page-by-page validation for enacted
    validate_page_counts(enacted_reapprops, str(breakdown), 2025)

    # Save extraction CSVs
    enacted_df = to_dataframe(enacted_reapprops)
    exec_df = to_dataframe(exec_reapprops)

    enacted_df.to_csv(base_dir / "enacted_25_26_reapprops.csv", index=False)
    exec_df.to_csv(base_dir / "executive_26_27_reapprops.csv", index=False)

    print(f"\n>>> Saved enacted_25_26_reapprops.csv ({len(enacted_df)} rows)")
    print(f">>> Saved executive_26_27_reapprops.csv ({len(exec_df)} rows)")

    # ================================================================
    # COMPARISON
    # ================================================================
    print("\n\n>>> Running comparison...")
    result = compare_budgets(enacted_reapprops, exec_reapprops)
    print_comparison_report(result)

    # Save dropped items
    dropped_df = to_dataframe(result.dropped)
    dropped_df.to_csv(base_dir / "dropped_reapprops.csv", index=False)
    print(f"\n>>> Saved dropped_reapprops.csv ({len(dropped_df)} rows)")

    # Save continued/modified
    continued_records = []
    for enacted_r, exec_r in result.continued:
        rec = asdict(enacted_r)
        rec['status'] = 'continued'
        rec['exec_reapprop_amount'] = exec_r.reapprop_amount
        rec['exec_page'] = exec_r.page_number
        continued_records.append(rec)
    for enacted_r, exec_r in result.modified:
        rec = asdict(enacted_r)
        rec['status'] = 'modified'
        rec['exec_reapprop_amount'] = exec_r.reapprop_amount
        rec['exec_page'] = exec_r.page_number
        continued_records.append(rec)

    continued_df = pd.DataFrame(continued_records)
    continued_df.to_csv(base_dir / "continued_modified_reapprops.csv", index=False)
    print(f">>> Saved continued_modified_reapprops.csv ({len(continued_df)} rows)")

    # Save new in exec
    new_df = to_dataframe(result.new_in_exec)
    new_df.to_csv(base_dir / "new_in_executive.csv", index=False)
    print(f">>> Saved new_in_executive.csv ({len(new_df)} rows)")

    # Final summary
    print(f"\n{'='*80}")
    print("EXTRACTION + COMPARISON SUMMARY")
    print(f"{'='*80}")
    print(f"  25-26 Enacted:    {len(enacted_reapprops)} reappropriations")
    print(f"  26-27 Executive:  {len(exec_reapprops)} reappropriations")
    print(f"  Continued:        {len(result.continued)}")
    print(f"  Modified:         {len(result.modified)}")
    print(f"  DROPPED:          {len(result.dropped)}")
    print(f"  New in exec:      {len(result.new_in_exec)}")
    print(f"  Accounted for:    {len(result.continued) + len(result.modified) + len(result.dropped)} / {len(enacted_reapprops)} enacted")
    print(f"  Programs: {len(set(r.program for r in enacted_reapprops))}")
    print(f"  Chapter years (enacted): {sorted(set(r.chapter_year for r in enacted_reapprops))}")
    print(f"  With approp ID: {sum(1 for r in enacted_reapprops if r.approp_id)}")
    print(f"  Without approp ID: {sum(1 for r in enacted_reapprops if not r.approp_id)}")


if __name__ == "__main__":
    main()

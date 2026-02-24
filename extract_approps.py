"""
NYS Budget Appropriation Extractor
===================================
Extracts 2025-26 appropriation items from the enacted Education ATL budget.

These are the CURRENT-YEAR appropriation items (pages 264-314) — new spending
authority, NOT reappropriations. Format: bill language ending with dots + dollar
amount, e.g.:

    (21713) ..................................... 54,000,000
    BRIDGES ......................................... 50,000
    .............................................. 4,000,000

Items without approp IDs (bare amounts) are also captured.

Skip rules filter out:
  1. Agency header summary block (before first program header)
  2. Program header lines (ALL CAPS + dots + total)
  3. "Program account subtotal" lines
  4. Separator lines (-------, =======)

Usage:
    python extract_approps.py
"""

import pdfplumber
import pandas as pd
import re
import json
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple
from pathlib import Path


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Appropriation:
    """A single appropriation extracted from a budget PDF."""
    program: str
    fund: str
    chapter_year: int = 2025  # Always 2025 for current-year appropriations
    approp_amount: int = 0
    approp_id: Optional[str] = None
    has_approp_id: bool = False
    bill_language: str = ""
    page_number: int = 0
    line_number_start: Optional[int] = None
    line_number_end: Optional[int] = None
    source_file: str = ""


@dataclass
class ParsingState:
    """Tracks hierarchical state as we parse through the PDF."""
    program: str = ""
    fund_lines: List[str] = field(default_factory=list)
    fund: str = ""
    saw_first_program: bool = False  # Skip everything before first program header

    # For multi-line item assembly
    pending_buffer: List[str] = field(default_factory=list)
    pending_line_start: Optional[int] = None
    pending_page: int = 0


# ============================================================================
# REGEX PATTERNS
# ============================================================================

# Core pattern: two or more dots followed by a comma-separated number at end of line
RE_APPROP_AMOUNT = re.compile(r'\.{2,}\s*([\d,]+)\s*$')

# Appropriation ID: (XXXXX) - five digits in parens
RE_APPROP_ID = re.compile(r'\((\d{5})\)')

# Line number prefix: "1 text..." through "56 text..."
RE_LINE_NUM = re.compile(r'^(\d{1,2})\s+(.+)$')

# Page header patterns (to skip)
RE_PAGE_HEADER = re.compile(r'^\d{3}\s+\d{5}-\d{2}-\d$')
RE_DEPT_HEADER = re.compile(r'^EDUCATION DEPARTMENT$')
# Matches both appropriation and reappropriation headers
RE_ATL_HEADER = re.compile(r'^AID TO LOCALITIES')

# Known Education programs
KNOWN_PROGRAMS = [
    "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM",
    "CULTURAL EDUCATION PROGRAM",
    "OFFICE OF HIGHER EDUCATION AND THE PROFESSIONS PROGRAM",
    "OFFICE OF MANAGEMENT SERVICES PROGRAM",
    "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION PROGRAM",
]

# Some program headers span two lines — the first line is a partial match
PARTIAL_PROGRAM_HEADERS = {
    "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION":
        "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION PROGRAM",
}

# Fund type top-level patterns (reused from extract_reapprops.py)
RE_FUND_TOP = re.compile(
    r'^(General Fund|Special Revenue Funds\s*-\s*Federal|Special Revenue Funds\s*-\s*Other)$',
    re.IGNORECASE
)

# Account line pattern
RE_ACCOUNT_LINE = re.compile(r'Account\s*-\s*\d{4,5}|Fund\s*-\s*\d{4,5}', re.IGNORECASE)

# Skip patterns
RE_SUBTOTAL = re.compile(r'subtotal', re.IGNORECASE)
RE_SEPARATOR = re.compile(r'^-{4,}$|^={4,}$')

# Agency header summary lines: have TWO numbers separated by spaces
# e.g., "General Fund ....................... 34,967,122,850 3,051,325,000"
RE_DOUBLE_AMOUNT = re.compile(r'\.{2,}\s*[\d,]+\s+[\d,]+\s*$')


# ============================================================================
# PARSING HELPERS
# ============================================================================

def parse_amount(amount_str: str) -> int:
    """Convert a comma-formatted dollar amount string to int."""
    return int(amount_str.replace(',', ''))


def extract_line_number(line: str) -> Tuple[Optional[int], str]:
    """Extract line number prefix if present."""
    m = RE_LINE_NUM.match(line)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 56:
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
    # Multi-word fund names that continue on next line
    if re.match(r'^[A-Z][A-Za-z\s\-]+Fund\s*$', text_stripped):
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
        return matches[-1]
    return None


def is_program_header(line: str) -> Optional[str]:
    """Check if a line is a program header. Returns the program name if matched, else None.

    Handles:
    - Single-line: "CULTURAL EDUCATION PROGRAM ........... 139,631,500"
    - Multi-line first part: "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION"
    - Multi-line second part: "PROGRAM ............................................... 45,531,674,500"
    """
    stripped = line.strip()
    # Remove dots and trailing amount for comparison
    text_for_match = re.sub(r'\.{2,}\s*[\d,]+\s*$', '', stripped).strip()

    # Full match against known programs
    for prog in KNOWN_PROGRAMS:
        if prog in text_for_match or prog == text_for_match:
            return prog

    # Partial match — first line of a multi-line program header
    for partial, full_name in PARTIAL_PROGRAM_HEADERS.items():
        if partial in text_for_match or partial == text_for_match:
            return full_name

    # Continuation line: just "PROGRAM" with dots + amount (second line of multi-line header)
    if re.match(r'^PROGRAM\s*\.{2,}', stripped):
        return '__CONTINUATION__'  # Signal to skip but don't change program (already set)

    return None


def is_agency_summary_line(line: str) -> bool:
    """Check if a line is part of the agency header summary block.

    These have two amounts on one line:
        General Fund ....................... 34,967,122,850 3,051,325,000
    """
    return bool(RE_DOUBLE_AMOUNT.search(line))


def is_schedule_or_meta_line(line: str) -> bool:
    """Check if a line is SCHEDULE header or other non-appropriation meta text."""
    stripped = line.strip()
    if stripped == 'SCHEDULE':
        return True
    if stripped.startswith('For payment according to'):
        return True
    if stripped.startswith('disallowances'):
        return True
    if stripped == 'APPROPRIATIONS REAPPROPRIATIONS':
        return True
    if stripped.startswith('All Funds'):
        return True
    return False


# ============================================================================
# MAIN EXTRACTION
# ============================================================================

def extract_appropriations(pdf_path: str, start_page: int, end_page: int) -> List[Appropriation]:
    """Extract all appropriation items from the specified page range.

    Args:
        pdf_path: Path to the PDF file
        start_page: First page number (as printed in PDF) to extract from
        end_page: Last page number (inclusive) to extract from

    Returns:
        List of Appropriation objects
    """
    pdf = pdfplumber.open(pdf_path)
    source_file = Path(pdf_path).name

    all_approps: List[Appropriation] = []
    state = ParsingState()

    # First pass: extract raw text from pages in range
    pages_text = []
    for page in pdf.pages:
        text = page.extract_text()
        if not text:
            continue
        # Get page number from header line (first line like "264 12553-09-5")
        first_line = text.split('\n')[0] if text else ''
        m = re.match(r'^(\d{3})\s+', first_line)
        if m:
            page_num = int(m.group(1))
            if start_page <= page_num <= end_page:
                pages_text.append((page_num, text))

    pdf.close()

    print(f"  Found {len(pages_text)} pages in range {start_page}-{end_page}")

    # Second pass: parse line by line
    for page_num, page_text in pages_text:
        raw_lines = page_text.split('\n')

        # Filter out header lines
        content_lines = [l for l in raw_lines if not is_header_line(l)]

        for raw_line in content_lines:
            line_num, line_text = extract_line_number(raw_line)
            line_text_stripped = line_text.strip()

            if not line_text_stripped:
                continue

            # Skip "AB" page markers
            if line_text_stripped == 'AB':
                continue

            # --------------------------------------------------------
            # CHECK 1: Is this a program header?
            # --------------------------------------------------------
            matched_program = is_program_header(line_text_stripped)
            if matched_program:
                # Flush any pending buffer
                if state.pending_buffer and state.saw_first_program:
                    _flush_no_amount(state)

                if matched_program != '__CONTINUATION__':
                    state.program = matched_program
                    state.fund_lines = []
                    state.fund = ""
                state.saw_first_program = True
                continue

            # Skip everything before first program header (agency summary block)
            if not state.saw_first_program:
                continue

            # --------------------------------------------------------
            # CHECK 2: Is this a fund header line?
            # --------------------------------------------------------
            if is_fund_header_line(line_text_stripped):
                # Flush pending buffer when switching funds
                if state.pending_buffer:
                    _flush_no_amount(state)

                if RE_FUND_TOP.match(line_text_stripped):
                    state.fund_lines = [line_text_stripped]
                else:
                    state.fund_lines.append(line_text_stripped)

                if RE_ACCOUNT_LINE.search(line_text_stripped):
                    state.fund = build_fund_string(state.fund_lines)
                continue

            # --------------------------------------------------------
            # CHECK 3: Skip separator lines and subtotal lines
            # --------------------------------------------------------
            if RE_SEPARATOR.match(line_text_stripped):
                continue

            if RE_SUBTOTAL.search(line_text_stripped):
                # Flush pending buffer — subtotal is not part of an item
                if state.pending_buffer:
                    _flush_no_amount(state)
                continue

            # Skip agency summary lines (double amounts)
            if is_agency_summary_line(line_text_stripped):
                continue

            # Skip schedule/meta lines
            if is_schedule_or_meta_line(line_text_stripped):
                continue

            # --------------------------------------------------------
            # CHECK 4: Does this line have dots + amount pattern?
            # --------------------------------------------------------
            amount_match = RE_APPROP_AMOUNT.search(line_text_stripped)

            if amount_match:
                approp_amount = parse_amount(amount_match.group(1))

                # Build full text from pending buffer + this line
                full_text_parts = list(state.pending_buffer)
                full_text_parts.append(line_text_stripped)
                full_text = "\n".join(full_text_parts)

                # Extract approp ID from full text
                approp_id = find_approp_id(full_text)

                approp = Appropriation(
                    program=state.program,
                    fund=state.fund,
                    chapter_year=2025,
                    approp_amount=approp_amount,
                    approp_id=approp_id,
                    has_approp_id=approp_id is not None,
                    bill_language=full_text,
                    page_number=page_num,
                    line_number_start=state.pending_line_start if state.pending_line_start else line_num,
                    line_number_end=line_num,
                    source_file=source_file,
                )

                all_approps.append(approp)

                # Reset buffer
                state.pending_buffer = []
                state.pending_line_start = None
                state.pending_page = page_num

            else:
                # No amount on this line — accumulate as pending bill language
                state.pending_buffer.append(line_text_stripped)
                if state.pending_line_start is None and line_num is not None:
                    state.pending_line_start = line_num
                if line_num is not None:
                    state.pending_page = page_num

    return all_approps


def _flush_no_amount(state: ParsingState):
    """Clear the pending buffer without creating an item (e.g., when hitting a new section)."""
    state.pending_buffer = []
    state.pending_line_start = None


# ============================================================================
# VALIDATION
# ============================================================================

def validate_extraction(approps: List[Appropriation]):
    """Run validation checks on extracted appropriations."""
    print(f"\n{'='*80}")
    print("VALIDATION")
    print(f"{'='*80}")

    # Check 1: Total count
    print(f"\n  Total items extracted: {len(approps)}")
    with_id = sum(1 for a in approps if a.has_approp_id)
    without_id = sum(1 for a in approps if not a.has_approp_id)
    print(f"  With approp ID: {with_id}")
    print(f"  Without approp ID (bare amounts): {without_id}")

    # Check 2: Program distribution
    print(f"\n  By program:")
    from collections import Counter
    prog_counts = Counter(a.program for a in approps)
    for prog, count in prog_counts.most_common():
        short = prog[:50]
        print(f"    {short}: {count}")

    # Check 3: Fund distribution
    print(f"\n  By fund:")
    fund_counts = Counter(a.fund for a in approps)
    for fund, count in fund_counts.most_common():
        print(f"    {fund[:60]}: {count}")

    # Check 4: Known items spot-check
    print(f"\n  Spot checks:")
    known_ids = {
        '21713': ('ACCES', 54_000_000),
        '23462': ('ACCES', 750_000),
        '21856': ('ACCES', 16_000_000),
        '21854': ('ACCES', 1_000_000),
        '21846': ('CULTURAL', 104_600_000),
        '21831': ('HIGHER ED', 16_332_000),
    }
    for aid, (prog_hint, expected_amount) in known_ids.items():
        matches = [a for a in approps if a.approp_id == aid]
        if matches:
            a = matches[0]
            ok = '  OK' if a.approp_amount == expected_amount else f'  AMOUNT MISMATCH: got {a.approp_amount}'
            print(f"    ID {aid} ({prog_hint}): p{a.page_number} ${a.approp_amount:,}{ok}")
        else:
            print(f"    ID {aid} ({prog_hint}): NOT FOUND")

    # Check 5: Known bare-amount items
    print(f"\n  Bare-amount items:")
    known_bare = [
        ('BRIDGES', 50_000),
        ('public radio', 4_000_000),
        ('Brooklyn', 100_000),
        ('St. Bonaventure', 2_493_000),
        ('roll call vote', 150_000),
    ]
    for keyword, expected_amount in known_bare:
        matches = [a for a in approps
                   if not a.has_approp_id and keyword.lower() in a.bill_language.lower()]
        if matches:
            a = matches[0]
            ok = '  OK' if a.approp_amount == expected_amount else f'  AMOUNT MISMATCH: got {a.approp_amount}'
            print(f"    {keyword}: p{a.page_number} ${a.approp_amount:,}{ok}")
        else:
            # Try with amount match
            amount_matches = [a for a in approps
                              if not a.has_approp_id and a.approp_amount == expected_amount]
            if amount_matches:
                a = amount_matches[0]
                print(f"    {keyword}: p{a.page_number} ${a.approp_amount:,} (matched by amount, keyword not in text)")
            else:
                print(f"    {keyword} (${expected_amount:,}): NOT FOUND")

    # Check 6: No false positives — check for subtotals or program headers
    print(f"\n  False positive checks:")
    subtotal_items = [a for a in approps if 'subtotal' in a.bill_language.lower()]
    print(f"    Subtotal items (should be 0): {len(subtotal_items)}")
    for s in subtotal_items:
        print(f"      p{s.page_number}: {s.bill_language[:80]}")

    program_items = [a for a in approps
                     if any(prog in a.bill_language for prog in KNOWN_PROGRAMS)
                     or any(partial in a.bill_language for partial in PARTIAL_PROGRAM_HEADERS)]
    print(f"    Program header items (should be 0): {len(program_items)}")
    for p in program_items:
        print(f"      p{p.page_number}: {p.bill_language[:80]}")

    # Check 7: Page distribution
    print(f"\n  Page range: {min(a.page_number for a in approps)}-{max(a.page_number for a in approps)}")
    page_counts = Counter(a.page_number for a in approps)
    print(f"  Pages with items: {len(page_counts)}")

    # Total appropriation amount
    total = sum(a.approp_amount for a in approps)
    print(f"\n  Total appropriation amount: ${total:,.0f}")


# ============================================================================
# OUTPUT
# ============================================================================

def to_dataframe(approps: List[Appropriation]) -> pd.DataFrame:
    """Convert list of Appropriation objects to a DataFrame."""
    records = [asdict(a) for a in approps]
    return pd.DataFrame(records)


# ============================================================================
# MAIN
# ============================================================================

def main():
    base_dir = Path("/Users/samscott/Desktop/REAPPROPS")

    # The enacted ATL PDF containing appropriation pages 264-314
    # This is the LBDC PDF editor export of the Education section
    pdf_candidates = [
        base_dir / "25-26 SED ATL Approps pg264-314.pdf",
        Path("/Users/samscott/Downloads/25-26 SED ATL S03003D_A03003D_pg264-314-lbdc_pdf_editor-2026-02-24-1653.pdf"),
    ]

    pdf_path = None
    for p in pdf_candidates:
        if p.exists():
            pdf_path = p
            break

    if not pdf_path:
        # Try the full enacted ATL PDF
        full_pdf = base_dir / "Reapprops 26-27" / "ATL 25-26.pdf"
        if full_pdf.exists():
            pdf_path = full_pdf
        else:
            print("ERROR: No suitable PDF found.")
            print("Expected one of:")
            for p in pdf_candidates:
                print(f"  {p}")
            print(f"  {base_dir / 'Reapprops 26-27' / 'ATL 25-26.pdf'}")
            return

    print("=" * 80)
    print("NYS Budget Appropriation Extractor")
    print("=" * 80)
    print(f"\n  PDF: {pdf_path.name}")

    # Extract appropriations from Education pages 264-314
    print(f"\n>>> Extracting 25-26 Enacted Appropriations (p264-314)...")
    approps = extract_appropriations(str(pdf_path), start_page=264, end_page=314)
    print(f"  Extracted {len(approps)} appropriation items")

    # Validate
    validate_extraction(approps)

    # Save CSV
    df = to_dataframe(approps)
    output_path = base_dir / "enacted_25_26_approps.csv"
    df.to_csv(output_path, index=False)
    print(f"\n>>> Saved {output_path.name} ({len(df)} rows)")

    # Summary by page for review
    print(f"\n{'='*80}")
    print("PER-PAGE SUMMARY")
    print(f"{'='*80}")
    from collections import Counter
    page_counts = Counter(a.page_number for a in approps)
    for pg in sorted(page_counts.keys()):
        items = [a for a in approps if a.page_number == pg]
        ids = [a.approp_id for a in items if a.approp_id]
        bare = sum(1 for a in items if not a.has_approp_id)
        total = sum(a.approp_amount for a in items)
        id_str = ', '.join(ids[:5])
        if len(ids) > 5:
            id_str += f"... +{len(ids)-5} more"
        bare_str = f" + {bare} bare" if bare else ""
        print(f"  p{pg}: {len(items)} items ({len(ids)} with ID{bare_str}) ${total:,.0f}  [{id_str}]")


if __name__ == "__main__":
    main()

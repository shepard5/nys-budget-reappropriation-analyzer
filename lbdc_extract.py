"""
LBDC HTML-Based Reappropriation Extractor
==========================================
Extracts every reappropriation from Education ATL budget bills by
uploading PDFs via the LBDC API and parsing the resulting HTML.

Core principle: Every reappropriation ends with (re. $X).
That pattern is the ONE reliable anchor. We find every occurrence,
then walk backwards through accumulated buffer to collect attributes.

Validation targets:
  - 25-26 Enacted: 592 reappropriations
  - 26-27 Executive: 362 reappropriations
"""

import re
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple, Dict
from pathlib import Path

from lbdc_editor import LBDCClient, LBDCDocument


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Reappropriation:
    """A single reappropriation extracted from a budget bill HTML."""
    # Hierarchical location
    program: str              # e.g. "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM"
    fund: str                 # e.g. "General Fund; Local Assistance Account - 10000"
    chapter_year: int         # e.g. 2024

    # Amounts
    reapprop_amount: int      # The (re. $X) amount
    approp_amount: Optional[int] = None  # Original appropriation amount

    # Identification
    approp_id: Optional[str] = None  # 5-digit ID like "21713"

    # Language/text
    bill_language: str = ""   # Full text of the reappropriation
    chapter_citation: str = ""  # Full "By chapter..." text

    # Position in HTML — critical for insertion placement
    page_idx: int = 0         # 0-based page div index
    p_start: int = 0          # 0-based <p> index within page (first line of this reapprop)
    p_end: int = 0            # 0-based <p> index within page (line with (re.$))
    global_p_start: int = 0   # Global <p> index across all pages
    global_p_end: int = 0     # Global <p> index across all pages

    # Source
    source_file: str = ""


@dataclass
class StructuralElement:
    """A structural marker (program/fund/chapter_year header) with its HTML position."""
    elem_type: str            # 'program' | 'fund' | 'chapter_year'
    text: str                 # The header text
    program: str = ""
    fund: str = ""
    chapter_year: int = 0
    chapter_citation: str = ""
    amendment_type: str = "basic"  # basic | amended | added | transferred | directive
    amending_year: int = 0

    # Position
    page_idx: int = 0
    p_idx: int = 0            # <p> index within page
    global_p_idx: int = 0     # Global <p> index


@dataclass
class ExtractionResult:
    """Full result of extracting a bill."""
    reapprops: List[Reappropriation]
    structures: List[StructuralElement]
    html: str                 # The original HTML (kept for later editing)
    source_file: str = ""


@dataclass
class ParsingState:
    """Tracks hierarchical state during parsing."""
    program: str = ""
    fund_lines: List[str] = field(default_factory=list)
    fund: str = ""
    chapter_year: int = 0
    chapter_citation: str = ""

    # Buffer for accumulating bill language lines
    pending_buffer: List[str] = field(default_factory=list)
    pending_p_indices: List[Tuple[int, int]] = field(default_factory=list)  # (page_idx, p_idx) per line
    pending_global_indices: List[int] = field(default_factory=list)


# ============================================================================
# REGEX PATTERNS (reused from v8 extract_reapprops.py)
# ============================================================================

# The ONE anchor: (re. $X,XXX) or (re. $X,XXX,XXX)
RE_REAPPROP = re.compile(r'\(re\.\s*\$\s*([\d,]+)\s*\)')

# Chapter year headers
RE_CHAPTER_START = re.compile(r'^By\s+chapter\s+\d+', re.IGNORECASE)
RE_CHAPTER_YEAR = re.compile(r'of\s+the\s+laws\s+of\s+(\d{4})')

# Executive "amended and reappropriated" format
RE_AMENDED_REAPPROP_START = re.compile(
    r'^The\s+appropriation[s]?\s+made\s+by\s+chapter\s+\d+', re.IGNORECASE
)

# Amendment detection
RE_AMENDING_YEAR = re.compile(
    r'as\s+(?:amended|added|transferred)\s+by\s+chapter\s+\d+.*?of\s+the\s+laws\s+of\s+(\d{4})',
    re.IGNORECASE
)

# Appropriation ID: (XXXXX)
RE_APPROP_ID = re.compile(r'\((\d{5})\)')

# Dollar amounts with dot leaders
RE_AMOUNT = re.compile(r'([\d,]+)\s*\.+\s*\(re\.')
RE_AMOUNT_WITH_DOTS = re.compile(r'([\d,]+)\s*\.{2,}')

# Program headers
KNOWN_PROGRAMS = [
    "ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM",
    "CULTURAL EDUCATION PROGRAM",
    "OFFICE OF HIGHER EDUCATION AND THE PROFESSIONS PROGRAM",
    "OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION PROGRAM",
]

# Fund patterns
RE_FUND_TOP = re.compile(
    r'^(General Fund|Special Revenue Funds\s*-\s*Federal|Special Revenue Funds\s*-\s*Other)$',
    re.IGNORECASE
)
RE_ACCOUNT_LINE = re.compile(r'Account\s*-\s*\d{4,5}|Fund\s*-\s*\d{4,5}', re.IGNORECASE)

# Line number prefix
RE_LINE_NUM = re.compile(r'^(\d{1,2})\s+(.+)$')

# Page header patterns (to skip)
RE_PAGE_HEADER = re.compile(r'^\d{3}\s+\d{5}-\d{2}-\d$')
RE_DEPT_HEADER = re.compile(r'^EDUCATION DEPARTMENT$')
RE_ATL_HEADER = re.compile(r'^AID TO LOCALITIES\s*-\s*REAPPROPRIATIONS\s+\d{4}-\d{2}$')


# ============================================================================
# PARSING HELPERS
# ============================================================================

def parse_amount(amount_str: str) -> int:
    """Convert comma-formatted dollar amount string to int."""
    return int(amount_str.replace(',', ''))


def strip_line_number(text: str) -> Tuple[Optional[int], str]:
    """Extract line number prefix if present. Returns (line_num, remaining_text)."""
    m = RE_LINE_NUM.match(text)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 56:
            return num, m.group(2)
    return None, text


def is_header_line(line: str) -> bool:
    """Check if a line is a page header (not bill content)."""
    return bool(RE_PAGE_HEADER.match(line) or
                RE_DEPT_HEADER.match(line) or
                RE_ATL_HEADER.match(line))


def is_fund_header_line(text: str) -> bool:
    """Check if text is part of a fund header block."""
    text_stripped = text.strip()
    if RE_FUND_TOP.match(text_stripped):
        return True
    if re.match(r'^[A-Z][A-Za-z\s\-]+Fund$', text_stripped):
        return True
    if RE_ACCOUNT_LINE.search(text_stripped) and not text_stripped.startswith('By ') and not text_stripped.startswith('For '):
        return True
    return False


def build_fund_string(fund_lines: List[str]) -> str:
    """Build semicolon-separated fund string."""
    return "; ".join(line.strip() for line in fund_lines if line.strip())


def find_approp_id(text: str) -> Optional[str]:
    """Find 5-digit appropriation ID. Returns last found."""
    matches = RE_APPROP_ID.findall(text)
    return matches[-1] if matches else None


def find_approp_amount(text: str) -> Optional[int]:
    """Find the original appropriation amount from the text."""
    amounts = RE_AMOUNT_WITH_DOTS.findall(text)
    if amounts:
        re_match = RE_REAPPROP.search(text)
        if re_match:
            before_re = text[:re_match.start()]
            before_amounts = RE_AMOUNT_WITH_DOTS.findall(before_re)
            if before_amounts:
                return parse_amount(before_amounts[-1])

    m = RE_AMOUNT.search(text)
    if m:
        return parse_amount(m.group(1))
    return None


def parse_amendment_info(text: str) -> dict:
    """Parse amendment fields from chapter citation text."""
    t = text.lower().strip()
    is_directive = t.startswith('the appropriation')
    m = RE_AMENDING_YEAR.search(text)
    amending_year = int(m.group(1)) if m else 0

    if is_directive:
        return {'is_amended': True, 'amending_year': amending_year, 'amendment_type': 'directive'}
    elif 'as amended by' in t:
        return {'is_amended': True, 'amending_year': amending_year, 'amendment_type': 'amended'}
    elif 'as added by' in t:
        return {'is_amended': True, 'amending_year': amending_year, 'amendment_type': 'added'}
    elif 'as transferred by' in t:
        return {'is_amended': True, 'amending_year': amending_year, 'amendment_type': 'transferred'}
    else:
        return {'is_amended': False, 'amending_year': 0, 'amendment_type': 'basic'}


# ============================================================================
# MAIN EXTRACTION
# ============================================================================

def extract_from_html(html: str, source_file: str = "") -> ExtractionResult:
    """
    Extract all reappropriations from LBDC HTML.

    Each <p> tag in the HTML = one line of the bill. We iterate through
    all pages and lines, maintaining hierarchical state, and emit a
    Reappropriation every time we hit a (re. $X) anchor.

    Also tracks structural elements (program/fund/chapter_year headers)
    with their HTML positions for later insertion placement.
    """
    doc = LBDCDocument(html)
    pages = doc.get_pages()

    all_reapprops: List[Reappropriation] = []
    all_structures: List[StructuralElement] = []
    state = ParsingState()

    global_p = 0  # Running count across all pages

    for page_idx, page in enumerate(pages):
        lines = page.find_all("p")

        for p_idx, p_tag in enumerate(lines):
            line_text = p_tag.get_text()
            current_global_p = global_p + p_idx

            # Skip blank lines
            if not line_text.strip():
                continue

            # Strip line number prefix (bill lines start with " 1 ", "10 ", etc.)
            _line_num, content_text = strip_line_number(line_text.strip())
            content_stripped = content_text.strip()

            if not content_stripped:
                continue

            # Skip page headers
            if is_header_line(content_stripped):
                continue

            # ── CHECK 1: Program header ──
            is_program = False
            for prog in KNOWN_PROGRAMS:
                if content_stripped == prog:
                    state.program = prog
                    state.fund_lines = []
                    state.fund = ""
                    state.chapter_year = 0
                    state.chapter_citation = ""
                    is_program = True

                    all_structures.append(StructuralElement(
                        elem_type='program',
                        text=prog,
                        program=prog,
                        page_idx=page_idx,
                        p_idx=p_idx,
                        global_p_idx=current_global_p,
                    ))
                    break

            if is_program:
                continue

            # ── CHECK 2: Fund header ──
            if is_fund_header_line(content_stripped):
                if RE_FUND_TOP.match(content_stripped):
                    state.fund_lines = [content_stripped]
                else:
                    state.fund_lines.append(content_stripped)

                if RE_ACCOUNT_LINE.search(content_stripped):
                    state.fund = build_fund_string(state.fund_lines)
                    state.chapter_year = 0
                    state.chapter_citation = ""

                    all_structures.append(StructuralElement(
                        elem_type='fund',
                        text=state.fund,
                        program=state.program,
                        fund=state.fund,
                        page_idx=page_idx,
                        p_idx=p_idx,
                        global_p_idx=current_global_p,
                    ))
                continue

            # ── CHECK 3: Chapter year header ──
            is_chapter_header = RE_CHAPTER_START.match(content_stripped)
            is_amended_header = RE_AMENDED_REAPPROP_START.match(content_stripped)

            if is_chapter_header or is_amended_header:
                year_match = RE_CHAPTER_YEAR.search(content_stripped)
                if year_match:
                    state.chapter_year = int(year_match.group(1))
                state.chapter_citation = content_stripped

                if content_stripped.rstrip().endswith(':'):
                    # Complete citation on one line
                    amend_info = parse_amendment_info(state.chapter_citation)
                    all_structures.append(StructuralElement(
                        elem_type='chapter_year',
                        text=state.chapter_citation,
                        program=state.program,
                        fund=state.fund,
                        chapter_year=state.chapter_year,
                        chapter_citation=state.chapter_citation,
                        amendment_type=amend_info['amendment_type'],
                        amending_year=amend_info['amending_year'],
                        page_idx=page_idx,
                        p_idx=p_idx,
                        global_p_idx=current_global_p,
                    ))
                continue

            # ── CHECK 3b: Continuation of chapter citation ──
            if state.chapter_citation and not state.chapter_citation.rstrip().endswith(':'):
                state.chapter_citation += " " + content_stripped
                year_match = RE_CHAPTER_YEAR.search(state.chapter_citation)
                if year_match:
                    state.chapter_year = int(year_match.group(1))

                if content_stripped.rstrip().endswith(':') or 'to read' in content_stripped.lower():
                    amend_info = parse_amendment_info(state.chapter_citation)
                    all_structures.append(StructuralElement(
                        elem_type='chapter_year',
                        text=state.chapter_citation,
                        program=state.program,
                        fund=state.fund,
                        chapter_year=state.chapter_year,
                        chapter_citation=state.chapter_citation,
                        amendment_type=amend_info['amendment_type'],
                        amending_year=amend_info['amending_year'],
                        page_idx=page_idx,
                        p_idx=p_idx,
                        global_p_idx=current_global_p,
                    ))
                continue

            # ── CHECK 4: (re. $X) anchor — emit reappropriation ──
            re_matches = list(RE_REAPPROP.finditer(content_stripped))

            if re_matches:
                for re_match in re_matches:
                    reapprop_amount = parse_amount(re_match.group(1))

                    # Build full text
                    full_text_parts = list(state.pending_buffer)
                    full_text_parts.append(content_stripped)
                    full_text = "\n".join(full_text_parts)

                    # Get position info
                    if state.pending_p_indices:
                        start_page, start_p = state.pending_p_indices[0]
                        start_global = state.pending_global_indices[0]
                    else:
                        start_page = page_idx
                        start_p = p_idx
                        start_global = current_global_p

                    reapprop = Reappropriation(
                        program=state.program,
                        fund=state.fund,
                        chapter_year=state.chapter_year,
                        reapprop_amount=reapprop_amount,
                        approp_amount=find_approp_amount(full_text),
                        approp_id=find_approp_id(full_text),
                        bill_language=full_text,
                        chapter_citation=state.chapter_citation,
                        page_idx=start_page,
                        p_start=start_p,
                        p_end=p_idx,
                        global_p_start=start_global,
                        global_p_end=current_global_p,
                        source_file=source_file,
                    )
                    all_reapprops.append(reapprop)

                # Reset buffer
                state.pending_buffer = []
                state.pending_p_indices = []
                state.pending_global_indices = []

            else:
                # Accumulate as bill language
                state.pending_buffer.append(content_stripped)
                state.pending_p_indices.append((page_idx, p_idx))
                state.pending_global_indices.append(current_global_p)

        # Update global_p counter for next page
        global_p += len(lines)

    return ExtractionResult(
        reapprops=all_reapprops,
        structures=all_structures,
        html=html,
        source_file=source_file,
    )


# ============================================================================
# UPLOAD + EXTRACT CONVENIENCE
# ============================================================================

def upload_and_extract(pdf_path: str, client: LBDCClient = None) -> ExtractionResult:
    """Upload PDF via LBDC API and extract reappropriations."""
    if client is None:
        client = LBDCClient()

    source_file = Path(pdf_path).name
    print(f"\n>>> Uploading {source_file}...")
    html = client.upload_pdf(pdf_path)
    print(f"    Got {len(html)} chars of HTML")

    result = extract_from_html(html, source_file)
    print(f"    Extracted {len(result.reapprops)} reappropriations")
    print(f"    Found {len(result.structures)} structural elements")

    # Quick stats
    programs = set(r.program for r in result.reapprops)
    funds = set(r.fund for r in result.reapprops)
    chyrs = sorted(set(r.chapter_year for r in result.reapprops))
    with_id = sum(1 for r in result.reapprops if r.approp_id)

    print(f"    Programs: {len(programs)}")
    print(f"    Funds: {len(funds)}")
    print(f"    Chapter years: {chyrs}")
    print(f"    With approp ID: {with_id}, without: {len(result.reapprops) - with_id}")

    return result


# ============================================================================
# REPORTING
# ============================================================================

def print_extraction_report(result: ExtractionResult):
    """Print detailed extraction report."""
    print(f"\n{'='*80}")
    print(f"EXTRACTION REPORT: {result.source_file}")
    print(f"{'='*80}")
    print(f"  Total reappropriations: {len(result.reapprops)}")

    # By program
    from collections import Counter
    prog_counts = Counter(r.program for r in result.reapprops)
    print(f"\n  By Program:")
    for prog, count in prog_counts.most_common():
        print(f"    {prog[:60]}: {count}")

    # By fund
    fund_counts = Counter(r.fund for r in result.reapprops)
    print(f"\n  By Fund:")
    for fund, count in fund_counts.most_common():
        print(f"    {fund[:60]}: {count}")

    # By chapter year
    chyr_counts = Counter(r.chapter_year for r in result.reapprops)
    print(f"\n  By Chapter Year:")
    for chyr, count in sorted(chyr_counts.items()):
        print(f"    {chyr}: {count}")

    # Dollar totals
    total_reapprop = sum(r.reapprop_amount for r in result.reapprops)
    print(f"\n  Total reapprop amount: ${total_reapprop:,.0f}")


def to_dataframe(reapprops: List[Reappropriation]):
    """Convert to pandas DataFrame (if pandas available)."""
    try:
        import pandas as pd
        records = [asdict(r) for r in reapprops]
        return pd.DataFrame(records)
    except ImportError:
        print("pandas not available — returning raw dicts")
        return [asdict(r) for r in reapprops]


# ============================================================================
# CLI
# ============================================================================

def main():
    """Extract reappropriations from one or two PDFs."""
    if len(sys.argv) < 2:
        print("""
LBDC HTML-Based Reappropriation Extractor
==========================================
Usage:
  python lbdc_extract.py <pdf_path>                     Extract from one PDF
  python lbdc_extract.py <enacted_pdf> <executive_pdf>  Extract from both
""")
        return

    client = LBDCClient()

    if len(sys.argv) >= 3:
        # Two PDFs — extract both
        enacted_result = upload_and_extract(sys.argv[1], client)
        exec_result = upload_and_extract(sys.argv[2], client)

        print_extraction_report(enacted_result)
        print_extraction_report(exec_result)

        # Save CSVs
        try:
            import pandas as pd
            base = Path(sys.argv[1]).parent

            enacted_df = to_dataframe(enacted_result.reapprops)
            exec_df = to_dataframe(exec_result.reapprops)

            enacted_df.to_csv(base / "enacted_reapprops.csv", index=False)
            exec_df.to_csv(base / "executive_reapprops.csv", index=False)
            print(f"\n>>> Saved enacted_reapprops.csv ({len(enacted_df)} rows)")
            print(f">>> Saved executive_reapprops.csv ({len(exec_df)} rows)")
        except ImportError:
            print("pandas not available — skipping CSV export")

        # Save HTML for later use
        (base / "enacted.html").write_text(enacted_result.html, encoding='utf-8')
        (base / "executive.html").write_text(exec_result.html, encoding='utf-8')
        print(">>> Saved enacted.html and executive.html")

    else:
        # Single PDF
        result = upload_and_extract(sys.argv[1], client)
        print_extraction_report(result)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
NYS Budget Reappropriation Analysis Tool
Consolidated single-script implementation for near 100% accuracy.

Identifies spending authority NOT reappropriated in subsequent NYS Executive
budget proposals by comparing enacted budget (prior year) against executive
budget (current year proposal).

Core Logic: Items from enacted budget (both appropriations AND reappropriations)
should reappear as reappropriations in the executive budget. Missing items =
discontinued spending authority.

Usage:
    python nys_budget_analyzer.py <enacted_pdf> <executive_pdf> [--output-dir DIR]

Example:
    python nys_budget_analyzer.py "2025_Enacted.pdf" "2026_Executive.pdf" --output-dir ./results
"""

import pdfplumber
import pandas as pd
import re
import json
import argparse
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set
from pathlib import Path
from collections import defaultdict
from datetime import datetime


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class BudgetRecord:
    """Single budget line item record."""
    # Primary identifiers
    agency: str
    appropriation_id: str
    chapter_year: str  # Year from "By chapter X, section Y, of the laws of YYYY"

    # Amounts
    appropriation_amount: int  # Original appropriation in dollars
    reappropriation_amount: int  # Reappropriation amount in dollars

    # Classification
    record_type: str  # "appropriation" or "reappropriation"
    budget_type: str  # "STATE OPERATIONS", "AID TO LOCALITIES", "CAPITAL PROJECTS"
    fund_type: str  # "General Fund", "Special Revenue Funds - Federal", etc.
    account: str  # "State Purposes Account - 10050"
    fiscal_year: str  # "2024-25"

    # Source tracking
    page_number: int
    line_number: Optional[int]
    bill_language: str  # Full description text
    raw_line: str  # Original line containing amounts
    source_file: str
    source_budget: str  # "enacted" or "executive"

    # Chapter citation details
    chapter_number: Optional[str] = None
    section_number: Optional[str] = None

    def composite_key(self) -> str:
        """Generate unique composite key for deduplication and matching.

        Key components: agency|appropriation_id|chapter_year|appropriation_amount

        Note: Account is NOT included because the same appropriation can appear
        under different account names between enacted and executive budgets.
        Appropriation amount IS included because the same ID can have multiple
        line items with different original amounts.
        """
        return f"{self._normalize(self.agency)}|{self.appropriation_id}|{self.chapter_year}|{self.appropriation_amount}"

    def _normalize(self, text: str) -> str:
        """Normalize text for consistent matching."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text.upper().strip())


@dataclass
class ParsingContext:
    """Tracks parsing state across pages."""
    agency: str = "Unknown"
    budget_type: str = "Unknown"
    fund_type: str = "Unknown"
    account: str = "Unknown"
    fiscal_year: str = "Unknown"
    chapter_year: str = "Unknown"
    chapter_number: str = ""
    section_number: str = ""
    is_reappropriation_section: bool = False


@dataclass
class ComparisonResult:
    """Result of comparing enacted vs executive budgets."""
    enacted_record: BudgetRecord
    status: str  # "discontinued", "continued", "modified"
    executive_match: Optional[BudgetRecord] = None
    amount_difference: Optional[int] = None


@dataclass
class ComparisonResults:
    """Container for all comparison results."""
    discontinued: List[ComparisonResult] = field(default_factory=list)
    continued: List[ComparisonResult] = field(default_factory=list)
    modified: List[ComparisonResult] = field(default_factory=list)
    all_enacted: List[BudgetRecord] = field(default_factory=list)
    all_executive: List[BudgetRecord] = field(default_factory=list)


# =============================================================================
# REGEX PATTERNS
# =============================================================================

class BudgetPatterns:
    """Compiled regex patterns for budget document parsing."""

    def __init__(self):
        # Reappropriation amount extraction
        # Matches: "3,933,000 ... (re. $3,851,000)" or just "(re. $3,851,000)"
        self.REAPPROP_FULL = re.compile(
            r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*\.{3,}\s*\(re\.\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\)'
        )

        # Just the reapprop marker
        self.REAPPROP_MARKER = re.compile(
            r'\(re\.\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\)'
        )

        # Appropriation ID (5-digit code in parentheses)
        self.APPROP_ID = re.compile(r'\((\d{5})\)')

        # Alternative appropriation ID formats
        self.APPROP_ID_UNDERLINE = re.compile(r'\(_(\d)_(\d)_(\d)_(\d)_(\d)_\)')  # Underlined format
        self.APPROP_ID_BRACKET = re.compile(r'\[(\d{5})\]')

        # Chapter/Year citation (AUTHORITATIVE year source)
        self.CHAPTER_LAW = re.compile(
            r'By\s+chapter\s+(\d+),\s*section\s+(\d+),?\s*of\s+the\s+laws\s+of\s+(\d{4})',
            re.IGNORECASE
        )

        # Budget type header
        self.BUDGET_TYPE = re.compile(
            r'^(STATE OPERATIONS|AID TO LOCALITIES|CAPITAL PROJECTS)\s*-?\s*(REAPPROPRIATIONS|APPROPRIATIONS)?\s*(\d{4}-\d{2})?',
            re.IGNORECASE | re.MULTILINE
        )

        # Agency name (ALL CAPS, 10+ chars)
        self.AGENCY = re.compile(r'^([A-Z][A-Z\s,\-&\.]{8,}[A-Z])$', re.MULTILINE)

        # Words to exclude from agency detection
        self.AGENCY_EXCLUDE = {
            'GENERAL FUND', 'SPECIAL REVENUE FUNDS', 'CAPITAL PROJECTS FUND',
            'FIDUCIARY FUNDS', 'APPROPRIATIONS', 'REAPPROPRIATIONS',
            'STATE OPERATIONS', 'AID TO LOCALITIES', 'CAPITAL PROJECTS',
            'FEDERAL', 'OTHER', 'SCHEDULE', 'BUDGET'
        }

        # Line number prefix
        self.LINE_PREFIX = re.compile(r'^(\d{1,2})\s+(.+)$')

        # Account pattern
        self.ACCOUNT = re.compile(
            r'([A-Za-z][A-Za-z\s\-]+Account\s*-\s*\d{5})',
            re.IGNORECASE
        )

        # Fund type
        self.FUND_TYPE = re.compile(
            r'(General Fund|Special Revenue Funds?\s*-\s*Federal|Special Revenue Funds?\s*-\s*Other|'
            r'Capital Projects Fund|Capital Projects Funds?\s*-\s*Other|Fiduciary Funds?|'
            r'Federal Education Fund|Federal Health and Human Services)',
            re.IGNORECASE
        )

        # Fiscal year
        self.FISCAL_YEAR = re.compile(r'(\d{4})-(\d{2})')

        # Page header (page number pattern)
        self.PAGE_HEADER = re.compile(r'^(\d+)\s+\d+-\d+-\d+$')

        # New appropriation (no reapprop marker) - for comparison
        self.NEW_APPROP = re.compile(
            r'\((\d{5})\)\s*\.{3,}\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)(?!\s*\.{3,}\s*\(re\.)'
        )


# =============================================================================
# PDF EXTRACTION
# =============================================================================

class PDFExtractor:
    """Extracts budget records from PDF files."""

    def __init__(self):
        self.patterns = BudgetPatterns()

    def extract_records(self, pdf_path: Path, source_budget: str) -> List[BudgetRecord]:
        """
        Extract all budget records from a PDF.

        Args:
            pdf_path: Path to the PDF file
            source_budget: "enacted" or "executive"

        Returns:
            List of BudgetRecord objects
        """
        records = []
        context = ParsingContext()
        seen_keys: Set[str] = set()  # For deduplication

        print(f"  Opening PDF: {pdf_path.name}")

        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            print(f"  Total pages: {total_pages}")

            for page_num, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if not text:
                    continue

                # Update context from page header
                context = self._update_context_from_page(text, context)

                # Extract records from this page
                page_records = self._extract_page_records(
                    text, context, page_num, pdf_path.name, source_budget
                )

                # Deduplicate as we go
                for record in page_records:
                    key = record.composite_key()
                    if key not in seen_keys:
                        seen_keys.add(key)
                        records.append(record)

                # Progress indicator
                if page_num % 100 == 0:
                    print(f"    Progress: {page_num}/{total_pages} pages ({page_num/total_pages*100:.1f}%)")

        print(f"  Extracted {len(records)} unique records")
        return records

    def _update_context_from_page(self, page_text: str, context: ParsingContext) -> ParsingContext:
        """Update parsing context based on page content."""
        lines = page_text.splitlines()

        # Check first few lines for header info
        for i, line in enumerate(lines[:5]):
            line = line.strip()

            # Budget type detection
            budget_match = self.patterns.BUDGET_TYPE.search(line)
            if budget_match:
                context.budget_type = budget_match.group(1).upper()
                context.is_reappropriation_section = 'REAPPROPRIATION' in line.upper()
                if budget_match.group(3):
                    context.fiscal_year = budget_match.group(3)

            # Agency detection (usually line 2)
            if i == 1 and line:
                agency_match = self.patterns.AGENCY.match(line)
                if agency_match:
                    agency = agency_match.group(1).strip()
                    if not any(excl in agency for excl in self.patterns.AGENCY_EXCLUDE):
                        context.agency = agency

        # Scan full page for fund type and account
        fund_match = self.patterns.FUND_TYPE.search(page_text)
        if fund_match:
            context.fund_type = fund_match.group(1).strip()

        account_match = self.patterns.ACCOUNT.search(page_text)
        if account_match:
            context.account = account_match.group(1).strip()

        return context

    def _extract_page_records(
        self,
        page_text: str,
        context: ParsingContext,
        page_num: int,
        source_file: str,
        source_budget: str
    ) -> List[BudgetRecord]:
        """Extract budget records from a single page."""
        records = []

        # Split page into chapter-based chunks for reappropriations
        if context.is_reappropriation_section:
            records.extend(self._extract_reappropriation_records(
                page_text, context, page_num, source_file, source_budget
            ))
        else:
            # Extract appropriations
            records.extend(self._extract_appropriation_records(
                page_text, context, page_num, source_file, source_budget
            ))

        return records

    def _extract_reappropriation_records(
        self,
        page_text: str,
        context: ParsingContext,
        page_num: int,
        source_file: str,
        source_budget: str
    ) -> List[BudgetRecord]:
        """Extract reappropriation records from page text."""
        records = []
        lines = page_text.splitlines()

        # Track current chapter context and text buffer
        current_chapter_year = context.chapter_year
        current_chapter_num = context.chapter_number
        current_section_num = context.section_number
        text_buffer = []
        start_line_num = None

        for line in lines:
            original_line = line
            line_stripped = line.strip()

            # Extract line number if present
            line_num = None
            line_match = self.patterns.LINE_PREFIX.match(line_stripped)
            if line_match:
                line_num = int(line_match.group(1))
                line_stripped = line_match.group(2)

            # Check for chapter citation (starts new chunk)
            chapter_match = self.patterns.CHAPTER_LAW.search(line_stripped)
            if chapter_match:
                current_chapter_num = chapter_match.group(1)
                current_section_num = chapter_match.group(2)
                current_chapter_year = chapter_match.group(3)

                # If we had a previous buffer with reapprop, save it first
                if text_buffer:
                    record = self._create_reapprop_record_from_buffer(
                        text_buffer, context, current_chapter_year,
                        current_chapter_num, current_section_num,
                        page_num, start_line_num, source_file, source_budget
                    )
                    if record:
                        records.append(record)

                # Start new buffer
                text_buffer = [original_line]
                start_line_num = line_num
                continue

            # Check for reappropriation marker
            reapprop_match = self.patterns.REAPPROP_MARKER.search(line_stripped)
            if reapprop_match:
                text_buffer.append(original_line)

                # Create record from buffer
                record = self._create_reapprop_record_from_buffer(
                    text_buffer, context, current_chapter_year,
                    current_chapter_num, current_section_num,
                    page_num, start_line_num, source_file, source_budget
                )
                if record:
                    records.append(record)

                # Reset buffer (but keep chapter context)
                text_buffer = []
                start_line_num = None
            else:
                # Accumulate text
                text_buffer.append(original_line)
                if start_line_num is None and line_num:
                    start_line_num = line_num

        # Update context for next page
        context.chapter_year = current_chapter_year
        context.chapter_number = current_chapter_num
        context.section_number = current_section_num

        return records

    def _create_reapprop_record_from_buffer(
        self,
        text_buffer: List[str],
        context: ParsingContext,
        chapter_year: str,
        chapter_num: str,
        section_num: str,
        page_num: int,
        start_line_num: Optional[int],
        source_file: str,
        source_budget: str
    ) -> Optional[BudgetRecord]:
        """Create a BudgetRecord from accumulated text buffer."""
        if not text_buffer:
            return None

        full_text = '\n'.join(text_buffer)

        # Find reappropriation amounts
        reapprop_match = self.patterns.REAPPROP_FULL.search(full_text)
        if not reapprop_match:
            # Try just the marker
            marker_match = self.patterns.REAPPROP_MARKER.search(full_text)
            if not marker_match:
                return None
            reapprop_amount = self._parse_amount(marker_match.group(1))
            approp_amount = reapprop_amount  # Use same as fallback
        else:
            approp_amount = self._parse_amount(reapprop_match.group(1))
            reapprop_amount = self._parse_amount(reapprop_match.group(2))

        # Find appropriation ID
        approp_id = self._find_approp_id(full_text)
        if not approp_id:
            return None  # Skip records without ID

        # Find the line containing the amounts for raw_line
        raw_line = ""
        for line in text_buffer:
            if self.patterns.REAPPROP_MARKER.search(line):
                raw_line = line.strip()
                break

        # Check for account in buffer
        account = context.account
        account_match = self.patterns.ACCOUNT.search(full_text)
        if account_match:
            account = account_match.group(1)

        # Check for fund type in buffer
        fund_type = context.fund_type
        fund_match = self.patterns.FUND_TYPE.search(full_text)
        if fund_match:
            fund_type = fund_match.group(1)

        return BudgetRecord(
            agency=context.agency,
            appropriation_id=approp_id,
            chapter_year=chapter_year if chapter_year else context.chapter_year,
            appropriation_amount=approp_amount,
            reappropriation_amount=reapprop_amount,
            record_type='reappropriation',
            budget_type=context.budget_type,
            fund_type=fund_type,
            account=account,
            fiscal_year=context.fiscal_year,
            page_number=page_num,
            line_number=start_line_num,
            bill_language=full_text.strip(),
            raw_line=raw_line,
            source_file=source_file,
            source_budget=source_budget,
            chapter_number=chapter_num,
            section_number=section_num
        )

    def _extract_appropriation_records(
        self,
        page_text: str,
        context: ParsingContext,
        page_num: int,
        source_file: str,
        source_budget: str
    ) -> List[BudgetRecord]:
        """Extract appropriation (non-reappropriation) records."""
        records = []
        lines = page_text.splitlines()
        text_buffer = []
        start_line_num = None

        for line in lines:
            original_line = line
            line_stripped = line.strip()

            # Extract line number
            line_num = None
            line_match = self.patterns.LINE_PREFIX.match(line_stripped)
            if line_match:
                line_num = int(line_match.group(1))
                line_stripped = line_match.group(2)

            # Check for appropriation pattern: (XXXXX) ... amount
            approp_match = self.patterns.NEW_APPROP.search(line_stripped)
            if approp_match:
                approp_id = approp_match.group(1)
                amount = self._parse_amount(approp_match.group(2))

                # Check for account in buffer or line
                account = context.account
                account_match = self.patterns.ACCOUNT.search(line_stripped)
                if account_match:
                    account = account_match.group(1)

                record = BudgetRecord(
                    agency=context.agency,
                    appropriation_id=approp_id,
                    chapter_year=context.fiscal_year[:4] if context.fiscal_year != "Unknown" else "Unknown",
                    appropriation_amount=amount,
                    reappropriation_amount=0,
                    record_type='appropriation',
                    budget_type=context.budget_type,
                    fund_type=context.fund_type,
                    account=account,
                    fiscal_year=context.fiscal_year,
                    page_number=page_num,
                    line_number=line_num,
                    bill_language=' '.join(text_buffer + [original_line]).strip(),
                    raw_line=line_stripped,
                    source_file=source_file,
                    source_budget=source_budget
                )
                records.append(record)
                text_buffer = []
                start_line_num = None
            else:
                text_buffer.append(original_line)
                if start_line_num is None and line_num:
                    start_line_num = line_num

        return records

    def _find_approp_id(self, text: str) -> Optional[str]:
        """Find appropriation ID in text, handling various formats."""
        # Try standard format first
        match = self.patterns.APPROP_ID.search(text)
        if match:
            return match.group(1)

        # Try underlined format: (_3_0_0_1_2_)
        underline_match = self.patterns.APPROP_ID_UNDERLINE.search(text)
        if underline_match:
            return ''.join(underline_match.groups())

        # Try bracket format
        bracket_match = self.patterns.APPROP_ID_BRACKET.search(text)
        if bracket_match:
            return bracket_match.group(1)

        return None

    def _parse_amount(self, amount_str: str) -> int:
        """Parse amount string to integer dollars."""
        if not amount_str:
            return 0
        cleaned = amount_str.replace(',', '').replace('$', '')
        try:
            return int(float(cleaned))
        except ValueError:
            return 0


# =============================================================================
# DEDUPLICATION
# =============================================================================

class DeduplicationEngine:
    """Handles deduplication of budget records."""

    def deduplicate(self, records: List[BudgetRecord]) -> List[BudgetRecord]:
        """
        Remove duplicate records, keeping the most complete version.

        Args:
            records: List of potentially duplicate records

        Returns:
            Deduplicated list of records
        """
        seen: Dict[str, BudgetRecord] = {}

        for record in records:
            key = record.composite_key()

            if key not in seen:
                seen[key] = record
            else:
                # Keep the better record
                existing = seen[key]
                if self._is_better_record(record, existing):
                    seen[key] = record

        return list(seen.values())

    def _is_better_record(self, new: BudgetRecord, existing: BudgetRecord) -> bool:
        """Determine if new record is more complete than existing."""
        new_score = self._quality_score(new)
        existing_score = self._quality_score(existing)
        return new_score > existing_score

    def _quality_score(self, record: BudgetRecord) -> int:
        """Calculate quality score for a record."""
        score = 0

        # Has key fields
        if record.agency and record.agency != "Unknown":
            score += 10
        if record.chapter_year and record.chapter_year != "Unknown":
            score += 10
        if record.account and record.account != "Unknown":
            score += 5
        if record.fund_type and record.fund_type != "Unknown":
            score += 5
        if record.line_number:
            score += 3

        # Bill language completeness
        if record.bill_language:
            score += min(len(record.bill_language) // 100, 10)

        # Has amounts
        if record.appropriation_amount > 0:
            score += 5
        if record.reappropriation_amount > 0:
            score += 5

        return score


# =============================================================================
# BUDGET COMPARISON
# =============================================================================

class BudgetComparator:
    """Compares enacted vs executive budgets to find discontinued items."""

    def compare(
        self,
        enacted_records: List[BudgetRecord],
        executive_records: List[BudgetRecord]
    ) -> ComparisonResults:
        """
        Compare enacted budget against executive budget.

        Items from enacted budget should appear as reappropriations in executive.
        Missing items = discontinued spending authority.
        """
        results = ComparisonResults()
        results.all_enacted = enacted_records
        results.all_executive = executive_records

        # Build lookup of executive reappropriations
        exec_reapprops: Dict[str, BudgetRecord] = {}
        for record in executive_records:
            if record.record_type == 'reappropriation':
                key = record.composite_key()
                exec_reapprops[key] = record

        print(f"  Executive reappropriations: {len(exec_reapprops)}")

        # Compare each enacted record against executive
        for enacted in enacted_records:
            key = enacted.composite_key()

            if key in exec_reapprops:
                exec_record = exec_reapprops[key]

                # Check if amounts match
                if enacted.reappropriation_amount == exec_record.reappropriation_amount:
                    results.continued.append(ComparisonResult(
                        enacted_record=enacted,
                        status='continued',
                        executive_match=exec_record
                    ))
                else:
                    results.modified.append(ComparisonResult(
                        enacted_record=enacted,
                        status='modified',
                        executive_match=exec_record,
                        amount_difference=exec_record.reappropriation_amount - enacted.reappropriation_amount
                    ))
            else:
                results.discontinued.append(ComparisonResult(
                    enacted_record=enacted,
                    status='discontinued'
                ))

        return results


# =============================================================================
# REPORT GENERATION
# =============================================================================

class ReportGenerator:
    """Generates output reports."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_all_reports(self, results: ComparisonResults) -> Dict[str, Path]:
        """Generate all output reports."""
        outputs = {}

        # Separate discontinued into appropriations vs reappropriations
        disc_reapprops = [r for r in results.discontinued
                         if r.enacted_record.reappropriation_amount > 0]
        disc_approps = [r for r in results.discontinued
                       if r.enacted_record.reappropriation_amount == 0]

        # Main discrepancy report (all discontinued)
        outputs['discontinued'] = self._write_discontinued_csv(
            results.discontinued, 'discontinued_all.csv'
        )

        # Separate files for reappropriations and appropriations
        outputs['disc_reappropriations'] = self._write_discontinued_csv(
            disc_reapprops, 'discontinued_reappropriations.csv'
        )
        outputs['disc_appropriations'] = self._write_discontinued_csv(
            disc_approps, 'discontinued_appropriations.csv'
        )

        # Raw data exports
        outputs['enacted_data'] = self._write_records_csv(
            results.all_enacted, 'enacted_budget_data.csv'
        )
        outputs['executive_data'] = self._write_records_csv(
            results.all_executive, 'executive_budget_data.csv'
        )

        # Summary statistics
        outputs['summary'] = self._write_summary_json(results)

        # Verification report
        outputs['verification'] = self._write_verification_report(results)

        return outputs

    def _write_discontinued_csv(self, discontinued: List[ComparisonResult], filename: str = 'discontinued_reappropriations.csv') -> Path:
        """Write discrepancy report to specified file."""
        path = self.output_dir / filename

        rows = []
        for result in discontinued:
            r = result.enacted_record
            rows.append({
                'agency': r.agency,
                'budget_type': r.budget_type,
                'fund_type': r.fund_type,
                'account': r.account,
                'appropriation_id': r.appropriation_id,
                'chapter_year': r.chapter_year,
                'appropriation_amount': r.appropriation_amount,
                'reappropriation_amount': r.reappropriation_amount,
                'bill_language': r.bill_language,
                'page_number': r.page_number,
                'line_number': r.line_number,
                'fiscal_year': r.fiscal_year,
                'source_file': r.source_file,
                'composite_key': r.composite_key()
            })

        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
        return path

    def _write_records_csv(self, records: List[BudgetRecord], filename: str) -> Path:
        """Write raw records to CSV."""
        path = self.output_dir / filename

        rows = []
        for r in records:
            rows.append({
                'agency': r.agency,
                'budget_type': r.budget_type,
                'fund_type': r.fund_type,
                'account': r.account,
                'appropriation_id': r.appropriation_id,
                'chapter_year': r.chapter_year,
                'appropriation_amount': r.appropriation_amount,
                'reappropriation_amount': r.reappropriation_amount,
                'record_type': r.record_type,
                'page_number': r.page_number,
                'line_number': r.line_number,
                'fiscal_year': r.fiscal_year,
                'raw_line': r.raw_line,
                'source_file': r.source_file,
                'composite_key': r.composite_key()
            })

        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
        return path

    def _write_summary_json(self, results: ComparisonResults) -> Path:
        """Write summary statistics."""
        path = self.output_dir / 'analysis_summary.json'

        # Calculate totals
        discontinued_total = sum(r.enacted_record.reappropriation_amount
                                  for r in results.discontinued)
        continued_total = sum(r.enacted_record.reappropriation_amount
                               for r in results.continued)

        # Group by agency
        agency_totals = defaultdict(lambda: {'count': 0, 'amount': 0})
        for result in results.discontinued:
            agency = result.enacted_record.agency
            agency_totals[agency]['count'] += 1
            agency_totals[agency]['amount'] += result.enacted_record.reappropriation_amount

        summary = {
            'generated_at': datetime.now().isoformat(),
            'totals': {
                'enacted_records': len(results.all_enacted),
                'executive_records': len(results.all_executive),
                'discontinued': len(results.discontinued),
                'continued': len(results.continued),
                'modified': len(results.modified),
                'discontinued_amount': discontinued_total,
                'continued_amount': continued_total
            },
            'by_agency': dict(sorted(
                agency_totals.items(),
                key=lambda x: x[1]['amount'],
                reverse=True
            )[:20])  # Top 20 agencies
        }

        with open(path, 'w') as f:
            json.dump(summary, f, indent=2)

        return path

    def _write_verification_report(self, results: ComparisonResults) -> Path:
        """Write human-readable verification report."""
        path = self.output_dir / 'verification_report.txt'

        lines = [
            "=" * 80,
            "NYS BUDGET REAPPROPRIATION ANALYSIS - VERIFICATION REPORT",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 80,
            "",
            "EXTRACTION SUMMARY",
            "-" * 40,
            f"Enacted Records Extracted: {len(results.all_enacted):,}",
            f"Executive Records Extracted: {len(results.all_executive):,}",
            "",
            "COMPARISON RESULTS",
            "-" * 40,
            f"Discontinued (Missing in Executive): {len(results.discontinued):,}",
            f"Continued (Present in Both): {len(results.continued):,}",
            f"Modified (Amount Changed): {len(results.modified):,}",
            "",
            "FINANCIAL SUMMARY",
            "-" * 40,
        ]

        discontinued_total = sum(r.enacted_record.reappropriation_amount
                                  for r in results.discontinued)
        continued_total = sum(r.enacted_record.reappropriation_amount
                               for r in results.continued)

        lines.extend([
            f"Total Discontinued Amount: ${discontinued_total:,.0f}",
            f"Total Continued Amount: ${continued_total:,.0f}",
            "",
            "TOP 10 LARGEST DISCONTINUED ITEMS",
            "-" * 40,
        ])

        # Sort by amount and show top 10
        top_discontinued = sorted(
            results.discontinued,
            key=lambda x: x.enacted_record.reappropriation_amount,
            reverse=True
        )[:10]

        for i, result in enumerate(top_discontinued, 1):
            r = result.enacted_record
            lines.extend([
                f"{i}. {r.agency}",
                f"   ID: {r.appropriation_id} | Year: {r.chapter_year}",
                f"   Amount: ${r.reappropriation_amount:,}",
                f"   Page: {r.page_number}, Line: {r.line_number or 'N/A'}",
                ""
            ])

        # Agency breakdown
        lines.extend([
            "",
            "DISCONTINUED BY AGENCY (TOP 15)",
            "-" * 40,
        ])

        agency_totals = defaultdict(lambda: {'count': 0, 'amount': 0})
        for result in results.discontinued:
            agency = result.enacted_record.agency
            agency_totals[agency]['count'] += 1
            agency_totals[agency]['amount'] += result.enacted_record.reappropriation_amount

        sorted_agencies = sorted(
            agency_totals.items(),
            key=lambda x: x[1]['amount'],
            reverse=True
        )[:15]

        for agency, data in sorted_agencies:
            lines.append(f"{agency}")
            lines.append(f"  Items: {data['count']:,} | Amount: ${data['amount']:,.0f}")
            lines.append("")

        with open(path, 'w') as f:
            f.write('\n'.join(lines))

        return path


# =============================================================================
# MAIN ANALYZER
# =============================================================================

class NYSBudgetAnalyzer:
    """Main analyzer class that orchestrates the full analysis."""

    def __init__(self, output_dir: Path = Path("./output")):
        self.output_dir = output_dir
        self.extractor = PDFExtractor()
        self.deduplicator = DeduplicationEngine()
        self.comparator = BudgetComparator()
        self.reporter = ReportGenerator(output_dir)

    def analyze(self, enacted_pdf: Path, executive_pdf: Path) -> ComparisonResults:
        """
        Run full budget analysis.

        Args:
            enacted_pdf: Path to enacted budget PDF
            executive_pdf: Path to executive budget PDF

        Returns:
            ComparisonResults object with all analysis results
        """
        print("=" * 70)
        print("NYS BUDGET REAPPROPRIATION ANALYSIS")
        print("=" * 70)
        print(f"Enacted PDF:   {enacted_pdf.name}")
        print(f"Executive PDF: {executive_pdf.name}")
        print(f"Output Dir:    {self.output_dir}")
        print("=" * 70)

        # Step 1: Extract enacted budget
        print("\n[1/5] Extracting enacted budget...")
        enacted_records = self.extractor.extract_records(enacted_pdf, "enacted")

        # Step 2: Extract executive budget
        print("\n[2/5] Extracting executive budget...")
        executive_records = self.extractor.extract_records(executive_pdf, "executive")

        # Step 3: Deduplicate
        print("\n[3/5] Deduplicating records...")
        enacted_deduped = self.deduplicator.deduplicate(enacted_records)
        executive_deduped = self.deduplicator.deduplicate(executive_records)
        print(f"  Enacted: {len(enacted_records)} -> {len(enacted_deduped)} unique")
        print(f"  Executive: {len(executive_records)} -> {len(executive_deduped)} unique")

        # Step 4: Compare budgets
        print("\n[4/5] Comparing budgets...")
        results = self.comparator.compare(enacted_deduped, executive_deduped)
        print(f"  Discontinued: {len(results.discontinued):,}")
        print(f"  Continued: {len(results.continued):,}")
        print(f"  Modified: {len(results.modified):,}")

        # Step 5: Generate reports
        print("\n[5/5] Generating reports...")
        outputs = self.reporter.generate_all_reports(results)
        for name, path in outputs.items():
            print(f"  {name}: {path}")

        # Print summary
        print("\n" + "=" * 70)
        print("ANALYSIS COMPLETE")
        print("=" * 70)

        discontinued_total = sum(r.enacted_record.reappropriation_amount
                                  for r in results.discontinued)
        print(f"\nTotal discontinued spending authority: ${discontinued_total:,.0f}")
        print(f"Affecting {len(set(r.enacted_record.agency for r in results.discontinued))} agencies")

        return results


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Command-line interface entry point."""
    parser = argparse.ArgumentParser(
        description='NYS Budget Reappropriation Analysis Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python nys_budget_analyzer.py enacted.pdf executive.pdf
    python nys_budget_analyzer.py "2025 Enacted.pdf" "2026 Executive.pdf" --output-dir ./results
        """
    )

    parser.add_argument(
        'enacted_pdf',
        type=Path,
        help='Path to the enacted budget PDF'
    )
    parser.add_argument(
        'executive_pdf',
        type=Path,
        help='Path to the executive budget PDF'
    )
    parser.add_argument(
        '--output-dir', '-o',
        type=Path,
        default=Path('./output'),
        help='Output directory for reports (default: ./output)'
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.enacted_pdf.exists():
        print(f"Error: Enacted PDF not found: {args.enacted_pdf}")
        sys.exit(1)
    if not args.executive_pdf.exists():
        print(f"Error: Executive PDF not found: {args.executive_pdf}")
        sys.exit(1)

    # Run analysis
    try:
        analyzer = NYSBudgetAnalyzer(args.output_dir)
        results = analyzer.analyze(args.enacted_pdf, args.executive_pdf)

        print(f"\nReports saved to: {args.output_dir}")
        sys.exit(0)

    except Exception as e:
        print(f"\nError during analysis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

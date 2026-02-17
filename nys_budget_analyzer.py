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
class BudgetaryAccountRecord:
    """A budgetary account line item (sub-account within an appropriation).

    In State Operations PDFs, each appropriation has child line items with
    budgetary account codes like Personal Service (50100), Travel (54000), etc.
    These are NOT appropriation IDs — they are expenditure category codes.
    """
    # Parent appropriation link
    parent_appropriation_id: str  # The real approp ID (e.g., "81001")

    # Sub-account identification
    account_code: str  # The budgetary code (e.g., "50100")
    account_description: str  # "Personal service--regular", "Travel", etc.

    # Amount
    amount: int  # Dollar amount for this line item
    reappropriation_amount: int  # Reapprop amount if in reapprop section

    # Context (inherited from parent/page)
    agency: str
    budget_type: str
    fund_type: str
    account: str  # Fund account (e.g., "State Purposes Account - 10050")
    fiscal_year: str
    chapter_year: str

    # Source
    page_number: int
    line_number: Optional[int]
    raw_line: str
    source_file: str
    source_budget: str  # "enacted" or "executive"
    record_type: str  # "appropriation" or "reappropriation"

    def composite_key(self) -> str:
        """Unique key: agency|parent_approp_id|account_code|chapter_year|amount"""
        agency_norm = re.sub(r'\s+', ' ', self.agency.upper().strip()) if self.agency else ""
        return f"{agency_norm}|{self.parent_appropriation_id}|{self.account_code}|{self.chapter_year}|{self.amount}"


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
    # Cross-page buffer persistence for records that span page boundaries
    pending_text_buffer: List[str] = field(default_factory=list)
    pending_start_line_num: Optional[int] = None
    pending_parent_approp_id: str = ""


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


@dataclass
class BudgetaryComparisonResults:
    """Results of comparing budgetary sub-accounts between enacted and executive."""
    discontinued: List[BudgetaryAccountRecord] = field(default_factory=list)
    continued: List[BudgetaryAccountRecord] = field(default_factory=list)
    all_enacted: List[BudgetaryAccountRecord] = field(default_factory=list)
    all_executive: List[BudgetaryAccountRecord] = field(default_factory=list)


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

        # Known budgetary account code prefixes (State Operations sub-accounts)
        # These are NOT appropriation IDs; they are expenditure category codes:
        #   50xxx = Personal Service, 51xxx = Contractual Services,
        #   54xxx = Travel, 56xxx = Equipment, 57xxx = Supplies/Nonpersonal Service,
        #   58xxx = Indirect Costs, 60xxx = Fringe Benefits
        self.BUDGETARY_ACCOUNT_PREFIXES = {'50', '51', '54', '56', '57', '58', '60'}

        # Pattern matching a budgetary line item: "description (XXXXX) ... amount"
        self.BUDGETARY_LINE = re.compile(
            r'([\w\s\-/]+?)\s*\((\d{5})\)\s*\.{2,}\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)'
        )

        # Keyword-based detection of budgetary sub-lines.
        # Catches sub-lines that inherited a real parent approp ID from buffer context.
        # Matches: "Personal service--regular (50100)", "Travel (54000)", etc.
        self.BUDGETARY_SUB_LINE_KEYWORDS = re.compile(
            r'(?:Personal service|Temporary service|Holiday[/]?overtime|'
            r'Contractual services?|Travel|Equipment|'
            r'Supplies and materials|Nonpersonal service|'
            r'Indirect costs?|Fringe benefits?)'
            r'\s*(?:--\w+\s*)?\(\d{5}\)',
            re.IGNORECASE
        )

    def is_budgetary_account_code(self, code: str) -> bool:
        """Return True if the 5-digit code is a budgetary account code, not an approp ID."""
        if not code or len(code) != 5:
            return False
        return code[:2] in self.BUDGETARY_ACCOUNT_PREFIXES

    def is_budgetary_sub_line(self, text: str) -> bool:
        """Return True if text contains a budgetary sub-line pattern.

        Catches sub-lines even when they've inherited a real parent approp ID.
        Matches patterns like 'Personal service--regular (50100) ... 9,900,000'
        """
        return bool(self.BUDGETARY_SUB_LINE_KEYWORDS.search(text))


# =============================================================================
# PDF EXTRACTION
# =============================================================================

class PDFExtractor:
    """Extracts budget records from PDF files."""

    def __init__(self):
        self.patterns = BudgetPatterns()

    def extract_records(self, pdf_path: Path, source_budget: str) -> Tuple[List[BudgetRecord], List[BudgetaryAccountRecord]]:
        """
        Extract all budget records from a PDF.

        Args:
            pdf_path: Path to the PDF file
            source_budget: "enacted" or "executive"

        Returns:
            Tuple of (BudgetRecord list, BudgetaryAccountRecord list)
        """
        records = []
        budgetary_records = []
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
                page_records, page_budgetary = self._extract_page_records(
                    text, context, page_num, pdf_path.name, source_budget
                )

                # Deduplicate as we go
                for record in page_records:
                    key = record.composite_key()
                    if key not in seen_keys:
                        seen_keys.add(key)
                        records.append(record)

                # Collect budgetary records
                budgetary_records.extend(page_budgetary)

                # Progress indicator
                if page_num % 100 == 0:
                    print(f"    Progress: {page_num}/{total_pages} pages ({page_num/total_pages*100:.1f}%)")

        print(f"  Extracted {len(records)} unique records")
        if budgetary_records:
            print(f"  Extracted {len(budgetary_records)} budgetary sub-account records")
        return records, budgetary_records

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
    ) -> Tuple[List[BudgetRecord], List[BudgetaryAccountRecord]]:
        """Extract budget records from a single page."""
        records = []
        budgetary_records = []

        # Split page into chapter-based chunks for reappropriations
        if context.is_reappropriation_section:
            records.extend(self._extract_reappropriation_records(
                page_text, context, page_num, source_file, source_budget,
                budgetary_records
            ))
        else:
            # Extract appropriations
            records.extend(self._extract_appropriation_records(
                page_text, context, page_num, source_file, source_budget,
                budgetary_records
            ))

        return records, budgetary_records

    def _extract_reappropriation_records(
        self,
        page_text: str,
        context: ParsingContext,
        page_num: int,
        source_file: str,
        source_budget: str,
        budgetary_records: List[BudgetaryAccountRecord] = None
    ) -> List[BudgetRecord]:
        """Extract reappropriation records from page text.

        For State Operations, tracks parent appropriation IDs across buffer resets
        so that budgetary account codes are properly linked to their parent.
        """
        records = []
        if budgetary_records is None:
            budgetary_records = []
        lines = page_text.splitlines()
        is_state_ops = context.budget_type == "STATE OPERATIONS"

        # Track current chapter context and text buffer
        current_chapter_year = context.chapter_year
        current_chapter_num = context.chapter_number
        current_section_num = context.section_number

        # Restore any pending buffer from the previous page (cross-page records)
        text_buffer = list(context.pending_text_buffer)
        start_line_num = context.pending_start_line_num
        # Clear the pending buffer so it doesn't carry over again
        context.pending_text_buffer = []
        context.pending_start_line_num = None

        # Track parent appropriation ID across buffer resets (key fix for State Ops)
        # Restore from context if carrying over from previous page
        current_parent_approp_id = context.pending_parent_approp_id or None

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
                        if is_state_ops and not self.patterns.is_budgetary_account_code(record.appropriation_id):
                            current_parent_approp_id = record.appropriation_id
                        records.append(record)

                # Start new buffer
                text_buffer = [original_line]
                start_line_num = line_num

                # Extract real approp ID from chapter citation line
                if is_state_ops:
                    all_ids = self.patterns.APPROP_ID.findall(line_stripped)
                    for found_id in all_ids:
                        if not self.patterns.is_budgetary_account_code(found_id):
                            current_parent_approp_id = found_id
                continue

            # Scan non-chapter lines for real approp IDs (updates parent tracker)
            if is_state_ops:
                all_ids = self.patterns.APPROP_ID.findall(line_stripped)
                for found_id in all_ids:
                    if not self.patterns.is_budgetary_account_code(found_id):
                        current_parent_approp_id = found_id

            # Check for reappropriation marker
            reapprop_match = self.patterns.REAPPROP_MARKER.search(line_stripped)
            if reapprop_match:
                text_buffer.append(original_line)

                # Check if this is a budgetary sub-account reappropriation
                buffer_text = '\n'.join(text_buffer)
                found_id = self._find_approp_id(buffer_text)

                if (is_state_ops and found_id
                        and self.patterns.is_budgetary_account_code(found_id)):
                    # Route to budgetary records
                    reapprop_amount = self._parse_amount(reapprop_match.group(1))
                    full_match = self.patterns.REAPPROP_FULL.search(buffer_text)
                    orig_amount = self._parse_amount(full_match.group(1)) if full_match else reapprop_amount

                    desc_match = self.patterns.BUDGETARY_LINE.search(buffer_text)
                    description = desc_match.group(1).strip() if desc_match else ""

                    budgetary_records.append(BudgetaryAccountRecord(
                        parent_appropriation_id=current_parent_approp_id,
                        account_code=found_id,
                        account_description=description,
                        amount=orig_amount,
                        reappropriation_amount=reapprop_amount,
                        agency=context.agency,
                        budget_type=context.budget_type,
                        fund_type=context.fund_type,
                        account=context.account,
                        fiscal_year=context.fiscal_year,
                        chapter_year=current_chapter_year,
                        page_number=page_num,
                        line_number=start_line_num,
                        raw_line=line_stripped,
                        source_file=source_file,
                        source_budget=source_budget,
                        record_type='reappropriation',
                    ))
                else:
                    # Check if buffer text is actually a budgetary sub-line
                    # that inherited a real parent ID from the chapter citation.
                    # BUT: only if the found_id is NOT the real approp ID —
                    # if _find_approp_id returned a non-budgetary code, the
                    # buffer contains a real appropriation that just happens
                    # to mention a budgetary category (e.g., "Equipment (56000)")
                    if (is_state_ops
                            and (not found_id or self.patterns.is_budgetary_account_code(found_id))
                            and self.patterns.is_budgetary_sub_line(buffer_text)):
                        # This is a sub-line masquerading with a real ID — route to budgetary
                        reapprop_amount = self._parse_amount(reapprop_match.group(1))
                        full_match = self.patterns.REAPPROP_FULL.search(buffer_text)
                        orig_amount = self._parse_amount(full_match.group(1)) if full_match else reapprop_amount

                        # Extract the actual budgetary code from the text
                        budgetary_code_match = re.search(r'\((\d{5})\)', buffer_text)
                        budgetary_codes = self.patterns.APPROP_ID.findall(buffer_text)
                        actual_code = next((c for c in budgetary_codes if self.patterns.is_budgetary_account_code(c)), "Unknown")

                        desc_match = self.patterns.BUDGETARY_LINE.search(buffer_text)
                        description = desc_match.group(1).strip() if desc_match else ""

                        budgetary_records.append(BudgetaryAccountRecord(
                            parent_appropriation_id=current_parent_approp_id or found_id or "Unknown",
                            account_code=actual_code,
                            account_description=description,
                            amount=orig_amount,
                            reappropriation_amount=reapprop_amount,
                            agency=context.agency,
                            budget_type=context.budget_type,
                            fund_type=context.fund_type,
                            account=context.account,
                            fiscal_year=context.fiscal_year,
                            chapter_year=current_chapter_year,
                            page_number=page_num,
                            line_number=start_line_num,
                            raw_line=line_stripped,
                            source_file=source_file,
                            source_budget=source_budget,
                            record_type='reappropriation',
                        ))
                    else:
                        # Genuinely a real appropriation
                        record = self._create_reapprop_record_from_buffer(
                            text_buffer, context, current_chapter_year,
                            current_chapter_num, current_section_num,
                            page_num, start_line_num, source_file, source_budget
                        )
                        if record:
                            if is_state_ops and not self.patterns.is_budgetary_account_code(record.appropriation_id):
                                current_parent_approp_id = record.appropriation_id
                            records.append(record)

                # Reset buffer (but keep chapter context and parent tracker)
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

        # Persist any unprocessed buffer for the next page (cross-page records)
        if text_buffer:
            context.pending_text_buffer = text_buffer
            context.pending_start_line_num = start_line_num
        else:
            context.pending_text_buffer = []
            context.pending_start_line_num = None

        # Persist parent approp ID for cross-page continuity
        context.pending_parent_approp_id = current_parent_approp_id or ""

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
        source_budget: str,
        budgetary_records: List[BudgetaryAccountRecord] = None
    ) -> List[BudgetRecord]:
        """Extract appropriation (non-reappropriation) records.

        For State Operations, budgetary account codes (50xxx, 51xxx, etc.) are
        routed to budgetary_records instead of being treated as appropriation IDs.
        """
        records = []
        if budgetary_records is None:
            budgetary_records = []
        lines = page_text.splitlines()
        text_buffer = []
        start_line_num = None
        is_state_ops = context.budget_type == "STATE OPERATIONS"

        # Track parent appropriation ID for budgetary sub-accounts
        current_parent_approp_id = None

        for line in lines:
            original_line = line
            line_stripped = line.strip()

            # Extract line number
            line_num = None
            line_match = self.patterns.LINE_PREFIX.match(line_stripped)
            if line_match:
                line_num = int(line_match.group(1))
                line_stripped = line_match.group(2)

            # Scan for real approp IDs in any line (updates parent tracker)
            if is_state_ops:
                all_ids = self.patterns.APPROP_ID.findall(line_stripped)
                for found_id in all_ids:
                    if not self.patterns.is_budgetary_account_code(found_id):
                        current_parent_approp_id = found_id

            # Check for appropriation pattern: (XXXXX) ... amount
            approp_match = self.patterns.NEW_APPROP.search(line_stripped)
            if approp_match:
                approp_id = approp_match.group(1)
                amount = self._parse_amount(approp_match.group(2))

                if is_state_ops and self.patterns.is_budgetary_account_code(approp_id):
                    # This is a budgetary sub-account line, NOT a real appropriation
                    desc_match = self.patterns.BUDGETARY_LINE.search(line_stripped)
                    description = desc_match.group(1).strip() if desc_match else ""

                    budgetary_records.append(BudgetaryAccountRecord(
                        parent_appropriation_id=current_parent_approp_id or "Unknown",
                        account_code=approp_id,
                        account_description=description,
                        amount=amount,
                        reappropriation_amount=0,
                        agency=context.agency,
                        budget_type=context.budget_type,
                        fund_type=context.fund_type,
                        account=context.account,
                        fiscal_year=context.fiscal_year,
                        chapter_year=context.fiscal_year[:4] if context.fiscal_year != "Unknown" else "Unknown",
                        page_number=page_num,
                        line_number=line_num,
                        raw_line=line_stripped,
                        source_file=source_file,
                        source_budget=source_budget,
                        record_type='appropriation',
                    ))
                    text_buffer = []
                    start_line_num = None
                elif is_state_ops and self.patterns.is_budgetary_sub_line(line_stripped):
                    # Sub-line with a non-budgetary code on the same line (e.g., parent ID
                    # appeared earlier on the line). Route to budgetary records.
                    budgetary_codes = self.patterns.APPROP_ID.findall(line_stripped)
                    actual_code = next((c for c in budgetary_codes if self.patterns.is_budgetary_account_code(c)), "Unknown")

                    desc_match = self.patterns.BUDGETARY_LINE.search(line_stripped)
                    description = desc_match.group(1).strip() if desc_match else ""

                    budgetary_records.append(BudgetaryAccountRecord(
                        parent_appropriation_id=current_parent_approp_id or approp_id,
                        account_code=actual_code,
                        account_description=description,
                        amount=amount,
                        reappropriation_amount=0,
                        agency=context.agency,
                        budget_type=context.budget_type,
                        fund_type=context.fund_type,
                        account=context.account,
                        fiscal_year=context.fiscal_year,
                        chapter_year=context.fiscal_year[:4] if context.fiscal_year != "Unknown" else "Unknown",
                        page_number=page_num,
                        line_number=line_num,
                        raw_line=line_stripped,
                        source_file=source_file,
                        source_budget=source_budget,
                        record_type='appropriation',
                    ))
                    text_buffer = []
                    start_line_num = None
                else:
                    # Real appropriation
                    if is_state_ops:
                        current_parent_approp_id = approp_id

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
        """Find appropriation ID in text, preferring real IDs over budgetary codes."""
        # Collect all 5-digit IDs in parentheses
        all_matches = self.patterns.APPROP_ID.findall(text)

        if all_matches:
            # Prefer non-budgetary IDs
            real_ids = [m for m in all_matches if not self.patterns.is_budgetary_account_code(m)]
            if real_ids:
                return real_ids[0]
            # Fall back to first match (might be budgetary - caller decides)
            return all_matches[0]

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

        Uses two-pass matching:
        - Pass 1: Exact composite_key match (agency|approp_id|chapter_year|amount)
        - Pass 2: Relaxed match dropping chapter_year (catches appropriation records
          where chapter_year defaults to fiscal year prefix, which differs between budgets)
        """
        results = ComparisonResults()
        results.all_enacted = enacted_records
        results.all_executive = executive_records

        # Pass 1: Build lookup from ALL executive records (not just reappropriations)
        exec_by_key: Dict[str, BudgetRecord] = {}
        for record in executive_records:
            key = record.composite_key()
            exec_by_key[key] = record

        # Pass 2: Build relaxed lookup (no chapter_year) for fallback matching
        # Key: agency|appropriation_id|appropriation_amount
        exec_by_relaxed: Dict[str, BudgetRecord] = {}
        for record in executive_records:
            relaxed = f"{record._normalize(record.agency)}|{record.appropriation_id}|{record.appropriation_amount}"
            exec_by_relaxed[relaxed] = record

        print(f"  Executive records: {len(exec_by_key)} (exact keys), {len(exec_by_relaxed)} (relaxed keys)")

        # Compare each enacted record against executive
        for enacted in enacted_records:
            key = enacted.composite_key()
            relaxed = f"{enacted._normalize(enacted.agency)}|{enacted.appropriation_id}|{enacted.appropriation_amount}"

            # Try exact match first, then relaxed match
            exec_record = exec_by_key.get(key) or exec_by_relaxed.get(relaxed)

            if exec_record:
                # Check if reappropriation amounts match
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

    def compare_budgetary(
        self,
        enacted_budgetary: List[BudgetaryAccountRecord],
        executive_budgetary: List[BudgetaryAccountRecord]
    ) -> BudgetaryComparisonResults:
        """Compare budgetary sub-accounts between enacted and executive."""
        results = BudgetaryComparisonResults()
        results.all_enacted = enacted_budgetary
        results.all_executive = executive_budgetary

        # Build lookup of executive budgetary records
        exec_lookup: Set[str] = set()
        for record in executive_budgetary:
            exec_lookup.add(record.composite_key())

        for enacted in enacted_budgetary:
            key = enacted.composite_key()
            if key in exec_lookup:
                results.continued.append(enacted)
            else:
                results.discontinued.append(enacted)

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

    def generate_budgetary_reports(self, budgetary_results: BudgetaryComparisonResults) -> Dict[str, Path]:
        """Generate reports for budgetary sub-account level analysis."""
        outputs = {}

        outputs['budgetary_discrepancies'] = self._write_budgetary_csv(
            budgetary_results.discontinued, 'budgetary_account_discrepancies.csv'
        )
        outputs['enacted_budgetary'] = self._write_budgetary_csv(
            budgetary_results.all_enacted, 'enacted_budgetary_details.csv'
        )
        outputs['executive_budgetary'] = self._write_budgetary_csv(
            budgetary_results.all_executive, 'executive_budgetary_details.csv'
        )

        return outputs

    def _write_budgetary_csv(self, records: List[BudgetaryAccountRecord], filename: str) -> Path:
        """Write budgetary account records to CSV."""
        path = self.output_dir / filename

        rows = []
        for r in records:
            rows.append({
                'agency': r.agency,
                'budget_type': r.budget_type,
                'fund_type': r.fund_type,
                'account': r.account,
                'parent_appropriation_id': r.parent_appropriation_id,
                'account_code': r.account_code,
                'account_description': r.account_description,
                'amount': r.amount,
                'reappropriation_amount': r.reappropriation_amount,
                'chapter_year': r.chapter_year,
                'fiscal_year': r.fiscal_year,
                'record_type': r.record_type,
                'page_number': r.page_number,
                'line_number': r.line_number,
                'raw_line': r.raw_line,
                'source_file': r.source_file,
                'composite_key': r.composite_key(),
            })

        df = pd.DataFrame(rows)
        df.to_csv(path, index=False)
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
        print("\n[1/6] Extracting enacted budget...")
        enacted_records, enacted_budgetary = self.extractor.extract_records(enacted_pdf, "enacted")

        # Step 2: Extract executive budget
        print("\n[2/6] Extracting executive budget...")
        executive_records, executive_budgetary = self.extractor.extract_records(executive_pdf, "executive")

        # Step 3: Deduplicate
        print("\n[3/5] Deduplicating records...")
        enacted_deduped = self.deduplicator.deduplicate(enacted_records)
        executive_deduped = self.deduplicator.deduplicate(executive_records)
        print(f"  Enacted: {len(enacted_records)} -> {len(enacted_deduped)} unique")
        print(f"  Executive: {len(executive_records)} -> {len(executive_deduped)} unique")
        if enacted_budgetary or executive_budgetary:
            print(f"  (Filtered {len(enacted_budgetary)} enacted / {len(executive_budgetary)} executive budgetary sub-lines)")

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
        print(f"\nDiscontinued spending authority: ${discontinued_total:,.0f}")
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

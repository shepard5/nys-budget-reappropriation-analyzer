"""
Core regex patterns for NYS budget PDF parsing.
"""

import re


class BudgetPatternsV4:
    """Regex patterns for budget extraction - core v3 patterns only."""

    def __init__(self):
        # Amounts
        self.REAPPROP_FULL = re.compile(r'(\d{1,3}(?:,\d{3})*)\s*\.{3,}\s*\(re\.\s*\$(\d{1,3}(?:,\d{3})*)\)')
        self.REAPPROP_MARKER = re.compile(r'\(re\.\s*\$(\d{1,3}(?:,\d{3})*)\)')

        # IDs
        self.APPROP_ID = re.compile(r'\((\d{5})\)')
        self.APPROP_ID_UNDERLINE = re.compile(r'_\(_(\d)_(\d)_(\d)_(\d)_(\d)_\)')

        # Chapter citation
        self.CHAPTER_LAW = re.compile(r'By\s+chapter\s+(\d+),?\s*section\s+(\d+),?\s*of\s+the\s+laws\s+of\s+(\d{4})', re.I)

        # Headers
        self.BUDGET_TYPE = re.compile(r'^(STATE OPERATIONS|AID TO LOCALITIES|CAPITAL PROJECTS)\s*-?\s*(REAPPROPRIATIONS|APPROPRIATIONS)?', re.I)
        self.FUND_TYPE = re.compile(r'(General Fund|Special Revenue Funds? - (?:Federal|Other)|Capital Projects Fund)', re.I)
        self.ACCOUNT = re.compile(r'([A-Za-z][A-Za-z\s\-]+Account\s*-\s*\d{5})')
        self.FISCAL_YEAR = re.compile(r'(\d{4})-(\d{2})')

    def extract_approp_id(self, text: str) -> str | None:
        m = self.APPROP_ID.search(text)
        if m:
            return m.group(1)
        m = self.APPROP_ID_UNDERLINE.search(text)
        if m:
            return ''.join(m.groups())
        return None

    def extract_chapter_citation(self, text: str) -> dict | None:
        m = self.CHAPTER_LAW.search(text)
        return {'chapter': m.group(1), 'section': m.group(2), 'year': m.group(3)} if m else None

    def extract_amounts(self, text: str) -> dict | None:
        m = self.REAPPROP_FULL.search(text)
        if m:
            return {'appropriation': int(m.group(1).replace(',', '')), 'reappropriation': int(m.group(2).replace(',', ''))}
        m = self.REAPPROP_MARKER.search(text)
        if m:
            return {'appropriation': 0, 'reappropriation': int(m.group(1).replace(',', ''))}
        return None

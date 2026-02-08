"""
Enhanced budget record model for v4.

Extends the v3 BudgetRecord with additional parsed fields from bill_language.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import re


@dataclass
class BudgetRecordV4:
    """
    Enhanced budget record with deep extraction capabilities.

    Includes all v3 fields plus new fields parsed from bill_language.
    """

    # === PRIMARY IDENTIFIERS (from v3) ===
    agency: str
    appropriation_id: str
    chapter_year: str

    # === AMOUNTS (from v3) ===
    appropriation_amount: int
    reappropriation_amount: int

    # === CLASSIFICATION (from v3) ===
    record_type: str  # "appropriation" | "reappropriation"
    budget_type: str  # "STATE OPERATIONS" | "AID TO LOCALITIES" | "CAPITAL PROJECTS"
    fund_type: str
    account: str
    fiscal_year: str

    # === SOURCE TRACKING (from v3) ===
    page_number: int
    line_number: Optional[int]
    bill_language: str
    raw_line: str
    source_file: str
    source_budget: str  # "enacted" | "executive"

    # === CHAPTER CITATION (from v3) ===
    chapter_number: Optional[str] = None
    section_number: Optional[str] = None

    # === NEW: PROGRAM INFORMATION ===
    program_purpose: Optional[str] = None  # First descriptive sentence
    program_category: Optional[str] = None  # Classified category

    # === NEW: RECIPIENT INFORMATION ===
    recipient_name: Optional[str] = None
    recipient_type: Optional[str] = None  # "nonprofit", "municipality", "state_agency", etc.
    recipients: List[Dict[str, Any]] = field(default_factory=list)  # Multiple recipients

    # === NEW: STATUTORY REFERENCES ===
    statutory_references: List[Dict[str, str]] = field(default_factory=list)

    # === NEW: TRANSFER/INTERCHANGE AUTHORITY ===
    has_transfer_authority: bool = False
    has_interchange_authority: bool = False
    transfer_targets: List[str] = field(default_factory=list)

    # === NEW: APPROVAL REQUIREMENTS ===
    requires_approval: bool = False
    approval_authority: Optional[str] = None

    # === NEW: SET-ASIDES AND ALLOCATIONS ===
    set_aside_amount: Optional[int] = None
    set_aside_purpose: Optional[str] = None
    percentage_allocations: List[Dict[str, Any]] = field(default_factory=list)

    # === NEW: TIME CONSTRAINTS ===
    time_limits: List[Dict[str, Any]] = field(default_factory=list)

    # === COMPUTED FIELDS ===
    appropriation_age_years: int = 0
    composite_key: str = ""

    def __post_init__(self):
        """Compute derived fields after initialization."""
        # Compute appropriation age
        try:
            fiscal_start = int(self.fiscal_year.split('-')[0])
            chapter_yr = int(self.chapter_year)
            self.appropriation_age_years = fiscal_start - chapter_yr
        except (ValueError, AttributeError):
            self.appropriation_age_years = 0

        # Generate composite key if not set
        if not self.composite_key:
            self.composite_key = self._generate_composite_key()

    def _normalize(self, text: str) -> str:
        """Normalize text for comparison."""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', str(text).upper().strip())

    def _generate_composite_key(self) -> str:
        """
        Generate unique composite key for deduplication and matching.

        Key components: agency|appropriation_id|chapter_year|appropriation_amount

        Note: Account is NOT included because the same appropriation can appear
        under different account names between enacted and executive budgets.
        """
        return f"{self._normalize(self.agency)}|{self.appropriation_id}|{self.chapter_year}|{self.appropriation_amount}"

    def extended_key(self) -> str:
        """
        Extended key for cross-budget tracking.

        Tracks the same appropriation ID across multiple budget cycles,
        regardless of chapter year or amount.
        """
        return f"{self._normalize(self.agency)}|{self.appropriation_id}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert record to dictionary for serialization."""
        return {
            'agency': self.agency,
            'appropriation_id': self.appropriation_id,
            'chapter_year': self.chapter_year,
            'appropriation_amount': self.appropriation_amount,
            'reappropriation_amount': self.reappropriation_amount,
            'record_type': self.record_type,
            'budget_type': self.budget_type,
            'fund_type': self.fund_type,
            'account': self.account,
            'fiscal_year': self.fiscal_year,
            'page_number': self.page_number,
            'line_number': self.line_number,
            'bill_language': self.bill_language,
            'source_file': self.source_file,
            'source_budget': self.source_budget,
            'program_purpose': self.program_purpose,
            'program_category': self.program_category,
            'recipient_name': self.recipient_name,
            'recipient_type': self.recipient_type,
            'has_transfer_authority': self.has_transfer_authority,
            'has_interchange_authority': self.has_interchange_authority,
            'transfer_targets': self.transfer_targets,
            'requires_approval': self.requires_approval,
            'approval_authority': self.approval_authority,
            'set_aside_amount': self.set_aside_amount,
            'set_aside_purpose': self.set_aside_purpose,
            'appropriation_age_years': self.appropriation_age_years,
            'composite_key': self.composite_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BudgetRecordV4':
        """Create record from dictionary."""
        return cls(
            agency=data.get('agency', ''),
            appropriation_id=data.get('appropriation_id', ''),
            chapter_year=data.get('chapter_year', ''),
            appropriation_amount=data.get('appropriation_amount', 0),
            reappropriation_amount=data.get('reappropriation_amount', 0),
            record_type=data.get('record_type', ''),
            budget_type=data.get('budget_type', ''),
            fund_type=data.get('fund_type', ''),
            account=data.get('account', ''),
            fiscal_year=data.get('fiscal_year', ''),
            page_number=data.get('page_number', 0),
            line_number=data.get('line_number'),
            bill_language=data.get('bill_language', ''),
            raw_line=data.get('raw_line', ''),
            source_file=data.get('source_file', ''),
            source_budget=data.get('source_budget', ''),
            chapter_number=data.get('chapter_number'),
            section_number=data.get('section_number'),
            program_purpose=data.get('program_purpose'),
            program_category=data.get('program_category'),
            recipient_name=data.get('recipient_name'),
            recipient_type=data.get('recipient_type'),
            recipients=data.get('recipients', []),
            statutory_references=data.get('statutory_references', []),
            has_transfer_authority=data.get('has_transfer_authority', False),
            has_interchange_authority=data.get('has_interchange_authority', False),
            transfer_targets=data.get('transfer_targets', []),
            requires_approval=data.get('requires_approval', False),
            approval_authority=data.get('approval_authority'),
            set_aside_amount=data.get('set_aside_amount'),
            set_aside_purpose=data.get('set_aside_purpose'),
            percentage_allocations=data.get('percentage_allocations', []),
            time_limits=data.get('time_limits', []),
            composite_key=data.get('composite_key', ''),
        )

    @classmethod
    def from_v3_record(cls, v3_data: Dict[str, Any], parsed_entities: Dict[str, Any] = None) -> 'BudgetRecordV4':
        """
        Create v4 record from v3 CSV row data.

        Args:
            v3_data: Dictionary from v3 CSV row
            parsed_entities: Optional pre-parsed entities from bill_language
        """
        record = cls(
            agency=v3_data.get('agency', ''),
            appropriation_id=v3_data.get('appropriation_id', ''),
            chapter_year=v3_data.get('chapter_year', ''),
            appropriation_amount=int(v3_data.get('appropriation_amount', 0) or 0),
            reappropriation_amount=int(v3_data.get('reappropriation_amount', 0) or 0),
            record_type='reappropriation' if int(v3_data.get('reappropriation_amount', 0) or 0) > 0 else 'appropriation',
            budget_type=v3_data.get('budget_type', ''),
            fund_type=v3_data.get('fund_type', ''),
            account=v3_data.get('account', ''),
            fiscal_year=v3_data.get('fiscal_year', ''),
            page_number=int(v3_data.get('page_number', 0) or 0),
            line_number=float(v3_data['line_number']) if v3_data.get('line_number') else None,
            bill_language=v3_data.get('bill_language', ''),
            raw_line=v3_data.get('raw_line', ''),
            source_file=v3_data.get('source_file', ''),
            source_budget=v3_data.get('source_budget', 'enacted'),
            composite_key=v3_data.get('composite_key', ''),
        )

        # Apply parsed entities if provided
        if parsed_entities:
            record.program_purpose = parsed_entities.get('program_purpose')
            record.program_category = parsed_entities.get('program_category')
            record.recipient_name = parsed_entities.get('recipient_name')
            record.recipient_type = parsed_entities.get('recipient_type')
            record.recipients = parsed_entities.get('recipients', [])
            record.statutory_references = parsed_entities.get('statutory_references', [])
            record.has_transfer_authority = parsed_entities.get('has_transfer_authority', False)
            record.has_interchange_authority = parsed_entities.get('has_interchange_authority', False)
            record.transfer_targets = parsed_entities.get('transfer_targets', [])
            record.requires_approval = parsed_entities.get('requires_approval', False)
            record.approval_authority = parsed_entities.get('approval_authority')
            record.set_aside_amount = parsed_entities.get('set_aside_amount')
            record.set_aside_purpose = parsed_entities.get('set_aside_purpose')

        return record

    def __str__(self) -> str:
        return f"{self.agency} | {self.appropriation_id} | {self.chapter_year} | ${self.reappropriation_amount:,}"

    def __repr__(self) -> str:
        return f"BudgetRecordV4(agency='{self.agency}', id='{self.appropriation_id}', year='{self.chapter_year}', amount=${self.reappropriation_amount:,})"

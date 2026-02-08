"""
Extracted entities from budget bill language.

These dataclasses represent structured information parsed from the
free-text bill_language field in budget records.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class RecipientEntity:
    """Organization or entity receiving funds."""
    name: str
    entity_type: str  # "nonprofit", "municipality", "state_agency", "educational", "corporation", "other"
    sub_allocation_id: Optional[str] = None
    sub_allocation_amount: Optional[int] = None

    def __str__(self) -> str:
        if self.sub_allocation_amount:
            return f"{self.name} ({self.entity_type}): ${self.sub_allocation_amount:,}"
        return f"{self.name} ({self.entity_type})"


@dataclass
class StatutoryReference:
    """Reference to law or regulation in budget language."""
    law_name: str  # "education law", "executive law", "social services law", etc.
    section: str   # "6452", "529", "367-b"
    subdivision: Optional[str] = None
    full_citation: str = ""

    def __str__(self) -> str:
        if self.subdivision:
            return f"Section {self.section}({self.subdivision}) of the {self.law_name}"
        return f"Section {self.section} of the {self.law_name}"


@dataclass
class SetAside:
    """Earmarked portion of an appropriation."""
    amount: int
    purpose: str
    is_maximum: bool = True  # "up to $X" vs exact amount

    def __str__(self) -> str:
        prefix = "Up to " if self.is_maximum else ""
        return f"{prefix}${self.amount:,} for {self.purpose}"


@dataclass
class PercentageAllocation:
    """Percentage-based constraint or allocation."""
    percentage: float
    constraint_type: str  # "maximum", "minimum", "exact", "limit"
    applies_to: str  # Description of what it applies to

    def __str__(self) -> str:
        return f"{self.percentage}% ({self.constraint_type}) - {self.applies_to}"


@dataclass
class TimeLimit:
    """Time-based constraint in budget language."""
    duration_value: int
    duration_unit: str  # "days", "months", "years"
    constraint_type: str  # "maximum", "within", "minimum", "age_requirement"
    description: str = ""

    def __str__(self) -> str:
        return f"{self.duration_value} {self.duration_unit} ({self.constraint_type})"


@dataclass
class TransferAuthority:
    """Transfer or interchange authority for funds."""
    can_transfer: bool = False
    can_interchange: bool = False
    target_agencies: List[str] = field(default_factory=list)
    target_accounts: List[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_authority: Optional[str] = None  # "director of the budget", "commissioner", etc.

    def __str__(self) -> str:
        parts = []
        if self.can_transfer:
            parts.append("transfer")
        if self.can_interchange:
            parts.append("interchange")
        auth_type = "/".join(parts) if parts else "none"

        if self.target_agencies:
            targets = ", ".join(self.target_agencies[:3])
            if len(self.target_agencies) > 3:
                targets += f" (+{len(self.target_agencies) - 3} more)"
            return f"{auth_type} to {targets}"
        return auth_type

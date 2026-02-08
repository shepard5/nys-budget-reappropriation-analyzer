"""
NYS Budget Analyzer v4 - God-Level Budget Analysis Tool

A comprehensive platform for analyzing New York State budget appropriations
and reappropriations across fiscal years.

Features:
- PDF extraction with deep entity parsing
- Flexible CLI query/filter system
- Advanced analytics (distributions, rankings, trends)
- Multi-format exports (CSV, Excel, HTML dashboard)
- Cross-cycle appropriation tracking
"""

__version__ = "4.0.0"
__author__ = "NYS Ways and Means"

from .models.budget_record import BudgetRecordV4
from .models.entities import RecipientEntity, StatutoryReference, SetAside, PercentageAllocation

__all__ = [
    'BudgetRecordV4',
    'RecipientEntity',
    'StatutoryReference',
    'SetAside',
    'PercentageAllocation',
]

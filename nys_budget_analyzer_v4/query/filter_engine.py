"""
Filter engine for querying budget records.

Supports flexible filtering with various operators.
"""

import re
from enum import Enum
from dataclasses import dataclass
from typing import Any, List, Dict, Callable, Union


class FilterOperator(Enum):
    """Filter comparison operators."""
    EQUALS = "eq"
    NOT_EQUALS = "neq"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "gt"
    LESS_THAN = "lt"
    GREATER_EQUAL = "gte"
    LESS_EQUAL = "lte"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    REGEX = "regex"


@dataclass
class Filter:
    """Single filter condition."""
    field: str
    operator: FilterOperator
    value: Any = None
    case_sensitive: bool = False

    def __str__(self) -> str:
        return f"{self.field} {self.operator.value} {self.value}"


class FilterEngine:
    """
    Applies filters to budget record collections.

    Supports various comparison operators and handles nested field access.
    """

    def __init__(self):
        self._operators: Dict[FilterOperator, Callable] = {
            FilterOperator.EQUALS: self._op_equals,
            FilterOperator.NOT_EQUALS: self._op_not_equals,
            FilterOperator.CONTAINS: self._op_contains,
            FilterOperator.NOT_CONTAINS: self._op_not_contains,
            FilterOperator.STARTS_WITH: self._op_starts_with,
            FilterOperator.ENDS_WITH: self._op_ends_with,
            FilterOperator.IN: self._op_in,
            FilterOperator.NOT_IN: self._op_not_in,
            FilterOperator.GREATER_THAN: self._op_gt,
            FilterOperator.LESS_THAN: self._op_lt,
            FilterOperator.GREATER_EQUAL: self._op_gte,
            FilterOperator.LESS_EQUAL: self._op_lte,
            FilterOperator.BETWEEN: self._op_between,
            FilterOperator.IS_NULL: self._op_is_null,
            FilterOperator.IS_NOT_NULL: self._op_is_not_null,
            FilterOperator.IS_TRUE: self._op_is_true,
            FilterOperator.IS_FALSE: self._op_is_false,
            FilterOperator.REGEX: self._op_regex,
        }

    def apply(self, records: List[Dict[str, Any]], filters: List[Filter]) -> List[Dict[str, Any]]:
        """
        Apply all filters to records.

        Args:
            records: List of record dictionaries
            filters: List of Filter objects to apply

        Returns:
            Filtered list of records
        """
        if not filters:
            return records

        result = records
        for f in filters:
            result = [r for r in result if self._matches(r, f)]
        return result

    def _matches(self, record: Dict[str, Any], filter: Filter) -> bool:
        """Check if a record matches a filter."""
        value = self._get_field_value(record, filter.field)
        op_func = self._operators.get(filter.operator)

        if op_func is None:
            raise ValueError(f"Unknown operator: {filter.operator}")

        return op_func(value, filter.value, filter.case_sensitive)

    def _get_field_value(self, record: Dict[str, Any], field: str) -> Any:
        """
        Get field value from record, supporting dot notation for nested access.

        Examples:
            'agency' -> record['agency']
            'recipients.0.name' -> record['recipients'][0]['name']
        """
        parts = field.split('.')
        value = record

        for part in parts:
            if value is None:
                return None

            # Handle list index
            if part.isdigit():
                idx = int(part)
                if isinstance(value, list) and idx < len(value):
                    value = value[idx]
                else:
                    return None
            # Handle dict/object access
            elif isinstance(value, dict):
                value = value.get(part)
            elif hasattr(value, part):
                value = getattr(value, part)
            else:
                return None

        return value

    def _normalize(self, value: Any, case_sensitive: bool = False) -> str:
        """Normalize value for comparison."""
        if value is None:
            return ""
        s = str(value)
        return s if case_sensitive else s.lower()

    # Operator implementations
    def _op_equals(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(value, case_sensitive) == self._normalize(filter_value, case_sensitive)

    def _op_not_equals(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(value, case_sensitive) != self._normalize(filter_value, case_sensitive)

    def _op_contains(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(filter_value, case_sensitive) in self._normalize(value, case_sensitive)

    def _op_not_contains(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(filter_value, case_sensitive) not in self._normalize(value, case_sensitive)

    def _op_starts_with(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(value, case_sensitive).startswith(self._normalize(filter_value, case_sensitive))

    def _op_ends_with(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return self._normalize(value, case_sensitive).endswith(self._normalize(filter_value, case_sensitive))

    def _op_in(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        if not isinstance(filter_value, (list, tuple, set)):
            filter_value = [filter_value]
        norm_value = self._normalize(value, case_sensitive)
        return norm_value in [self._normalize(v, case_sensitive) for v in filter_value]

    def _op_not_in(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return not self._op_in(value, filter_value, case_sensitive)

    def _op_gt(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        try:
            return float(value) > float(filter_value)
        except (ValueError, TypeError):
            return False

    def _op_lt(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        try:
            return float(value) < float(filter_value)
        except (ValueError, TypeError):
            return False

    def _op_gte(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        try:
            return float(value) >= float(filter_value)
        except (ValueError, TypeError):
            return False

    def _op_lte(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        try:
            return float(value) <= float(filter_value)
        except (ValueError, TypeError):
            return False

    def _op_between(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        if not isinstance(filter_value, (list, tuple)) or len(filter_value) != 2:
            return False
        try:
            v = float(value)
            return float(filter_value[0]) <= v <= float(filter_value[1])
        except (ValueError, TypeError):
            return False

    def _op_is_null(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return value is None or value == "" or value == []

    def _op_is_not_null(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return not self._op_is_null(value, filter_value, case_sensitive)

    def _op_is_true(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return bool(value) is True

    def _op_is_false(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        return bool(value) is False

    def _op_regex(self, value: Any, filter_value: Any, case_sensitive: bool) -> bool:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            return bool(re.search(filter_value, str(value), flags))
        except re.error:
            return False


class QueryBuilder:
    """
    Fluent query builder for budget data.

    Provides a chainable interface for building complex queries.
    """

    def __init__(self, records: List[Dict[str, Any]]):
        self._records = records
        self._filters: List[Filter] = []
        self._order_by: List[tuple] = []
        self._limit: int = None
        self._offset: int = 0
        self._engine = FilterEngine()

    def where(self, field: str, operator: Union[str, FilterOperator], value: Any = None) -> 'QueryBuilder':
        """Add a filter condition."""
        if isinstance(operator, str):
            operator = FilterOperator(operator)
        self._filters.append(Filter(field=field, operator=operator, value=value))
        return self

    def agency(self, *agencies: str) -> 'QueryBuilder':
        """Filter by agency name(s)."""
        if len(agencies) == 1:
            return self.where('agency', FilterOperator.EQUALS, agencies[0])
        return self.where('agency', FilterOperator.IN, list(agencies))

    def budget_type(self, *types: str) -> 'QueryBuilder':
        """Filter by budget type."""
        if len(types) == 1:
            return self.where('budget_type', FilterOperator.EQUALS, types[0])
        return self.where('budget_type', FilterOperator.IN, list(types))

    def fund_type(self, *types: str) -> 'QueryBuilder':
        """Filter by fund type."""
        if len(types) == 1:
            return self.where('fund_type', FilterOperator.CONTAINS, types[0])
        # For multiple, use regex
        pattern = '|'.join(types)
        return self.where('fund_type', FilterOperator.REGEX, pattern)

    def chapter_year(self, *years: str) -> 'QueryBuilder':
        """Filter by chapter year."""
        if len(years) == 1:
            return self.where('chapter_year', FilterOperator.EQUALS, years[0])
        return self.where('chapter_year', FilterOperator.IN, list(years))

    def fiscal_year(self, *years: str) -> 'QueryBuilder':
        """Filter by fiscal year."""
        if len(years) == 1:
            return self.where('fiscal_year', FilterOperator.EQUALS, years[0])
        return self.where('fiscal_year', FilterOperator.IN, list(years))

    def min_amount(self, amount: int) -> 'QueryBuilder':
        """Filter by minimum reappropriation amount."""
        return self.where('reappropriation_amount', FilterOperator.GREATER_EQUAL, amount)

    def max_amount(self, amount: int) -> 'QueryBuilder':
        """Filter by maximum reappropriation amount."""
        return self.where('reappropriation_amount', FilterOperator.LESS_EQUAL, amount)

    def amount_range(self, min_amt: int, max_amt: int) -> 'QueryBuilder':
        """Filter by amount range."""
        return self.where('reappropriation_amount', FilterOperator.BETWEEN, [min_amt, max_amt])

    def has_transfer_authority(self, value: bool = True) -> 'QueryBuilder':
        """Filter by transfer authority."""
        op = FilterOperator.IS_TRUE if value else FilterOperator.IS_FALSE
        return self.where('has_transfer_authority', op)

    def requires_approval(self, value: bool = True) -> 'QueryBuilder':
        """Filter by approval requirement."""
        op = FilterOperator.IS_TRUE if value else FilterOperator.IS_FALSE
        return self.where('requires_approval', op)

    def category(self, *categories: str) -> 'QueryBuilder':
        """Filter by program category."""
        if len(categories) == 1:
            return self.where('program_category', FilterOperator.EQUALS, categories[0])
        return self.where('program_category', FilterOperator.IN, list(categories))

    def search(self, pattern: str) -> 'QueryBuilder':
        """Search in bill_language using regex."""
        return self.where('bill_language', FilterOperator.REGEX, pattern)

    def order_by(self, field: str, direction: str = 'asc') -> 'QueryBuilder':
        """Order results by field."""
        self._order_by.append((field, direction.lower()))
        return self

    def limit(self, n: int) -> 'QueryBuilder':
        """Limit number of results."""
        self._limit = n
        return self

    def offset(self, n: int) -> 'QueryBuilder':
        """Skip first n results."""
        self._offset = n
        return self

    def execute(self) -> List[Dict[str, Any]]:
        """Execute query and return results."""
        # Apply filters
        result = self._engine.apply(self._records, self._filters)

        # Apply ordering
        for field, direction in reversed(self._order_by):
            reverse = direction == 'desc'
            result = sorted(
                result,
                key=lambda r: (r.get(field) is None, r.get(field, 0)),
                reverse=reverse
            )

        # Apply offset and limit
        if self._offset:
            result = result[self._offset:]
        if self._limit:
            result = result[:self._limit]

        return result

    def count(self) -> int:
        """Return count of matching records."""
        return len(self._engine.apply(self._records, self._filters))

    def first(self) -> Dict[str, Any] | None:
        """Return first matching record."""
        results = self.limit(1).execute()
        return results[0] if results else None

    def exists(self) -> bool:
        """Check if any records match."""
        return self.count() > 0

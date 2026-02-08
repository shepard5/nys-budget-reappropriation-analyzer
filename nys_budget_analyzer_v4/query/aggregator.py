"""
Aggregation engine for grouping and summarizing budget data.
"""

from typing import Any, List, Dict, Callable, Optional
from collections import defaultdict
import statistics


class Aggregator:
    """
    Groups and aggregates budget records.

    Supports group-by operations with various aggregation functions.
    """

    def __init__(self, records: List[Dict[str, Any]]):
        self._records = records
        self._group_by_fields: List[str] = []
        self._aggregations: Dict[str, tuple] = {}  # alias -> (func_name, field)

    def group_by(self, *fields: str) -> 'Aggregator':
        """Add fields to group by."""
        self._group_by_fields.extend(fields)
        return self

    def sum(self, field: str, alias: str = None) -> 'Aggregator':
        """Add sum aggregation."""
        alias = alias or f'sum_{field}'
        self._aggregations[alias] = ('sum', field)
        return self

    def count(self, alias: str = 'count') -> 'Aggregator':
        """Add count aggregation."""
        self._aggregations[alias] = ('count', '*')
        return self

    def avg(self, field: str, alias: str = None) -> 'Aggregator':
        """Add average aggregation."""
        alias = alias or f'avg_{field}'
        self._aggregations[alias] = ('avg', field)
        return self

    def min(self, field: str, alias: str = None) -> 'Aggregator':
        """Add min aggregation."""
        alias = alias or f'min_{field}'
        self._aggregations[alias] = ('min', field)
        return self

    def max(self, field: str, alias: str = None) -> 'Aggregator':
        """Add max aggregation."""
        alias = alias or f'max_{field}'
        self._aggregations[alias] = ('max', field)
        return self

    def median(self, field: str, alias: str = None) -> 'Aggregator':
        """Add median aggregation."""
        alias = alias or f'median_{field}'
        self._aggregations[alias] = ('median', field)
        return self

    def stdev(self, field: str, alias: str = None) -> 'Aggregator':
        """Add standard deviation aggregation."""
        alias = alias or f'stdev_{field}'
        self._aggregations[alias] = ('stdev', field)
        return self

    def execute(self) -> List[Dict[str, Any]]:
        """Execute aggregation and return results."""
        if not self._group_by_fields:
            # No grouping - aggregate entire dataset
            return [self._aggregate_group(self._records)]

        # Group records
        groups = defaultdict(list)
        for record in self._records:
            key = self._make_group_key(record)
            groups[key].append(record)

        # Aggregate each group
        results = []
        for group_key, group_records in groups.items():
            result = self._aggregate_group(group_records)

            # Add group-by field values
            if isinstance(group_key, tuple):
                for i, field in enumerate(self._group_by_fields):
                    result[field] = group_key[i]
            else:
                result[self._group_by_fields[0]] = group_key

            results.append(result)

        return results

    def _make_group_key(self, record: Dict[str, Any]) -> tuple:
        """Create grouping key from record."""
        if len(self._group_by_fields) == 1:
            return record.get(self._group_by_fields[0])
        return tuple(record.get(f) for f in self._group_by_fields)

    def _aggregate_group(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply aggregations to a group of records."""
        result = {}

        for alias, (func_name, field) in self._aggregations.items():
            if func_name == 'count':
                result[alias] = len(records)
            elif func_name == 'sum':
                values = [self._get_numeric(r, field) for r in records]
                result[alias] = sum(v for v in values if v is not None)
            elif func_name == 'avg':
                values = [self._get_numeric(r, field) for r in records]
                values = [v for v in values if v is not None]
                result[alias] = statistics.mean(values) if values else None
            elif func_name == 'min':
                values = [self._get_numeric(r, field) for r in records]
                values = [v for v in values if v is not None]
                result[alias] = min(values) if values else None
            elif func_name == 'max':
                values = [self._get_numeric(r, field) for r in records]
                values = [v for v in values if v is not None]
                result[alias] = max(values) if values else None
            elif func_name == 'median':
                values = [self._get_numeric(r, field) for r in records]
                values = [v for v in values if v is not None]
                result[alias] = statistics.median(values) if values else None
            elif func_name == 'stdev':
                values = [self._get_numeric(r, field) for r in records]
                values = [v for v in values if v is not None]
                result[alias] = statistics.stdev(values) if len(values) > 1 else None

        return result

    def _get_numeric(self, record: Dict[str, Any], field: str) -> Optional[float]:
        """Get numeric value from record field."""
        value = record.get(field)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None


def aggregate(records: List[Dict[str, Any]]) -> Aggregator:
    """Create an aggregator for the given records."""
    return Aggregator(records)


def quick_stats(records: List[Dict[str, Any]], amount_field: str = 'reappropriation_amount') -> Dict[str, Any]:
    """
    Calculate quick summary statistics for a set of records.

    Returns dict with count, sum, mean, median, min, max, stdev.
    """
    if not records:
        return {
            'count': 0,
            'sum': 0,
            'mean': None,
            'median': None,
            'min': None,
            'max': None,
            'stdev': None,
        }

    values = []
    for r in records:
        try:
            v = float(r.get(amount_field, 0) or 0)
            values.append(v)
        except (ValueError, TypeError):
            pass

    if not values:
        return {
            'count': len(records),
            'sum': 0,
            'mean': None,
            'median': None,
            'min': None,
            'max': None,
            'stdev': None,
        }

    return {
        'count': len(records),
        'sum': sum(values),
        'mean': statistics.mean(values),
        'median': statistics.median(values),
        'min': min(values),
        'max': max(values),
        'stdev': statistics.stdev(values) if len(values) > 1 else None,
    }


def group_summary(records: List[Dict[str, Any]], group_field: str,
                  amount_field: str = 'reappropriation_amount',
                  top_n: int = None) -> List[Dict[str, Any]]:
    """
    Quick group-by summary with count and sum.

    Args:
        records: List of records
        group_field: Field to group by
        amount_field: Field to sum
        top_n: Limit to top N groups by sum (optional)

    Returns:
        List of dicts with group value, count, and total amount
    """
    agg = (Aggregator(records)
           .group_by(group_field)
           .count()
           .sum(amount_field, 'total_amount')
           .execute())

    # Sort by total amount descending
    agg.sort(key=lambda x: x.get('total_amount', 0), reverse=True)

    if top_n:
        agg = agg[:top_n]

    return agg

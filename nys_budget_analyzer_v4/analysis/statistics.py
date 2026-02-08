"""
Statistical analysis functions for budget data.
"""

import statistics
from typing import List, Dict, Any, Optional
from collections import defaultdict


def distribution_stats(values: List[float]) -> Dict[str, Any]:
    """
    Calculate comprehensive distribution statistics.

    Returns dict with count, sum, mean, median, mode, stdev, variance,
    min, max, range, quartiles, and percentiles.
    """
    if not values:
        return {
            'count': 0,
            'sum': 0,
            'mean': None,
            'median': None,
            'mode': None,
            'stdev': None,
            'variance': None,
            'min': None,
            'max': None,
            'range': None,
            'q1': None,
            'q3': None,
            'iqr': None,
            'p10': None,
            'p90': None,
            'p95': None,
            'p99': None,
        }

    sorted_values = sorted(values)
    n = len(sorted_values)

    # Basic stats
    result = {
        'count': n,
        'sum': sum(values),
        'mean': statistics.mean(values),
        'median': statistics.median(values),
        'min': min(values),
        'max': max(values),
        'range': max(values) - min(values),
    }

    # Mode (may fail if no unique mode)
    try:
        result['mode'] = statistics.mode(values)
    except statistics.StatisticsError:
        result['mode'] = None

    # Stdev and variance (need at least 2 values)
    if n > 1:
        result['stdev'] = statistics.stdev(values)
        result['variance'] = statistics.variance(values)
    else:
        result['stdev'] = None
        result['variance'] = None

    # Quartiles
    result['q1'] = sorted_values[n // 4] if n >= 4 else sorted_values[0]
    result['q3'] = sorted_values[3 * n // 4] if n >= 4 else sorted_values[-1]
    result['iqr'] = result['q3'] - result['q1']

    # Percentiles
    result['p10'] = sorted_values[int(n * 0.10)] if n >= 10 else sorted_values[0]
    result['p90'] = sorted_values[int(n * 0.90)] if n >= 10 else sorted_values[-1]
    result['p95'] = sorted_values[int(n * 0.95)] if n >= 20 else sorted_values[-1]
    result['p99'] = sorted_values[int(n * 0.99)] if n >= 100 else sorted_values[-1]

    return result


def agency_rankings(records: List[Dict[str, Any]],
                    amount_field: str = 'reappropriation_amount',
                    sort_by: str = 'total_amount') -> List[Dict[str, Any]]:
    """
    Rank agencies by various metrics.

    Args:
        records: List of budget records
        amount_field: Field containing the amount to analyze
        sort_by: Metric to sort by ('total_amount', 'count', 'avg_amount', 'max_amount')

    Returns:
        List of dicts with agency stats, sorted by specified metric
    """
    agency_stats = defaultdict(lambda: {
        'total_amount': 0,
        'count': 0,
        'amounts': [],
        'oldest_year': 9999,
        'newest_year': 0,
    })

    for r in records:
        agency = r.get('agency', 'Unknown')
        amount = r.get(amount_field, 0) or 0

        try:
            amount = float(amount)
        except (ValueError, TypeError):
            amount = 0

        stats = agency_stats[agency]
        stats['total_amount'] += amount
        stats['count'] += 1
        stats['amounts'].append(amount)

        # Track year range
        try:
            year = int(r.get('chapter_year', 0))
            if year > 0:
                stats['oldest_year'] = min(stats['oldest_year'], year)
                stats['newest_year'] = max(stats['newest_year'], year)
        except (ValueError, TypeError):
            pass

    # Calculate derived stats
    results = []
    for agency, stats in agency_stats.items():
        amounts = stats['amounts']
        result = {
            'agency': agency,
            'total_amount': stats['total_amount'],
            'count': stats['count'],
            'avg_amount': statistics.mean(amounts) if amounts else 0,
            'median_amount': statistics.median(amounts) if amounts else 0,
            'max_amount': max(amounts) if amounts else 0,
            'min_amount': min(amounts) if amounts else 0,
            'oldest_year': stats['oldest_year'] if stats['oldest_year'] < 9999 else None,
            'newest_year': stats['newest_year'] if stats['newest_year'] > 0 else None,
        }
        results.append(result)

    # Sort by specified metric
    results.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

    # Add rank
    for i, r in enumerate(results):
        r['rank'] = i + 1

    return results


class BudgetStatistics:
    """
    Comprehensive statistical analysis for budget data.
    """

    def __init__(self, records: List[Dict[str, Any]]):
        self.records = records
        self._amount_field = 'reappropriation_amount'

    def set_amount_field(self, field: str) -> 'BudgetStatistics':
        """Set the field to use for amount calculations."""
        self._amount_field = field
        return self

    def distribution(self) -> Dict[str, Any]:
        """Get distribution statistics for amounts."""
        amounts = self._extract_amounts()
        return distribution_stats(amounts)

    def by_agency(self, top_n: int = None, sort_by: str = 'total_amount') -> List[Dict[str, Any]]:
        """Get statistics grouped by agency."""
        results = agency_rankings(self.records, self._amount_field, sort_by)
        if top_n:
            results = results[:top_n]
        return results

    def by_field(self, field: str, top_n: int = None) -> List[Dict[str, Any]]:
        """Get statistics grouped by any field."""
        groups = defaultdict(list)
        for r in self.records:
            key = r.get(field, 'Unknown')
            groups[key].append(r)

        results = []
        for key, group_records in groups.items():
            amounts = [r.get(self._amount_field, 0) or 0 for r in group_records]
            amounts = [float(a) for a in amounts if a]

            results.append({
                field: key,
                'count': len(group_records),
                'total_amount': sum(amounts),
                'avg_amount': statistics.mean(amounts) if amounts else 0,
            })

        results.sort(key=lambda x: x['total_amount'], reverse=True)

        if top_n:
            results = results[:top_n]

        for i, r in enumerate(results):
            r['rank'] = i + 1

        return results

    def by_year(self, year_field: str = 'chapter_year') -> List[Dict[str, Any]]:
        """Get statistics by year."""
        return self.by_field(year_field)

    def by_category(self, top_n: int = None) -> List[Dict[str, Any]]:
        """Get statistics by program category."""
        return self.by_field('program_category', top_n)

    def by_fund_type(self, top_n: int = None) -> List[Dict[str, Any]]:
        """Get statistics by fund type."""
        return self.by_field('fund_type', top_n)

    def summary(self) -> Dict[str, Any]:
        """Get comprehensive summary statistics."""
        dist = self.distribution()
        by_agency = self.by_agency(top_n=10)
        by_category = self.by_category()
        by_year = self.by_year()

        return {
            'total_records': len(self.records),
            'total_amount': dist['sum'],
            'distribution': dist,
            'top_agencies': by_agency,
            'by_category': by_category,
            'by_year': by_year,
            'unique_agencies': len(set(r.get('agency') for r in self.records)),
            'unique_appropriation_ids': len(set(r.get('appropriation_id') for r in self.records)),
        }

    def _extract_amounts(self) -> List[float]:
        """Extract numeric amounts from records."""
        amounts = []
        for r in self.records:
            try:
                amount = float(r.get(self._amount_field, 0) or 0)
                if amount > 0:
                    amounts.append(amount)
            except (ValueError, TypeError):
                pass
        return amounts

    def percentile_breakdown(self, percentiles: List[int] = None) -> Dict[str, Any]:
        """
        Get amount breakdown by percentiles.

        Shows how total amount is distributed across percentile bands.
        """
        if percentiles is None:
            percentiles = [50, 75, 90, 95, 99]

        amounts = self._extract_amounts()
        if not amounts:
            return {'percentiles': percentiles, 'bands': []}

        sorted_amounts = sorted(amounts, reverse=True)
        n = len(sorted_amounts)
        total = sum(sorted_amounts)

        bands = []
        prev_pct = 0
        cumulative = 0

        for pct in percentiles:
            idx = int(n * (pct / 100))
            band_amounts = sorted_amounts[int(n * prev_pct / 100):idx]
            band_sum = sum(band_amounts)
            cumulative += band_sum

            bands.append({
                'percentile': f'{prev_pct}-{pct}',
                'count': len(band_amounts),
                'sum': band_sum,
                'pct_of_total': (band_sum / total * 100) if total > 0 else 0,
                'cumulative_pct': (cumulative / total * 100) if total > 0 else 0,
            })
            prev_pct = pct

        # Add remaining (top percentile)
        remaining = sorted_amounts[int(n * prev_pct / 100):]
        if remaining:
            remaining_sum = sum(remaining)
            bands.append({
                'percentile': f'{prev_pct}-100',
                'count': len(remaining),
                'sum': remaining_sum,
                'pct_of_total': (remaining_sum / total * 100) if total > 0 else 0,
                'cumulative_pct': 100.0,
            })

        return {
            'percentiles': percentiles,
            'bands': bands,
            'total_records': n,
            'total_amount': total,
        }

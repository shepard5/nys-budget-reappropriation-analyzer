"""
Interactive HTML dashboard generator for budget analysis.

Uses DataTables.js for filtering and Chart.js for visualizations.
"""

import json
from typing import List, Dict, Any, Optional
from pathlib import Path
from collections import defaultdict


class HTMLDashboardGenerator:
    """
    Generates interactive HTML dashboards from budget data.

    Features:
    - DataTables.js for sortable, searchable data tables
    - Chart.js for bar charts, pie charts, and line graphs
    - Summary statistics cards
    - Responsive design
    """

    def __init__(self, records: List[Dict[str, Any]]):
        self.records = records

    def generate(self, output_path: str, title: str = "NYS Budget Analysis Dashboard"):
        """Generate complete HTML dashboard."""
        html = self._build_html(title)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        return output_path

    def _build_html(self, title: str) -> str:
        """Build complete HTML document."""
        # Calculate statistics
        stats = self._calculate_stats()

        # Prepare chart data
        agency_chart_data = self._get_agency_chart_data()
        category_chart_data = self._get_category_chart_data()
        year_chart_data = self._get_year_chart_data()

        # Prepare table data
        table_data = self._prepare_table_data()

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>

    <!-- DataTables CSS -->
    <link rel="stylesheet" href="https://cdn.datatables.net/1.13.7/css/jquery.dataTables.min.css">
    <link rel="stylesheet" href="https://cdn.datatables.net/buttons/2.4.2/css/buttons.dataTables.min.css">

    <style>
        :root {{
            --primary-color: #2c5282;
            --secondary-color: #4a5568;
            --accent-color: #38a169;
            --background: #f7fafc;
            --card-bg: #ffffff;
            --text-primary: #1a202c;
            --text-secondary: #718096;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--background);
            color: var(--text-primary);
            line-height: 1.6;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}

        header {{
            background: var(--primary-color);
            color: white;
            padding: 24px;
            margin-bottom: 24px;
            border-radius: 8px;
        }}

        header h1 {{
            font-size: 1.75rem;
            font-weight: 600;
        }}

        header p {{
            opacity: 0.9;
            margin-top: 4px;
        }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}

        .stat-card {{
            background: var(--card-bg);
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}

        .stat-card h3 {{
            color: var(--text-secondary);
            font-size: 0.875rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .stat-card .value {{
            font-size: 1.75rem;
            font-weight: 700;
            color: var(--primary-color);
            margin-top: 8px;
        }}

        .charts-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }}

        .chart-card {{
            background: var(--card-bg);
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}

        .chart-card h2 {{
            font-size: 1.125rem;
            font-weight: 600;
            margin-bottom: 16px;
            color: var(--text-primary);
        }}

        .chart-container {{
            position: relative;
            height: 300px;
        }}

        .data-section {{
            background: var(--card-bg);
            padding: 24px;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            margin-bottom: 24px;
        }}

        .data-section h2 {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 16px;
        }}

        table.dataTable {{
            font-size: 0.875rem;
        }}

        table.dataTable thead th {{
            background: var(--primary-color);
            color: white;
        }}

        .footer {{
            text-align: center;
            padding: 20px;
            color: var(--text-secondary);
            font-size: 0.875rem;
        }}

        .amount {{
            font-family: 'SF Mono', 'Monaco', monospace;
            text-align: right;
        }}

        @media (max-width: 768px) {{
            .charts-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>{title}</h1>
            <p>Generated from {len(self.records):,} budget records</p>
        </header>

        <!-- Summary Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <h3>Total Records</h3>
                <div class="value">{stats['total_records']:,}</div>
            </div>
            <div class="stat-card">
                <h3>Total Amount</h3>
                <div class="value">${stats['total_amount']:,.0f}</div>
            </div>
            <div class="stat-card">
                <h3>Unique Agencies</h3>
                <div class="value">{stats['unique_agencies']:,}</div>
            </div>
            <div class="stat-card">
                <h3>Unique IDs</h3>
                <div class="value">{stats['unique_ids']:,}</div>
            </div>
            <div class="stat-card">
                <h3>Average Amount</h3>
                <div class="value">${stats['avg_amount']:,.0f}</div>
            </div>
            <div class="stat-card">
                <h3>Year Range</h3>
                <div class="value">{stats['year_range']}</div>
            </div>
        </div>

        <!-- Charts -->
        <div class="charts-grid">
            <div class="chart-card">
                <h2>Top 10 Agencies by Amount</h2>
                <div class="chart-container">
                    <canvas id="agencyChart"></canvas>
                </div>
            </div>
            <div class="chart-card">
                <h2>Distribution by Category</h2>
                <div class="chart-container">
                    <canvas id="categoryChart"></canvas>
                </div>
            </div>
            <div class="chart-card">
                <h2>Amount by Chapter Year</h2>
                <div class="chart-container">
                    <canvas id="yearChart"></canvas>
                </div>
            </div>
        </div>

        <!-- Data Table -->
        <div class="data-section">
            <h2>All Records</h2>
            <table id="dataTable" class="display" style="width:100%">
                <thead>
                    <tr>
                        <th>Agency</th>
                        <th>ID</th>
                        <th>Year</th>
                        <th>Appropriation</th>
                        <th>Reappropriation</th>
                        <th>Category</th>
                        <th>Budget Type</th>
                    </tr>
                </thead>
                <tbody>
                </tbody>
            </table>
        </div>

        <div class="footer">
            <p>NYS Budget Analyzer v4 | Data extracted from official NYS budget documents</p>
        </div>
    </div>

    <!-- Scripts -->
    <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.7/js/jquery.dataTables.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/2.4.2/js/dataTables.buttons.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/2.4.2/js/buttons.html5.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

    <script>
        // Table data
        const tableData = {json.dumps(table_data)};

        // Initialize DataTable
        $(document).ready(function() {{
            $('#dataTable').DataTable({{
                data: tableData,
                columns: [
                    {{ data: 'agency' }},
                    {{ data: 'id' }},
                    {{ data: 'year' }},
                    {{ data: 'appropriation', className: 'amount', render: $.fn.dataTable.render.number(',', '.', 0, '$') }},
                    {{ data: 'reappropriation', className: 'amount', render: $.fn.dataTable.render.number(',', '.', 0, '$') }},
                    {{ data: 'category' }},
                    {{ data: 'budget_type' }}
                ],
                pageLength: 25,
                order: [[4, 'desc']],
                dom: 'Bfrtip',
                buttons: ['copy', 'csv', 'excel']
            }});
        }});

        // Agency Chart
        const agencyCtx = document.getElementById('agencyChart').getContext('2d');
        new Chart(agencyCtx, {{
            type: 'bar',
            data: {{
                labels: {json.dumps(agency_chart_data['labels'])},
                datasets: [{{
                    label: 'Amount ($ Millions)',
                    data: {json.dumps(agency_chart_data['values'])},
                    backgroundColor: 'rgba(44, 82, 130, 0.8)',
                    borderColor: 'rgba(44, 82, 130, 1)',
                    borderWidth: 1
                }}]
            }},
            options: {{
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{ display: false }}
                }},
                scales: {{
                    x: {{
                        beginAtZero: true,
                        ticks: {{
                            callback: function(value) {{ return '$' + value + 'M'; }}
                        }}
                    }}
                }}
            }}
        }});

        // Category Chart
        const categoryCtx = document.getElementById('categoryChart').getContext('2d');
        new Chart(categoryCtx, {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(category_chart_data['labels'])},
                datasets: [{{
                    data: {json.dumps(category_chart_data['values'])},
                    backgroundColor: [
                        '#2c5282', '#38a169', '#d69e2e', '#e53e3e', '#805ad5',
                        '#dd6b20', '#319795', '#d53f8c', '#3182ce', '#718096'
                    ]
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        position: 'right',
                        labels: {{ boxWidth: 12, padding: 8, font: {{ size: 11 }} }}
                    }}
                }}
            }}
        }});

        // Year Chart
        const yearCtx = document.getElementById('yearChart').getContext('2d');
        new Chart(yearCtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(year_chart_data['labels'])},
                datasets: [{{
                    label: 'Amount ($ Millions)',
                    data: {json.dumps(year_chart_data['values'])},
                    borderColor: 'rgba(56, 161, 105, 1)',
                    backgroundColor: 'rgba(56, 161, 105, 0.1)',
                    fill: true,
                    tension: 0.3
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{ display: false }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: true,
                        ticks: {{
                            callback: function(value) {{ return '$' + value + 'M'; }}
                        }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>'''

    def _calculate_stats(self) -> Dict[str, Any]:
        """Calculate summary statistics."""
        total_amount = sum(self._get_amount(r) for r in self.records)
        unique_agencies = len(set(r.get('agency', '') for r in self.records))
        unique_ids = len(set(r.get('appropriation_id', '') for r in self.records))

        years = [r.get('chapter_year') for r in self.records if r.get('chapter_year')]
        years = [int(y) for y in years if y and str(y).isdigit()]

        return {
            'total_records': len(self.records),
            'total_amount': total_amount,
            'unique_agencies': unique_agencies,
            'unique_ids': unique_ids,
            'avg_amount': total_amount / len(self.records) if self.records else 0,
            'year_range': f"{min(years) if years else 'N/A'} - {max(years) if years else 'N/A'}"
        }

    def _get_agency_chart_data(self) -> Dict[str, List]:
        """Get top 10 agencies for bar chart."""
        agency_totals = defaultdict(float)
        for r in self.records:
            agency = r.get('agency', 'Unknown')
            agency_totals[agency] += self._get_amount(r)

        sorted_agencies = sorted(agency_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            'labels': [a[0][:35] for a in sorted_agencies],
            'values': [round(a[1] / 1_000_000, 1) for a in sorted_agencies]
        }

    def _get_category_chart_data(self) -> Dict[str, List]:
        """Get category distribution for pie chart."""
        cat_totals = defaultdict(float)
        for r in self.records:
            cat = r.get('program_category') or 'Uncategorized'
            cat_totals[cat] += self._get_amount(r)

        sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            'labels': [c[0].replace('_', ' ').title() for c in sorted_cats],
            'values': [round(c[1] / 1_000_000, 1) for c in sorted_cats]
        }

    def _get_year_chart_data(self) -> Dict[str, List]:
        """Get year trend for line chart."""
        year_totals = defaultdict(float)
        for r in self.records:
            year = r.get('chapter_year')
            if year and str(year).isdigit():
                year_totals[int(year)] += self._get_amount(r)

        sorted_years = sorted(year_totals.items())

        return {
            'labels': [str(y[0]) for y in sorted_years],
            'values': [round(y[1] / 1_000_000, 1) for y in sorted_years]
        }

    def _prepare_table_data(self) -> List[Dict[str, Any]]:
        """Prepare data for DataTables."""
        table_data = []
        for r in self.records:
            table_data.append({
                'agency': r.get('agency', '')[:50],
                'id': r.get('appropriation_id', ''),
                'year': r.get('chapter_year', ''),
                'appropriation': self._get_amount(r, 'appropriation_amount'),
                'reappropriation': self._get_amount(r, 'reappropriation_amount'),
                'category': (r.get('program_category') or '').replace('_', ' ').title(),
                'budget_type': r.get('budget_type', '')
            })
        return table_data

    def _get_amount(self, record: Dict[str, Any], field: str = 'reappropriation_amount') -> float:
        """Extract amount from record."""
        val = record.get(field)
        if val:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

        # Fallback to appropriation amount
        if field == 'reappropriation_amount':
            val = record.get('appropriation_amount')
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0


def generate_html_dashboard(records: List[Dict[str, Any]],
                            output_path: str,
                            title: str = "NYS Budget Analysis Dashboard") -> str:
    """
    Generate an interactive HTML dashboard from budget records.

    Args:
        records: List of budget record dictionaries
        output_path: Path for output HTML file
        title: Dashboard title

    Returns:
        Path to generated file
    """
    generator = HTMLDashboardGenerator(records)
    return generator.generate(output_path, title)

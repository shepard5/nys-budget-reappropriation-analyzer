"""Report generation modules."""

from .excel_report import ExcelReportGenerator, generate_excel_report
from .html_report import HTMLDashboardGenerator, generate_html_dashboard

__all__ = [
    'ExcelReportGenerator',
    'generate_excel_report',
    'HTMLDashboardGenerator',
    'generate_html_dashboard',
]

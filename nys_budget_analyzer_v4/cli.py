#!/usr/bin/env python3
"""
NYS Budget Analyzer v4 - Command Line Interface

Usage:
    nys-budget analyze <enacted_pdf> <executive_pdf> [-o OUTPUT_DIR]
    nys-budget query <data_file> [--agency AGENCY] [--min-amount AMT] ...
    nys-budget stats <data_file> [--metric METRIC] [--group-by FIELD]
    nys-budget enrich <csv_file> [-o OUTPUT_FILE]
"""

import sys
import json
import csv
from pathlib import Path
from typing import List, Dict, Any, Optional

import click

from .extraction.entity_parser import EntityParser, parse
from .query.filter_engine import FilterEngine, Filter, FilterOperator, QueryBuilder
from .query.aggregator import Aggregator, quick_stats, group_summary
from .analysis.statistics import BudgetStatistics, distribution_stats, agency_rankings


@click.group()
@click.version_option(version='4.0.0', prog_name='nys-budget')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def cli(ctx, verbose):
    """NYS Budget Analyzer v4 - God-Level Budget Analysis Tool"""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose


# ============================================================
# ENRICH COMMAND - Add parsed entities to v3 CSV
# ============================================================

@cli.command()
@click.argument('csv_file', type=click.Path(exists=True))
@click.option('--output', '-o', type=click.Path(), help='Output file path')
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['csv', 'json']),
              default='csv', help='Output format')
@click.pass_context
def enrich(ctx, csv_file, output, output_format):
    """
    Enrich a v3 CSV file with parsed entities from bill_language.

    Adds columns for program_purpose, program_category, recipient_name,
    has_transfer_authority, requires_approval, etc.

    Examples:
        nys-budget enrich discontinued.csv -o discontinued_enriched.csv
        nys-budget enrich data.csv -f json -o data_enriched.json
    """
    verbose = ctx.obj.get('verbose', False)

    # Load CSV
    records = load_csv(csv_file)
    if verbose:
        click.echo(f"Loaded {len(records)} records from {csv_file}")

    # Parse entities
    parser = EntityParser()
    enriched_records = []

    with click.progressbar(records, label='Parsing entities') as bar:
        for record in bar:
            bill_language = record.get('bill_language', '')
            entities = parser.parse(bill_language)
            enriched = {**record, **entities}
            enriched_records.append(enriched)

    # Determine output path
    if not output:
        input_path = Path(csv_file)
        suffix = '.json' if output_format == 'json' else '.csv'
        output = input_path.parent / f"{input_path.stem}_enriched{suffix}"

    # Write output
    if output_format == 'json':
        write_json(enriched_records, output)
    else:
        write_csv(enriched_records, output)

    click.echo(f"Enriched data saved to: {output}")


# ============================================================
# QUERY COMMAND - Filter and search data
# ============================================================

@cli.command()
@click.argument('data_file', type=click.Path(exists=True))
@click.option('--agency', '-a', multiple=True, help='Filter by agency (can specify multiple)')
@click.option('--budget-type', '-b', multiple=True, help='Filter by budget type')
@click.option('--fund-type', multiple=True, help='Filter by fund type')
@click.option('--chapter-year', '-y', multiple=True, help='Filter by chapter year')
@click.option('--min-amount', type=int, help='Minimum reappropriation amount')
@click.option('--max-amount', type=int, help='Maximum reappropriation amount')
@click.option('--category', '-c', multiple=True, help='Filter by program category')
@click.option('--has-transfer', is_flag=True, help='Only items with transfer authority')
@click.option('--requires-approval', is_flag=True, help='Only items requiring approval')
@click.option('--search', '-s', help='Search in bill_language (regex)')
@click.option('--order-by', help='Order by field (field:asc or field:desc)')
@click.option('--limit', '-n', type=int, help='Limit number of results')
@click.option('--output', '-o', type=click.Path(), help='Output file')
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['csv', 'json', 'table']),
              default='table', help='Output format')
@click.pass_context
def query(ctx, data_file, agency, budget_type, fund_type, chapter_year,
          min_amount, max_amount, category, has_transfer, requires_approval,
          search, order_by, limit, output, output_format):
    """
    Query and filter budget data.

    Examples:
        nys-budget query data.csv --agency "DEPARTMENT OF HEALTH" --min-amount 1000000
        nys-budget query data.json -a "EDUCATION DEPARTMENT" --category education
        nys-budget query data.csv --search "transfer.*department" --has-transfer
    """
    verbose = ctx.obj.get('verbose', False)

    # Load data
    records = load_data(data_file)
    if verbose:
        click.echo(f"Loaded {len(records)} records")

    # Build query
    qb = QueryBuilder(records)

    if agency:
        qb.agency(*agency)
    if budget_type:
        qb.budget_type(*budget_type)
    if fund_type:
        qb.fund_type(*fund_type)
    if chapter_year:
        qb.chapter_year(*chapter_year)
    if min_amount:
        qb.min_amount(min_amount)
    if max_amount:
        qb.max_amount(max_amount)
    if category:
        qb.category(*category)
    if has_transfer:
        qb.has_transfer_authority(True)
    if requires_approval:
        qb.requires_approval(True)
    if search:
        qb.search(search)
    if order_by:
        parts = order_by.split(':')
        field = parts[0]
        direction = parts[1] if len(parts) > 1 else 'asc'
        qb.order_by(field, direction)
    if limit:
        qb.limit(limit)

    # Execute
    results = qb.execute()
    click.echo(f"Found {len(results)} matching records")

    # Output
    if output:
        if output_format == 'json' or output.endswith('.json'):
            write_json(results, output)
        else:
            write_csv(results, output)
        click.echo(f"Results saved to: {output}")
    elif output_format == 'table':
        print_table(results, max_rows=limit or 20)
    elif output_format == 'json':
        click.echo(json.dumps(results[:limit or 20], indent=2))
    else:
        # CSV to stdout
        if results:
            writer = csv.DictWriter(sys.stdout, fieldnames=results[0].keys())
            writer.writeheader()
            for r in results[:limit or 100]:
                writer.writerow(r)


# ============================================================
# STATS COMMAND - Calculate statistics
# ============================================================

@cli.command()
@click.argument('data_file', type=click.Path(exists=True))
@click.option('--metric', '-m',
              type=click.Choice(['summary', 'distribution', 'agencies', 'categories', 'years', 'percentiles']),
              default='summary', help='Statistical metric to calculate')
@click.option('--group-by', '-g', help='Group statistics by field')
@click.option('--top', '-n', type=int, default=20, help='Limit to top N results')
@click.option('--sort-by', help='Sort by metric (for rankings)')
@click.option('--output', '-o', type=click.Path(), help='Output file')
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['json', 'table', 'csv']),
              default='table', help='Output format')
@click.pass_context
def stats(ctx, data_file, metric, group_by, top, sort_by, output, output_format):
    """
    Calculate statistical metrics on budget data.

    Examples:
        nys-budget stats data.csv -m summary
        nys-budget stats data.csv -m agencies -n 15
        nys-budget stats data.csv -m distribution
        nys-budget stats data.csv -m percentiles
    """
    verbose = ctx.obj.get('verbose', False)

    # Load data
    records = load_data(data_file)
    if verbose:
        click.echo(f"Loaded {len(records)} records")

    stats_calc = BudgetStatistics(records)

    # Calculate requested metric
    if metric == 'summary':
        result = stats_calc.summary()
    elif metric == 'distribution':
        result = stats_calc.distribution()
    elif metric == 'agencies':
        sort_key = sort_by or 'total_amount'
        result = stats_calc.by_agency(top_n=top, sort_by=sort_key)
    elif metric == 'categories':
        result = stats_calc.by_category(top_n=top)
    elif metric == 'years':
        result = stats_calc.by_year()
    elif metric == 'percentiles':
        result = stats_calc.percentile_breakdown()
    else:
        result = {}

    # Group by custom field if specified
    if group_by and metric not in ['summary', 'distribution', 'percentiles']:
        result = stats_calc.by_field(group_by, top_n=top)

    # Output
    if output:
        if output.endswith('.json') or output_format == 'json':
            write_json(result, output)
        elif output.endswith('.csv') or output_format == 'csv':
            if isinstance(result, list):
                write_csv(result, output)
            else:
                # Convert dict to list for CSV
                write_json(result, output.replace('.csv', '.json'))
        click.echo(f"Results saved to: {output}")
    else:
        if output_format == 'json':
            click.echo(json.dumps(result, indent=2, default=str))
        elif output_format == 'table' and isinstance(result, list):
            print_table(result)
        else:
            # Pretty print dict
            print_stats(result)


# ============================================================
# AGGREGATE COMMAND - Group and aggregate data
# ============================================================

@cli.command()
@click.argument('data_file', type=click.Path(exists=True))
@click.option('--group-by', '-g', multiple=True, required=True, help='Fields to group by')
@click.option('--sum', 'sum_field', help='Field to sum')
@click.option('--count/--no-count', default=True, help='Include count')
@click.option('--avg', 'avg_field', help='Field to average')
@click.option('--order-by', help='Order by field:direction')
@click.option('--top', '-n', type=int, help='Limit to top N')
@click.option('--output', '-o', type=click.Path(), help='Output file')
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['csv', 'json', 'table']),
              default='table', help='Output format')
@click.pass_context
def aggregate(ctx, data_file, group_by, sum_field, count, avg_field, order_by, top, output, output_format):
    """
    Aggregate data by grouping fields.

    Examples:
        nys-budget aggregate data.csv -g agency --sum reappropriation_amount
        nys-budget aggregate data.csv -g agency -g fund_type --sum reappropriation_amount
    """
    records = load_data(data_file)

    agg = Aggregator(records)
    for field in group_by:
        agg.group_by(field)

    if count:
        agg.count()
    if sum_field:
        agg.sum(sum_field, 'total_amount')
    if avg_field:
        agg.avg(avg_field, 'avg_amount')

    results = agg.execute()

    # Order
    if order_by:
        parts = order_by.split(':')
        field = parts[0]
        reverse = len(parts) > 1 and parts[1].lower() == 'desc'
        results.sort(key=lambda x: x.get(field, 0) or 0, reverse=reverse)

    if top:
        results = results[:top]

    # Output
    if output:
        if output_format == 'json' or output.endswith('.json'):
            write_json(results, output)
        else:
            write_csv(results, output)
        click.echo(f"Results saved to: {output}")
    elif output_format == 'table':
        print_table(results)
    else:
        click.echo(json.dumps(results, indent=2))


# ============================================================
# REPORT COMMAND - Generate formatted reports
# ============================================================

@cli.command()
@click.argument('data_file', type=click.Path(exists=True))
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['excel', 'html', 'both']),
              default='html', help='Report format')
@click.option('--output', '-o', type=click.Path(), help='Output file path')
@click.option('--title', '-t', default='NYS Budget Analysis',
              help='Report title')
@click.pass_context
def report(ctx, data_file, output_format, output, title):
    """
    Generate formatted reports (Excel or HTML dashboard).

    Examples:
        nys-budget report data.csv -f html -o dashboard.html
        nys-budget report discontinued.csv -f excel -o analysis.xlsx
        nys-budget report data.json -f both -t "Discontinued Reappropriations 2025-26"
    """
    from .reports import generate_excel_report, generate_html_dashboard

    verbose = ctx.obj.get('verbose', False)

    # Load data
    records = load_data(data_file)
    if verbose:
        click.echo(f"Loaded {len(records)} records from {data_file}")

    # Determine output paths
    input_path = Path(data_file)
    base_name = input_path.stem

    outputs_generated = []

    if output_format in ('excel', 'both'):
        excel_path = output if output and output.endswith('.xlsx') else f"{base_name}_report.xlsx"
        try:
            generate_excel_report(records, excel_path, title)
            outputs_generated.append(excel_path)
            click.echo(f"Excel report saved to: {excel_path}")
        except ImportError as e:
            click.echo(f"Warning: {e}")
            click.echo("Install openpyxl for Excel export: pip install openpyxl")

    if output_format in ('html', 'both'):
        html_path = output if output and output.endswith('.html') else f"{base_name}_dashboard.html"
        generate_html_dashboard(records, html_path, title)
        outputs_generated.append(html_path)
        click.echo(f"HTML dashboard saved to: {html_path}")

    if outputs_generated:
        click.echo(f"\nGenerated {len(outputs_generated)} report(s)")


# ============================================================
# CACHE COMMAND - Manage extraction cache
# ============================================================

@cli.command()
@click.option('--list', '-l', 'list_cache', is_flag=True, help='List all cached extractions')
@click.option('--stats', '-s', is_flag=True, help='Show cache statistics')
@click.option('--clear', '-c', is_flag=True, help='Clear all cached data')
@click.option('--clear-file', type=click.Path(), help='Clear cache for specific file')
@click.option('--info', '-i', type=click.Path(), help='Show cache info for specific file')
@click.pass_context
def cache(ctx, list_cache, stats, clear, clear_file, info):
    """
    Manage the extraction cache.

    The cache stores PDF extraction results to avoid re-parsing unchanged files.
    Cache location: ~/.nys_budget_cache/

    Examples:
        nys-budget cache --stats         # Show cache statistics
        nys-budget cache --list          # List all cached files
        nys-budget cache --info file.pdf # Check if file is cached
        nys-budget cache --clear         # Clear all cache
        nys-budget cache --clear-file file.pdf  # Clear specific file
    """
    from .storage import get_cache

    extraction_cache = get_cache()

    if stats:
        cache_stats = extraction_cache.get_stats()
        click.echo("=== Cache Statistics ===")
        click.echo(f"Cache directory: {cache_stats['cache_dir']}")
        click.echo(f"Cached files: {cache_stats['cached_files']}")
        click.echo(f"Total records: {cache_stats['total_records']:,}")
        click.echo(f"Cache size: {cache_stats['cache_size_mb']} MB")
        click.echo(f"Cache version: {cache_stats['version']}")

    elif list_cache:
        cached = extraction_cache.list_cached()
        if not cached:
            click.echo("No cached extractions found")
            return

        click.echo(f"=== Cached Extractions ({len(cached)} files) ===")
        for entry in cached:
            click.echo(f"\n  File: {entry['file_name']}")
            click.echo(f"  Path: {entry['file_path']}")
            click.echo(f"  Records: {entry['record_count']:,}")
            click.echo(f"  Extracted: {entry['extraction_time']}")

    elif clear:
        if click.confirm('Are you sure you want to clear the entire cache?'):
            extraction_cache.clear_cache()
            click.echo("Cache cleared successfully")
        else:
            click.echo("Cache clear cancelled")

    elif clear_file:
        if extraction_cache.is_cached(clear_file):
            extraction_cache.clear_cache(clear_file)
            click.echo(f"Cache cleared for: {clear_file}")
        else:
            click.echo(f"File not in cache: {clear_file}")

    elif info:
        cache_info = extraction_cache.get_cache_info(info)
        if cache_info:
            click.echo("=== Cache Info ===")
            click.echo(f"File: {cache_info['file_name']}")
            click.echo(f"Hash: {cache_info['file_hash'][:16]}...")
            click.echo(f"Records: {cache_info['record_count']:,}")
            click.echo(f"Extracted: {cache_info['extraction_time']}")
            click.echo(f"File size: {cache_info['file_size']:,} bytes")
        else:
            click.echo(f"File not in cache: {info}")
            if extraction_cache.is_cached(info):
                click.echo("(Hash computed, but no metadata found)")

    else:
        # Default: show stats
        cache_stats = extraction_cache.get_stats()
        click.echo("=== Cache Summary ===")
        click.echo(f"Cached files: {cache_stats['cached_files']}")
        click.echo(f"Cache size: {cache_stats['cache_size_mb']} MB")
        click.echo("\nUse --help for available options")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def load_data(file_path: str) -> List[Dict[str, Any]]:
    """Load data from CSV or JSON file."""
    path = Path(file_path)
    if path.suffix.lower() == '.json':
        return load_json(file_path)
    else:
        return load_csv(file_path)


def load_csv(file_path: str) -> List[Dict[str, Any]]:
    """Load records from CSV file."""
    records = []
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(dict(row))
    return records


def load_json(file_path: str) -> List[Dict[str, Any]]:
    """Load records from JSON file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return [data]


def write_csv(records: List[Dict[str, Any]], file_path: str):
    """Write records to CSV file."""
    if not records:
        return

    # Get all unique keys
    fieldnames = []
    for r in records:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    with open(file_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            # Convert lists/dicts to strings for CSV
            row = {}
            for k, v in r.items():
                if isinstance(v, (list, dict)):
                    row[k] = json.dumps(v)
                else:
                    row[k] = v
            writer.writerow(row)


def write_json(data: Any, file_path: str):
    """Write data to JSON file."""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)


def print_table(records: List[Dict[str, Any]], max_rows: int = 20):
    """Print records as a simple table."""
    if not records:
        click.echo("No records to display")
        return

    # Select columns to display
    display_cols = ['agency', 'appropriation_id', 'chapter_year',
                    'reappropriation_amount', 'program_category']

    # Use available columns
    available = list(records[0].keys())
    cols = [c for c in display_cols if c in available]
    if not cols:
        cols = available[:5]

    # Calculate column widths
    widths = {}
    for col in cols:
        max_width = len(col)
        for r in records[:max_rows]:
            val = str(r.get(col, ''))[:40]
            max_width = max(max_width, len(val))
        widths[col] = min(max_width, 40)

    # Print header
    header = ' | '.join(col.ljust(widths[col]) for col in cols)
    click.echo(header)
    click.echo('-' * len(header))

    # Print rows
    for r in records[:max_rows]:
        row = ' | '.join(str(r.get(col, ''))[:widths[col]].ljust(widths[col]) for col in cols)
        click.echo(row)

    if len(records) > max_rows:
        click.echo(f"... and {len(records) - max_rows} more records")


def print_stats(data: Dict[str, Any], indent: int = 0):
    """Pretty print statistics dictionary."""
    prefix = '  ' * indent
    for key, value in data.items():
        if isinstance(value, dict):
            click.echo(f"{prefix}{key}:")
            print_stats(value, indent + 1)
        elif isinstance(value, list):
            click.echo(f"{prefix}{key}: ({len(value)} items)")
            if value and isinstance(value[0], dict):
                for i, item in enumerate(value[:5]):
                    click.echo(f"{prefix}  [{i}] {item}")
                if len(value) > 5:
                    click.echo(f"{prefix}  ... and {len(value) - 5} more")
        elif isinstance(value, float):
            click.echo(f"{prefix}{key}: {value:,.2f}")
        elif isinstance(value, int) and value > 1000:
            click.echo(f"{prefix}{key}: {value:,}")
        else:
            click.echo(f"{prefix}{key}: {value}")


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main():
    """Main entry point for CLI."""
    cli(obj={})


if __name__ == '__main__':
    main()

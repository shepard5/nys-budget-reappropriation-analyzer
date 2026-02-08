#!/usr/bin/env python3
"""
Highlight Discontinued Items in PDF

Creates a copy of the enacted budget PDF with discontinued items highlighted.
Supports both reappropriations (yellow) and appropriations (orange).

Usage:
    python highlight_discontinued.py <enacted_pdf> <discontinued_csv> <output_pdf> [--type TYPE]

Examples:
    # Highlight reappropriations (default, yellow)
    python highlight_discontinued.py "ATL 25-26.pdf" "discontinued_reappropriations.csv" "highlighted.pdf"

    # Highlight appropriations (orange)
    python highlight_discontinued.py "ATL 25-26.pdf" "discontinued_appropriations.csv" "highlighted.pdf" --type appropriations

    # Highlight all discontinued items
    python highlight_discontinued.py "ATL 25-26.pdf" "discontinued_all.csv" "highlighted.pdf" --type all
"""

import fitz  # PyMuPDF
import pandas as pd
import re
import argparse
import sys
from pathlib import Path


def load_discontinued_items(csv_path: Path, item_type: str) -> pd.DataFrame:
    """Load discontinued items from CSV based on type filter."""
    df = pd.read_csv(csv_path)

    if item_type == 'reappropriations':
        # Filter to reappropriations only (reapprop_amount > 0)
        df = df[df['reappropriation_amount'] > 0].copy()
        print(f"Loaded {len(df)} discontinued reappropriations")
    elif item_type == 'appropriations':
        # Filter to appropriations only (reapprop_amount == 0)
        df = df[df['reappropriation_amount'] == 0].copy()
        print(f"Loaded {len(df)} discontinued appropriations")
    else:  # 'all'
        print(f"Loaded {len(df)} discontinued items (all types)")

    return df


def build_search_patterns(df: pd.DataFrame, item_type: str) -> dict:
    """Build search patterns grouped by page number."""
    patterns_by_page = {}

    for _, row in df.iterrows():
        page_num = int(row['page_number'])
        approp_id = str(row['appropriation_id'])
        approp_amt = int(row['appropriation_amount'])
        reapprop_amt = int(row['reappropriation_amount'])

        if page_num not in patterns_by_page:
            patterns_by_page[page_num] = []

        if reapprop_amt > 0:
            # This is a reappropriation - search for (re. $X,XXX)
            amt_formatted = f"{reapprop_amt:,}"
            patterns_by_page[page_num].append({
                'type': 'reappropriation',
                'approp_id': approp_id,
                'search_amt': amt_formatted,
                'search_texts': [
                    f"(re. ${amt_formatted})",
                    f"(re.${amt_formatted})",
                    f"re. ${amt_formatted}",
                ]
            })
        else:
            # This is an appropriation - search for (XXXXX) ... $amount
            amt_formatted = f"{approp_amt:,}"
            patterns_by_page[page_num].append({
                'type': 'appropriation',
                'approp_id': approp_id,
                'search_amt': amt_formatted,
                'search_texts': [
                    f"({approp_id})",  # Search for the ID
                    f"{amt_formatted}",  # Also try to find the amount
                ]
            })

    return patterns_by_page


def highlight_pdf(pdf_path: Path, patterns_by_page: dict, output_path: Path, item_type: str):
    """Open PDF and highlight matching patterns."""
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    highlights_made = 0
    pages_with_highlights = set()

    # Colors: Yellow for reappropriations, Orange for appropriations
    color_reapprop = (1, 1, 0)      # Yellow
    color_approp = (1, 0.7, 0.3)    # Orange

    print(f"Processing {total_pages} pages...")

    for page_num, patterns in patterns_by_page.items():
        if page_num > total_pages:
            print(f"  Warning: Page {page_num} exceeds document length ({total_pages})")
            continue

        page = doc[page_num - 1]  # 0-indexed

        for pattern_info in patterns:
            pattern_type = pattern_info['type']
            search_texts = pattern_info['search_texts']

            # Choose color based on type
            color = color_reapprop if pattern_type == 'reappropriation' else color_approp

            found = False
            for search_text in search_texts:
                text_instances = page.search_for(search_text)
                if text_instances:
                    for inst in text_instances:
                        # Create highlight annotation
                        highlight = page.add_highlight_annot(inst)
                        highlight.set_colors(stroke=color)
                        highlight.update()
                        highlights_made += 1
                        pages_with_highlights.add(page_num)
                        found = True
                    if pattern_type == 'reappropriation':
                        break  # For reapprops, stop after finding the (re. $) marker
                    # For appropriations, we highlight both ID and amount if found

        # Progress indicator
        if page_num % 100 == 0:
            print(f"  Processed through page {page_num}...")

    # Save the highlighted PDF
    print(f"\nSaving highlighted PDF to: {output_path}")
    doc.save(output_path)
    doc.close()

    print(f"\nSummary:")
    print(f"  Total highlights made: {highlights_made}")
    print(f"  Pages with highlights: {len(pages_with_highlights)}")

    return highlights_made


def main():
    parser = argparse.ArgumentParser(
        description='Highlight discontinued items in PDF',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Highlight reappropriations (yellow)
    python highlight_discontinued.py enacted.pdf disc_reapprops.csv output.pdf --type reappropriations

    # Highlight appropriations (orange)
    python highlight_discontinued.py enacted.pdf disc_approps.csv output.pdf --type appropriations

    # Highlight all (both colors)
    python highlight_discontinued.py enacted.pdf disc_all.csv output.pdf --type all
        """
    )

    parser.add_argument('enacted_pdf', type=Path, help='Path to enacted budget PDF')
    parser.add_argument('discontinued_csv', type=Path, help='Path to discontinued CSV file')
    parser.add_argument('output_pdf', type=Path, help='Path for output highlighted PDF')
    parser.add_argument('--type', '-t',
                        choices=['reappropriations', 'appropriations', 'all'],
                        default='reappropriations',
                        help='Type of items to highlight (default: reappropriations)')

    args = parser.parse_args()

    # Validate inputs
    if not args.enacted_pdf.exists():
        print(f"Error: PDF not found: {args.enacted_pdf}")
        sys.exit(1)
    if not args.discontinued_csv.exists():
        print(f"Error: CSV not found: {args.discontinued_csv}")
        sys.exit(1)

    print("=" * 60)
    print("HIGHLIGHT DISCONTINUED ITEMS")
    print("=" * 60)
    print(f"Input PDF:  {args.enacted_pdf}")
    print(f"Input CSV:  {args.discontinued_csv}")
    print(f"Output PDF: {args.output_pdf}")
    print(f"Item Type:  {args.type}")
    print(f"Colors:     Yellow = reappropriations, Orange = appropriations")
    print()

    # Load discontinued items
    df = load_discontinued_items(args.discontinued_csv, args.type)

    if len(df) == 0:
        print("No items to highlight after filtering.")
        sys.exit(0)

    # Build search patterns
    patterns = build_search_patterns(df, args.type)
    print(f"Built patterns for {len(patterns)} pages")
    print()

    # Highlight PDF
    highlights = highlight_pdf(args.enacted_pdf, patterns, args.output_pdf, args.type)

    if highlights > 0:
        print(f"\n✅ Done! Open {args.output_pdf} to see highlighted discontinued items.")
    else:
        print("\n⚠️  No highlights were made. Check if patterns match PDF content.")


if __name__ == "__main__":
    main()

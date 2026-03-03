"""
Reappropriation Pipeline — End-to-End
======================================
Uploads both budget PDFs, extracts reappropriations, identifies drops,
determines insertion positions, applies tracked-change insertions to the
executive bill, and generates a signed output PDF.

Usage:
  python pipeline.py                          # Uses default paths
  python pipeline.py <enacted.pdf> <exec.pdf> # Custom paths
  python pipeline.py --extract-only           # Just extract, no insertions
  python pipeline.py --from-html              # Use cached HTML files
"""

import sys
import json
import time
from pathlib import Path
from typing import List

from lbdc_editor import LBDCClient, LBDCDocument
from lbdc_extract import (
    Reappropriation, StructuralElement, ExtractionResult,
    extract_from_html, upload_and_extract, print_extraction_report, to_dataframe
)
from compare import compare_budgets, print_comparison_report, ComparisonResult
from place_inserts import (
    build_insert_instructions, print_placement_report, InsertInstruction
)


# ============================================================================
# DEFAULT PATHS
# ============================================================================

BASE_DIR = Path("C:/Users/samsc/Desktop/Reapprops")
DEFAULT_ENACTED = BASE_DIR / "2526.pdf"
DEFAULT_EXECUTIVE = BASE_DIR / "2627.pdf"
DEFAULT_OUTPUT = BASE_DIR / "2627_with_drops.pdf"


# ============================================================================
# APPLY INSERTIONS TO HTML
# ============================================================================

def apply_insertions(
    doc: LBDCDocument,
    instructions: List[InsertInstruction],
) -> int:
    """
    Apply insertion instructions to the executive LBDCDocument.

    Instructions are pre-sorted by target position descending (bottom-up)
    so that inserting lines doesn't shift the positions of subsequent
    insertions.

    Each insertion adds new tracked-change lines (<p class="new-line"><ins>).

    Returns count of lines inserted.
    """
    total_lines = 0

    for ins in instructions:
        page_idx = ins.target_page_idx
        insert_after = ins.target_p_after

        # Collect all lines to insert (fund header + chapter header + bill lines)
        all_lines = []

        if ins.needs_fund_header:
            # Add blank separator + fund header lines
            all_lines.append("")  # Blank separator
            all_lines.extend(ins.fund_header_lines)
            all_lines.append("")  # Blank separator after fund

        if ins.needs_chapter_header:
            all_lines.extend(ins.chapter_header_lines)

        # Add the bill language lines
        all_lines.extend(ins.lines_to_insert)

        # Insert lines bottom-up within this group (so indices don't shift)
        for line_text in reversed(all_lines):
            if line_text:
                doc.insert_line(insert_after, line_text, page_idx)
                total_lines += 1
            else:
                # Insert a blank separator line
                # We'll insert a minimal line that becomes a spacer
                doc.insert_line(insert_after, " ", page_idx)
                total_lines += 1

    return total_lines


# ============================================================================
# EXPORT REPORTS
# ============================================================================

def export_reports(
    enacted: List[Reappropriation],
    executive: List[Reappropriation],
    comparison: ComparisonResult,
    instructions: List[InsertInstruction],
    output_dir: Path,
):
    """Export CSVs and summary files."""
    try:
        import pandas as pd
    except ImportError:
        print("pandas not available — skipping CSV export")
        return

    # Dropped reapprops
    dropped_reapprops = [enacted[i] for i in comparison.dropped]
    dropped_df = to_dataframe(dropped_reapprops)
    dropped_df.to_csv(output_dir / "dropped_reapprops.csv", index=False)
    print(f"  Saved dropped_reapprops.csv ({len(dropped_df)} rows)")

    # Continued/modified
    continued_records = []
    for ei, xi in comparison.continued:
        rec = {
            'status': 'continued',
            'program': enacted[ei].program,
            'fund': enacted[ei].fund,
            'chapter_year': enacted[ei].chapter_year,
            'approp_id': enacted[ei].approp_id,
            'enacted_reapprop_amount': enacted[ei].reapprop_amount,
            'exec_reapprop_amount': executive[xi].reapprop_amount,
        }
        continued_records.append(rec)
    for ei, xi in comparison.modified:
        rec = {
            'status': 'modified',
            'program': enacted[ei].program,
            'fund': enacted[ei].fund,
            'chapter_year': enacted[ei].chapter_year,
            'approp_id': enacted[ei].approp_id,
            'enacted_reapprop_amount': enacted[ei].reapprop_amount,
            'exec_reapprop_amount': executive[xi].reapprop_amount,
        }
        continued_records.append(rec)

    if continued_records:
        cont_df = pd.DataFrame(continued_records)
        cont_df.to_csv(output_dir / "continued_modified.csv", index=False)
        print(f"  Saved continued_modified.csv ({len(cont_df)} rows)")

    # New in exec
    new_reapprops = [executive[i] for i in comparison.new_in_exec]
    if new_reapprops:
        new_df = to_dataframe(new_reapprops)
        new_df.to_csv(output_dir / "new_in_executive.csv", index=False)
        print(f"  Saved new_in_executive.csv ({len(new_df)} rows)")

    # Insertion placement report
    placement_records = []
    for ins in instructions:
        d = ins.dropped
        placement_records.append({
            'program': d.program,
            'fund': d.fund,
            'chapter_year': d.chapter_year,
            'approp_id': d.approp_id,
            'reapprop_amount': d.reapprop_amount,
            'anchor_type': ins.anchor_type,
            'target_page': ins.target_page_idx,
            'target_p_after': ins.target_p_after,
            'needs_chapter_header': ins.needs_chapter_header,
            'needs_fund_header': ins.needs_fund_header,
            'bill_language_preview': d.bill_language[:100],
        })
    if placement_records:
        place_df = pd.DataFrame(placement_records)
        place_df.to_csv(output_dir / "insertion_placements.csv", index=False)
        print(f"  Saved insertion_placements.csv ({len(place_df)} rows)")

    # Summary JSON
    summary = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'enacted_count': len(enacted),
        'executive_count': len(executive),
        'continued': len(comparison.continued),
        'modified': len(comparison.modified),
        'dropped': len(comparison.dropped),
        'new_in_exec': len(comparison.new_in_exec),
        'insertions_built': len(instructions),
        'total_dropped_amount': sum(enacted[i].reapprop_amount for i in comparison.dropped),
    }
    (output_dir / "pipeline_summary.json").write_text(
        json.dumps(summary, indent=2), encoding='utf-8'
    )
    print(f"  Saved pipeline_summary.json")


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def run_pipeline(
    enacted_pdf: str = None,
    exec_pdf: str = None,
    output_pdf: str = None,
    extract_only: bool = False,
    from_html: bool = False,
):
    """
    End-to-end pipeline:
      1. Upload both PDFs → get HTML
      2. Parse HTML → extract reappropriations
      3. Compare → find drops
      4. Build insertion instructions
      5. Apply insertions to executive HTML
      6. Generate signed output PDF
      7. Export reports
    """
    enacted_pdf = enacted_pdf or str(DEFAULT_ENACTED)
    exec_pdf = exec_pdf or str(DEFAULT_EXECUTIVE)
    output_pdf = output_pdf or str(DEFAULT_OUTPUT)
    output_dir = Path(enacted_pdf).parent

    print("=" * 80)
    print("NYS Budget Reappropriation Pipeline (LBDC HTML)")
    print("=" * 80)
    print(f"  Enacted:   {enacted_pdf}")
    print(f"  Executive: {exec_pdf}")
    print(f"  Output:    {output_pdf}")

    # ── Step 1: Get HTML ──
    client = LBDCClient()

    if from_html:
        # Load cached HTML
        print("\n>>> Loading cached HTML...")
        enacted_html = (output_dir / "enacted.html").read_text(encoding='utf-8')
        exec_html = (output_dir / "executive.html").read_text(encoding='utf-8')
        print(f"    Enacted HTML: {len(enacted_html)} chars")
        print(f"    Executive HTML: {len(exec_html)} chars")
    else:
        print("\n>>> Uploading PDFs to LBDC API...")
        enacted_html = client.upload_pdf(enacted_pdf)
        exec_html = client.upload_pdf(exec_pdf)

        # Cache HTML for re-runs
        (output_dir / "enacted.html").write_text(enacted_html, encoding='utf-8')
        (output_dir / "executive.html").write_text(exec_html, encoding='utf-8')
        print("    Cached HTML to enacted.html / executive.html")

    # ── Step 2: Extract ──
    print("\n>>> Extracting reappropriations...")
    enacted_result = extract_from_html(enacted_html, Path(enacted_pdf).name)
    exec_result = extract_from_html(exec_html, Path(exec_pdf).name)

    enacted_reapprops = enacted_result.reapprops
    exec_reapprops = exec_result.reapprops

    print(f"\n    Enacted:   {len(enacted_reapprops)} reappropriations (expect 592)")
    print(f"    Executive: {len(exec_reapprops)} reappropriations (expect 362)")

    print_extraction_report(enacted_result)
    print_extraction_report(exec_result)

    # Save extraction CSVs
    try:
        import pandas as pd
        enacted_df = to_dataframe(enacted_reapprops)
        exec_df = to_dataframe(exec_reapprops)
        enacted_df.to_csv(output_dir / "enacted_reapprops.csv", index=False)
        exec_df.to_csv(output_dir / "executive_reapprops.csv", index=False)
        print(f"\n    Saved enacted_reapprops.csv ({len(enacted_df)} rows)")
        print(f"    Saved executive_reapprops.csv ({len(exec_df)} rows)")
    except ImportError:
        pass

    if extract_only:
        print("\n>>> Extract-only mode — stopping here.")
        return

    # ── Step 3: Compare ──
    print("\n>>> Comparing budgets...")
    comparison = compare_budgets(enacted_reapprops, exec_reapprops)
    print_comparison_report(comparison, enacted_reapprops, exec_reapprops)

    if not comparison.dropped:
        print("\n>>> No drops found — nothing to insert. Done!")
        return

    # ── Step 4: Build insertion instructions ──
    print("\n>>> Building insertion instructions...")
    instructions = build_insert_instructions(
        enacted_reapprops,
        exec_reapprops,
        enacted_result.structures,
        exec_result.structures,
        comparison,
    )
    print_placement_report(instructions, enacted_reapprops, exec_reapprops)

    # ── Step 5: Apply insertions ──
    print("\n>>> Applying insertions to executive HTML...")
    exec_doc = LBDCDocument(exec_html, user_color="blue")
    lines_inserted = apply_insertions(exec_doc, instructions)
    print(f"    Inserted {lines_inserted} lines across {len(instructions)} reappropriations")

    # ── Step 6: Generate output PDF ──
    print("\n>>> Generating signed output PDF...")
    edited_html = exec_doc.to_html()
    pdf_bytes = client.generate_pdf(edited_html)
    Path(output_pdf).write_bytes(pdf_bytes)
    print(f"    Saved: {output_pdf} ({len(pdf_bytes):,} bytes)")

    # Also save the edited HTML for inspection
    (output_dir / "executive_edited.html").write_text(edited_html, encoding='utf-8')
    print(f"    Saved: executive_edited.html")

    # ── Step 7: Export reports ──
    print("\n>>> Exporting reports...")
    export_reports(
        enacted_reapprops, exec_reapprops,
        comparison, instructions, output_dir
    )

    # ── Summary ──
    total_dropped_amt = sum(enacted_reapprops[i].reapprop_amount for i in comparison.dropped)
    print(f"\n{'='*80}")
    print("PIPELINE COMPLETE")
    print(f"{'='*80}")
    print(f"  Enacted reapprops:    {len(enacted_reapprops)}")
    print(f"  Executive reapprops:  {len(exec_reapprops)}")
    print(f"  Continued:            {len(comparison.continued)}")
    print(f"  Modified:             {len(comparison.modified)}")
    print(f"  Dropped:              {len(comparison.dropped)}")
    print(f"  New in executive:     {len(comparison.new_in_exec)}")
    print(f"  Total dropped amount: ${total_dropped_amt:,.0f}")
    print(f"  Lines inserted:       {lines_inserted}")
    print(f"  Output PDF:           {output_pdf}")


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    args = sys.argv[1:]

    extract_only = "--extract-only" in args
    from_html = "--from-html" in args

    # Remove flags
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) >= 2:
        run_pipeline(positional[0], positional[1],
                     positional[2] if len(positional) >= 3 else None,
                     extract_only=extract_only, from_html=from_html)
    elif len(positional) == 1:
        print("Need both enacted and executive PDFs, or use no args for defaults.")
    else:
        run_pipeline(extract_only=extract_only, from_html=from_html)

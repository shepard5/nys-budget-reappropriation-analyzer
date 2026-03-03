"""
Neighbor-Based Insertion Placement
===================================
For each dropped reappropriation, determine exactly where in the
executive HTML it should be reinserted.

Core insight (from Sam): "the reapprop is supposed to go where it
was before it was dropped."

Algorithm:
  1. For each drop, find its predecessor/successor in enacted (same fund)
  2. If predecessor was matched to exec → insert after predecessor's exec position
  3. If successor was matched to exec → insert before successor's exec position
  4. If entire chapter_year section is missing → insert structurally

This replaces the 2300-line over-engineered build_inserts.py.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict

from lbdc_extract import Reappropriation, StructuralElement, ExtractionResult
from compare import ComparisonResult


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class InsertInstruction:
    """Instructions for inserting one dropped reappropriation into the executive bill."""
    # What to insert
    dropped_idx: int                    # Index into enacted reapprops list
    dropped: Reappropriation            # The dropped reappropriation

    # Where to insert in executive HTML
    target_page_idx: int                # Page in exec to insert on
    target_p_after: int                 # Insert after this <p> index within page
    target_global_p_after: int          # Global <p> index to insert after

    # Lines to insert (bill language, split by original line breaks)
    lines_to_insert: List[str]

    # Structural context
    needs_chapter_header: bool = False  # True if chapter year section doesn't exist in exec
    chapter_header_lines: List[str] = field(default_factory=list)

    needs_fund_header: bool = False
    fund_header_lines: List[str] = field(default_factory=list)

    # Anchor info for debugging/reporting
    anchor_type: str = ""               # 'predecessor' | 'successor' | 'structural' | 'end_of_fund'
    anchor_enacted_idx: int = -1        # Which enacted reapprop was used as anchor
    anchor_exec_idx: int = -1           # Its exec counterpart (if matched)


# ============================================================================
# STRUCTURAL LOOKUPS
# ============================================================================

def _build_fund_groups(reapprops: List[Reappropriation]) -> Dict[str, List[int]]:
    """Group reapprop indices by (program, fund) key, in document order."""
    groups = defaultdict(list)
    for i, r in enumerate(reapprops):
        key = (r.program, r.fund)
        groups[key].append(i)
    return groups


def _build_chapter_year_set(structures: List[StructuralElement]) -> Set[Tuple[str, str, int]]:
    """Build set of (program, fund, chapter_year) from structural elements."""
    return {
        (s.program, s.fund, s.chapter_year)
        for s in structures
        if s.elem_type == 'chapter_year'
    }


def _build_fund_set(structures: List[StructuralElement]) -> Set[Tuple[str, str]]:
    """Build set of (program, fund) from structural elements."""
    return {
        (s.program, s.fund)
        for s in structures
        if s.elem_type == 'fund'
    }


def _find_fund_last_p(
    exec_reapprops: List[Reappropriation],
    exec_structures: List[StructuralElement],
    program: str,
    fund: str,
) -> Tuple[int, int, int]:
    """Find the last <p> position within a fund section in executive.
    Returns (page_idx, p_idx, global_p_idx) or (-1, -1, -1) if not found."""

    # Find reapprops in this fund — the last one's p_end is our target
    fund_reapprops = [
        r for r in exec_reapprops
        if r.program == program and r.fund == fund
    ]
    if fund_reapprops:
        last = max(fund_reapprops, key=lambda r: r.global_p_end)
        return last.page_idx, last.p_end, last.global_p_end

    # No reapprops but fund structure exists — find the fund header
    for s in exec_structures:
        if s.elem_type == 'fund' and s.program == program and s.fund == fund:
            return s.page_idx, s.p_idx, s.global_p_idx

    return -1, -1, -1


def _find_chapter_year_position(
    exec_structures: List[StructuralElement],
    program: str,
    fund: str,
    chapter_year: int,
    amending_year: int,
) -> Tuple[int, int, int]:
    """Find where a chapter year section should be inserted within a fund.

    Ordering rules (from Sam's notes):
      - 2025 first (most recent base year first)
      - Then descending by chapter year (2024, 2023, 2022...)
      - Non-amended (amending_year=0) before amended within same chapter year

    Returns (page_idx, p_after, global_p_after) — insert after this position.
    Returns (-1, -1, -1) if fund not found.
    """
    # Get all chapter year structures in this fund
    fund_chyrs = [
        s for s in exec_structures
        if s.elem_type == 'chapter_year' and s.program == program and s.fund == fund
    ]

    if not fund_chyrs:
        # No chapter years in this fund at all — insert after fund header
        for s in exec_structures:
            if s.elem_type == 'fund' and s.program == program and s.fund == fund:
                return s.page_idx, s.p_idx, s.global_p_idx
        return -1, -1, -1

    # Sort existing by the ordering rules
    def sort_key(s):
        # 2025 first (highest year first), then by chapter year desc,
        # then non-amended (0) before amended
        return (-s.chapter_year, s.amending_year)

    fund_chyrs.sort(key=sort_key)

    # Where does our new chapter year fit?
    new_sort = (-chapter_year, amending_year)

    # Find the first existing entry that should come AFTER our new one
    for i, s in enumerate(fund_chyrs):
        existing_sort = (-s.chapter_year, s.amending_year)
        if existing_sort > new_sort:
            # Our new entry goes before this one
            # Insert after the PREVIOUS entry's last content, or after fund header
            if i == 0:
                # Goes before everything — insert after fund header
                for fs in exec_structures:
                    if fs.elem_type == 'fund' and fs.program == program and fs.fund == fund:
                        return fs.page_idx, fs.p_idx, fs.global_p_idx
            else:
                prev = fund_chyrs[i - 1]
                # Insert after the last reapprop in the previous chapter year section
                # or after the previous chapter year header itself
                return prev.page_idx, prev.p_idx, prev.global_p_idx

    # New entry goes at the end — insert after the last chapter year section
    last = fund_chyrs[-1]
    return last.page_idx, last.p_idx, last.global_p_idx


# ============================================================================
# MAIN PLACEMENT ENGINE
# ============================================================================

def build_insert_instructions(
    enacted_reapprops: List[Reappropriation],
    exec_reapprops: List[Reappropriation],
    enacted_structures: List[StructuralElement],
    exec_structures: List[StructuralElement],
    comparison: ComparisonResult,
) -> List[InsertInstruction]:
    """
    For each dropped reappropriation, build an InsertInstruction that says
    exactly where in the executive HTML it should be inserted.

    Strategy:
      1. Group enacted reapprops by (program, fund) to find neighbors
      2. For each drop, look at predecessor/successor in same fund
      3. If pred/succ is matched → use its exec position as anchor
      4. If chapter year section missing → place structurally
    """
    instructions = []

    # Build fund groups for enacted
    enacted_fund_groups = _build_fund_groups(enacted_reapprops)

    # Build structural lookups for executive
    exec_chyr_set = _build_chapter_year_set(exec_structures)
    exec_fund_set = _build_fund_set(exec_structures)

    # Reverse map: within each fund group, get the position of each reapprop
    enacted_position_in_fund = {}
    for fund_key, indices in enacted_fund_groups.items():
        for pos, idx in enumerate(indices):
            enacted_position_in_fund[idx] = (fund_key, pos)

    # Process each dropped reapprop
    dropped_set = set(comparison.dropped)

    for drop_idx in comparison.dropped:
        drop = enacted_reapprops[drop_idx]
        fund_key = (drop.program, drop.fund)
        fund_group = enacted_fund_groups.get(fund_key, [])

        # Find position of this drop within its fund group
        fund_pos = None
        for pos, idx in enumerate(fund_group):
            if idx == drop_idx:
                fund_pos = pos
                break

        if fund_pos is None:
            # Shouldn't happen, but safety
            instructions.append(_make_fallback_instruction(
                drop_idx, drop, exec_reapprops, exec_structures
            ))
            continue

        # ── Try predecessor (look backward in same fund for a matched reapprop) ──
        pred_exec_idx = None
        pred_enacted_idx = None
        for p in range(fund_pos - 1, -1, -1):
            enacted_idx = fund_group[p]
            if enacted_idx in comparison.match_map:
                pred_enacted_idx = enacted_idx
                pred_exec_idx = comparison.match_map[enacted_idx]
                break

        # ── Try successor (look forward in same fund for a matched reapprop) ──
        succ_exec_idx = None
        succ_enacted_idx = None
        for s in range(fund_pos + 1, len(fund_group)):
            enacted_idx = fund_group[s]
            if enacted_idx in comparison.match_map:
                succ_enacted_idx = enacted_idx
                succ_exec_idx = comparison.match_map[enacted_idx]
                break

        # Prepare the lines to insert
        bill_lines = drop.bill_language.split('\n')

        # Check if chapter year section exists in exec
        chyr_key = (drop.program, drop.fund, drop.chapter_year)
        needs_chapter_header = chyr_key not in exec_chyr_set

        chapter_header_lines = []
        if needs_chapter_header and drop.chapter_citation:
            # The chapter citation may be multi-line — split by logical lines
            # But we stored it as a single joined string, so just use it as one line
            chapter_header_lines = [drop.chapter_citation]

        # Check if fund section exists in exec
        fund_key_check = (drop.program, drop.fund)
        needs_fund_header = fund_key_check not in exec_fund_set

        fund_header_lines = []
        if needs_fund_header:
            # Split the fund string back into header lines
            fund_header_lines = [part.strip() for part in drop.fund.split(';') if part.strip()]

        # ── Determine insertion point ──

        if pred_exec_idx is not None:
            # Insert after predecessor's exec position
            pred_exec = exec_reapprops[pred_exec_idx]
            instruction = InsertInstruction(
                dropped_idx=drop_idx,
                dropped=drop,
                target_page_idx=pred_exec.page_idx,
                target_p_after=pred_exec.p_end,
                target_global_p_after=pred_exec.global_p_end,
                lines_to_insert=bill_lines,
                needs_chapter_header=needs_chapter_header,
                chapter_header_lines=chapter_header_lines,
                needs_fund_header=needs_fund_header,
                fund_header_lines=fund_header_lines,
                anchor_type='predecessor',
                anchor_enacted_idx=pred_enacted_idx,
                anchor_exec_idx=pred_exec_idx,
            )

        elif succ_exec_idx is not None:
            # Insert before successor's exec position (= after the line before it)
            succ_exec = exec_reapprops[succ_exec_idx]
            target_p = max(0, succ_exec.p_start - 1)
            target_global = max(0, succ_exec.global_p_start - 1)
            instruction = InsertInstruction(
                dropped_idx=drop_idx,
                dropped=drop,
                target_page_idx=succ_exec.page_idx,
                target_p_after=target_p,
                target_global_p_after=target_global,
                lines_to_insert=bill_lines,
                needs_chapter_header=needs_chapter_header,
                chapter_header_lines=chapter_header_lines,
                needs_fund_header=needs_fund_header,
                fund_header_lines=fund_header_lines,
                anchor_type='successor',
                anchor_enacted_idx=succ_enacted_idx,
                anchor_exec_idx=succ_exec_idx,
            )

        elif not needs_fund_header:
            # Fund exists but no matched neighbors — place at end of fund section
            page_idx, p_idx, global_p = _find_fund_last_p(
                exec_reapprops, exec_structures, drop.program, drop.fund
            )
            if page_idx >= 0:
                instruction = InsertInstruction(
                    dropped_idx=drop_idx,
                    dropped=drop,
                    target_page_idx=page_idx,
                    target_p_after=p_idx,
                    target_global_p_after=global_p,
                    lines_to_insert=bill_lines,
                    needs_chapter_header=needs_chapter_header,
                    chapter_header_lines=chapter_header_lines,
                    needs_fund_header=False,
                    fund_header_lines=[],
                    anchor_type='end_of_fund',
                )
            else:
                instruction = _make_fallback_instruction(
                    drop_idx, drop, exec_reapprops, exec_structures
                )

        else:
            # Whole fund section missing — structural placement
            instruction = _make_fallback_instruction(
                drop_idx, drop, exec_reapprops, exec_structures
            )

        instructions.append(instruction)

    # Sort instructions by target position (process bottom-up to avoid index shifts)
    instructions.sort(key=lambda ins: ins.target_global_p_after, reverse=True)

    return instructions


def _make_fallback_instruction(
    drop_idx: int,
    drop: Reappropriation,
    exec_reapprops: List[Reappropriation],
    exec_structures: List[StructuralElement],
) -> InsertInstruction:
    """Fallback: place at end of program section or end of document."""
    # Find last reapprop in same program
    same_prog = [r for r in exec_reapprops if r.program == drop.program]
    if same_prog:
        last = max(same_prog, key=lambda r: r.global_p_end)
        return InsertInstruction(
            dropped_idx=drop_idx,
            dropped=drop,
            target_page_idx=last.page_idx,
            target_p_after=last.p_end,
            target_global_p_after=last.global_p_end,
            lines_to_insert=drop.bill_language.split('\n'),
            needs_chapter_header=True,
            chapter_header_lines=[drop.chapter_citation] if drop.chapter_citation else [],
            needs_fund_header=True,
            fund_header_lines=[p.strip() for p in drop.fund.split(';') if p.strip()],
            anchor_type='structural',
        )

    # Last resort: end of document
    if exec_reapprops:
        last = max(exec_reapprops, key=lambda r: r.global_p_end)
        return InsertInstruction(
            dropped_idx=drop_idx,
            dropped=drop,
            target_page_idx=last.page_idx,
            target_p_after=last.p_end,
            target_global_p_after=last.global_p_end,
            lines_to_insert=drop.bill_language.split('\n'),
            needs_chapter_header=True,
            chapter_header_lines=[drop.chapter_citation] if drop.chapter_citation else [],
            needs_fund_header=True,
            fund_header_lines=[p.strip() for p in drop.fund.split(';') if p.strip()],
            anchor_type='structural',
        )

    # Empty document? Shouldn't happen.
    return InsertInstruction(
        dropped_idx=drop_idx,
        dropped=drop,
        target_page_idx=0,
        target_p_after=0,
        target_global_p_after=0,
        lines_to_insert=drop.bill_language.split('\n'),
        needs_chapter_header=True,
        chapter_header_lines=[drop.chapter_citation] if drop.chapter_citation else [],
        needs_fund_header=True,
        fund_header_lines=[p.strip() for p in drop.fund.split(';') if p.strip()],
        anchor_type='structural',
    )


# ============================================================================
# REPORTING
# ============================================================================

def print_placement_report(
    instructions: List[InsertInstruction],
    enacted: List[Reappropriation],
    executive: List[Reappropriation],
):
    """Print summary of insertion placements."""
    print(f"\n{'='*80}")
    print("INSERTION PLACEMENT REPORT")
    print(f"{'='*80}")
    print(f"  Total insertions: {len(instructions)}")

    from collections import Counter
    anchor_counts = Counter(ins.anchor_type for ins in instructions)
    print(f"\n  Anchor types:")
    for atype, count in anchor_counts.most_common():
        print(f"    {atype}: {count}")

    needs_chyr = sum(1 for ins in instructions if ins.needs_chapter_header)
    needs_fund = sum(1 for ins in instructions if ins.needs_fund_header)
    print(f"\n  Need chapter year header: {needs_chyr}")
    print(f"  Need fund header: {needs_fund}")

    # Show first 10
    print(f"\n  First 10 insertions (sorted by target position desc):")
    for ins in instructions[:10]:
        d = ins.dropped
        print(f"    [{ins.anchor_type:12s}] ChYr {d.chapter_year} | "
              f"ID {d.approp_id or 'N/A':>5s} | ${d.reapprop_amount:>12,.0f} | "
              f"→ page {ins.target_page_idx} p{ins.target_p_after}")
    if len(instructions) > 10:
        print(f"    ... and {len(instructions) - 10} more")

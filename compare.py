"""
Budget Comparison Engine
========================
Compares enacted vs executive reappropriations to find:
  - Continued (same amount in both)
  - Modified (amount changed)
  - Dropped (in enacted, not in executive)
  - New in executive (typically new chapter year 2025)

Also produces a match_map (enacted_idx → exec_idx) which is critical
for the neighbor-based insertion placement.

4-pass matching strategy:
  Pass 1: Exact match on (program, fund, chapter_year, approp_id)
  Pass 2: Fund-flexible match (program, chapter_year, approp_id)
  Pass 3: Amount match for items w/o approp_id
  Pass 4: Text similarity fallback (Jaccard > 0.6)
"""

import re
from dataclasses import dataclass
from typing import List, Tuple, Dict, Set
from collections import Counter

from lbdc_extract import Reappropriation


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class ComparisonResult:
    """Result of comparing enacted vs executive."""
    continued: List[Tuple[int, int]]    # (enacted_idx, exec_idx) — same amount
    modified: List[Tuple[int, int]]     # (enacted_idx, exec_idx) — amount changed
    dropped: List[int]                   # enacted indices not matched
    new_in_exec: List[int]               # exec indices not matched
    match_map: Dict[int, int]            # enacted_idx → exec_idx (all matches)


# ============================================================================
# TEXT SIMILARITY
# ============================================================================

def _normalize_fund(fund: str) -> str:
    """Normalize fund string for matching."""
    s = str(fund).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*;\s*', '; ', s)
    return s


def _normalize_text(text: str) -> str:
    """Normalize bill language for similarity comparison."""
    text = text.lower()
    text = re.sub(r'^\d{1,2}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\$?\d{1,3}(?:,\d{3})*', '', text)
    text = re.sub(r'\.{2,}', '', text)
    text = re.sub(r'\(re\.\s*\$[^)]*\)', '', text)
    text = re.sub(r'\(\d{5}\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def text_similarity(text1: str, text2: str) -> float:
    """Jaccard similarity on normalized word tokens."""
    t1 = _normalize_text(text1)
    t2 = _normalize_text(text2)
    words1 = set(w for w in t1.split() if len(w) >= 3)
    words2 = set(w for w in t2.split() if len(w) >= 3)
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / len(words1 | words2)


# ============================================================================
# COMPARISON ENGINE
# ============================================================================

def compare_budgets(
    enacted: List[Reappropriation],
    executive: List[Reappropriation],
) -> ComparisonResult:
    """
    Compare enacted vs executive reappropriations.

    Returns ComparisonResult with indices into the original lists,
    plus a match_map for neighbor-based insertion placement.
    """
    continued = []
    modified = []

    enacted_matched: Set[int] = set()
    exec_matched: Set[int] = set()
    match_map: Dict[int, int] = {}

    def record_match(ei: int, xi: int):
        enacted_matched.add(ei)
        exec_matched.add(xi)
        match_map[ei] = xi
        if enacted[ei].reapprop_amount == executive[xi].reapprop_amount:
            continued.append((ei, xi))
        else:
            modified.append((ei, xi))

    # ── Pass 1: Exact match (program, fund, chapter_year, approp_id) ──
    exec_by_key1 = {}
    for i, r in enumerate(executive):
        key = (r.program, _normalize_fund(r.fund), r.chapter_year, r.approp_id)
        exec_by_key1.setdefault(key, []).append(i)

    for ei, er in enumerate(enacted):
        if ei in enacted_matched or not er.approp_id:
            continue
        key = (er.program, _normalize_fund(er.fund), er.chapter_year, er.approp_id)
        for xi in exec_by_key1.get(key, []):
            if xi not in exec_matched:
                record_match(ei, xi)
                break

    # ── Pass 2: Fund-flexible match (program, chapter_year, approp_id) ──
    exec_by_key2 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, r.chapter_year, r.approp_id)
        exec_by_key2.setdefault(key, []).append(i)

    for ei, er in enumerate(enacted):
        if ei in enacted_matched or not er.approp_id:
            continue
        key = (er.program, er.chapter_year, er.approp_id)
        for xi in exec_by_key2.get(key, []):
            if xi not in exec_matched:
                record_match(ei, xi)
                break

    # ── Pass 3: Amount match for items WITHOUT approp_id ──
    exec_by_key3 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, _normalize_fund(r.fund), r.chapter_year, r.approp_amount)
        exec_by_key3.setdefault(key, []).append(i)

    for ei, er in enumerate(enacted):
        if ei in enacted_matched or er.approp_id:
            continue  # This pass only for items without ID
        key = (er.program, _normalize_fund(er.fund), er.chapter_year, er.approp_amount)
        best_xi = None
        best_sim = 0
        for xi in exec_by_key3.get(key, []):
            if xi not in exec_matched:
                sim = text_similarity(er.bill_language, executive[xi].bill_language)
                if sim > best_sim:
                    best_sim = sim
                    best_xi = xi
        if best_xi is not None and best_sim > 0.3:
            record_match(ei, best_xi)

    # ── Pass 4: Text similarity fallback ──
    exec_by_key4 = {}
    for i, r in enumerate(executive):
        if i in exec_matched:
            continue
        key = (r.program, r.chapter_year)
        exec_by_key4.setdefault(key, []).append(i)

    for ei, er in enumerate(enacted):
        if ei in enacted_matched:
            continue
        key = (er.program, er.chapter_year)
        best_xi = None
        best_sim = 0
        for xi in exec_by_key4.get(key, []):
            if xi not in exec_matched:
                sim = text_similarity(er.bill_language, executive[xi].bill_language)
                if sim > best_sim:
                    best_sim = sim
                    best_xi = xi
        if best_xi is not None and best_sim > 0.6:
            record_match(ei, best_xi)

    # ── Collect results ──
    dropped = [i for i in range(len(enacted)) if i not in enacted_matched]
    new_in_exec = [i for i in range(len(executive)) if i not in exec_matched]

    return ComparisonResult(
        continued=continued,
        modified=modified,
        dropped=dropped,
        new_in_exec=new_in_exec,
        match_map=match_map,
    )


# ============================================================================
# REPORTING
# ============================================================================

def print_comparison_report(
    result: ComparisonResult,
    enacted: List[Reappropriation],
    executive: List[Reappropriation],
):
    """Print detailed comparison report."""
    print(f"\n{'='*80}")
    print("COMPARISON: Enacted vs Executive")
    print(f"{'='*80}")

    print(f"\n  Continued (same amount):     {len(result.continued):>5}")
    print(f"  Modified (amount changed):   {len(result.modified):>5}")
    print(f"  DROPPED (not in executive):  {len(result.dropped):>5}")
    print(f"  New in executive:            {len(result.new_in_exec):>5}")

    total = len(result.continued) + len(result.modified) + len(result.dropped)
    print(f"\n  Total enacted accounted for: {total} / {len(enacted)}")

    # Dropped by program
    dropped_reapprops = [enacted[i] for i in result.dropped]
    print(f"\n  Dropped by Program:")
    prog_counts = Counter(r.program for r in dropped_reapprops)
    for prog, count in prog_counts.most_common():
        print(f"    {prog[:60]}: {count}")

    # Dropped by fund
    print(f"\n  Dropped by Fund:")
    fund_counts = Counter(r.fund for r in dropped_reapprops)
    for fund, count in fund_counts.most_common():
        print(f"    {fund[:60]}: {count}")

    # Dropped total dollars
    total_dropped = sum(r.reapprop_amount for r in dropped_reapprops)
    print(f"\n  Total dropped reapprop amount: ${total_dropped:,.0f}")

    # New in exec
    if result.new_in_exec:
        new_reapprops = [executive[i] for i in result.new_in_exec]
        print(f"\n  New in Executive (top 10):")
        for r in new_reapprops[:10]:
            print(f"    ChYr {r.chapter_year} | ID {r.approp_id} | "
                  f"${r.reapprop_amount:,.0f} | {r.bill_language[:60]}")
        if len(new_reapprops) > 10:
            print(f"    ... and {len(new_reapprops) - 10} more")

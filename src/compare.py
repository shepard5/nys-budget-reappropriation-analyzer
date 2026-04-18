"""
Compare enacted 25-26 reapprops vs executive 26-27 reapprops.

Matching key:
  (program, fund, approp_id, chapter_year, amending_year, approp_amount)

approp_amount = original enacted appropriation — stable across bills for the
same item. reapprop_amount (the SFS undisbursed balance) shrinks over time as
funds are drawn, so it's the classification signal, not the matching key.

Classification:
  continued  — match + reapprop_amount equal
  modified   — match + reapprop_amount differs
  dropped    — enacted has no counterpart in exec
  new_in_exec — exec has no counterpart in enacted

NaN approp_id items are matched as a second pass using (program, fund,
chapter_year, amending_year, approp_amount, reapprop_amount) + bill_language
prefix similarity. These are rare and flagged for manual review.

Output: outputs/comparison.csv — one row per enacted reapprop with status +
matched exec row info, plus additional rows for new_in_exec.
"""

import sys
from pathlib import Path
from difflib import SequenceMatcher
from typing import List, Dict, Tuple, Optional

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent


def key_full(row) -> Tuple:
    """Primary match key — excludes reapprop_amount so modified items still match."""
    return (
        row.program,
        row.fund,
        "" if pd.isna(row.approp_id) else str(int(float(row.approp_id))),
        int(row.chapter_year),
        int(row.amending_year),
        int(row.approp_amount),
    )


def key_no_id(row) -> Tuple:
    """Fallback for NaN-approp_id items — no approp_id, add reapprop_amount for
    tighter matching (since bill_language might not match identically)."""
    return (
        row.program,
        row.fund,
        int(row.chapter_year),
        int(row.amending_year),
        int(row.approp_amount),
        int(row.reapprop_amount),
    )


def text_sim(a: str, b: str, head: int = 150) -> float:
    a = (a or "")[:head]
    b = (b or "")[:head]
    return SequenceMatcher(None, a, b).ratio()


def compare(enacted: pd.DataFrame, executive: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame of comparison rows."""
    # Build lookup for exec by key
    exec_by_full_key: Dict[Tuple, List[int]] = {}
    exec_by_noid_key: Dict[Tuple, List[int]] = {}
    for j, row in executive.iterrows():
        exec_by_full_key.setdefault(key_full(row), []).append(j)
        if pd.isna(row.approp_id):
            exec_by_noid_key.setdefault(key_no_id(row), []).append(j)

    matched_exec = set()
    records = []

    # PASS 1: exact match on full key (including approp_id)
    for i, row in enacted.iterrows():
        if pd.isna(row.approp_id):
            continue  # handled in pass 2
        candidates = exec_by_full_key.get(key_full(row), [])
        # Prefer unmatched + same reapprop_amount if multiple
        pick = None
        for j in candidates:
            if j in matched_exec:
                continue
            if int(executive.loc[j].reapprop_amount) == int(row.reapprop_amount):
                pick = j
                break
        if pick is None:
            for j in candidates:
                if j not in matched_exec:
                    pick = j
                    break
        if pick is not None:
            matched_exec.add(pick)
            er = executive.loc[pick]
            status = "continued" if int(er.reapprop_amount) == int(row.reapprop_amount) else "modified"
            records.append({
                "status": status,
                "enacted_idx": i,
                "exec_idx": pick,
                "program": row.program,
                "fund": row.fund,
                "chapter_year": int(row.chapter_year),
                "amending_year": int(row.amending_year),
                "approp_id": str(int(float(row.approp_id))),
                "approp_amount": int(row.approp_amount),
                "enacted_reapprop_amount": int(row.reapprop_amount),
                "exec_reapprop_amount": int(er.reapprop_amount),
                "enacted_page": int(row.first_page),
                "enacted_line": int(row.first_line),
                "exec_page": int(er.first_page),
                "exec_line": int(er.first_line),
                "bill_language": row.bill_language,
                "match_method": "full_key",
            })
        else:
            records.append({
                "status": "dropped",
                "enacted_idx": i,
                "exec_idx": -1,
                "program": row.program,
                "fund": row.fund,
                "chapter_year": int(row.chapter_year),
                "amending_year": int(row.amending_year),
                "approp_id": str(int(float(row.approp_id))),
                "approp_amount": int(row.approp_amount),
                "enacted_reapprop_amount": int(row.reapprop_amount),
                "exec_reapprop_amount": 0,
                "enacted_page": int(row.first_page),
                "enacted_line": int(row.first_line),
                "exec_page": -1,
                "exec_line": -1,
                "bill_language": row.bill_language,
                "match_method": "",
            })

    # PASS 2: NaN-ID items — match by (prog, fund, chyr, amnd, amounts) + text similarity
    for i, row in enacted.iterrows():
        if not pd.isna(row.approp_id):
            continue
        candidates = [
            j for j in exec_by_noid_key.get(key_no_id(row), [])
            if j not in matched_exec
        ]
        pick = None
        if candidates:
            # rank by text similarity
            best_sim = 0.0
            for j in candidates:
                sim = text_sim(row.bill_language, executive.loc[j].bill_language)
                if sim > best_sim:
                    best_sim = sim
                    pick = j
            if best_sim < 0.4:
                pick = None  # don't match low-confidence
        if pick is not None:
            matched_exec.add(pick)
            er = executive.loc[pick]
            status = "continued" if int(er.reapprop_amount) == int(row.reapprop_amount) else "modified"
            records.append({
                "status": status,
                "enacted_idx": i,
                "exec_idx": pick,
                "program": row.program,
                "fund": row.fund,
                "chapter_year": int(row.chapter_year),
                "amending_year": int(row.amending_year),
                "approp_id": "",
                "approp_amount": int(row.approp_amount),
                "enacted_reapprop_amount": int(row.reapprop_amount),
                "exec_reapprop_amount": int(er.reapprop_amount),
                "enacted_page": int(row.first_page),
                "enacted_line": int(row.first_line),
                "exec_page": int(er.first_page),
                "exec_line": int(er.first_line),
                "bill_language": row.bill_language,
                "match_method": "no_id_text",
            })
        else:
            records.append({
                "status": "dropped",
                "enacted_idx": i,
                "exec_idx": -1,
                "program": row.program,
                "fund": row.fund,
                "chapter_year": int(row.chapter_year),
                "amending_year": int(row.amending_year),
                "approp_id": "",
                "approp_amount": int(row.approp_amount),
                "enacted_reapprop_amount": int(row.reapprop_amount),
                "exec_reapprop_amount": 0,
                "enacted_page": int(row.first_page),
                "enacted_line": int(row.first_line),
                "exec_page": -1,
                "exec_line": -1,
                "bill_language": row.bill_language,
                "match_method": "no_id_unmatched",
            })

    # New in exec — everything exec that wasn't matched
    for j, row in executive.iterrows():
        if j in matched_exec:
            continue
        records.append({
            "status": "new_in_exec",
            "enacted_idx": -1,
            "exec_idx": j,
            "program": row.program,
            "fund": row.fund,
            "chapter_year": int(row.chapter_year),
            "amending_year": int(row.amending_year),
            "approp_id": "" if pd.isna(row.approp_id) else str(int(float(row.approp_id))),
            "approp_amount": int(row.approp_amount),
            "enacted_reapprop_amount": 0,
            "exec_reapprop_amount": int(row.reapprop_amount),
            "enacted_page": -1,
            "enacted_line": -1,
            "exec_page": int(row.first_page),
            "exec_line": int(row.first_line),
            "bill_language": row.bill_language,
            "match_method": "",
        })

    return pd.DataFrame.from_records(records)


def main():
    enacted = pd.read_csv(ROOT / "outputs" / "enacted_reapprops.csv")
    executive = pd.read_csv(ROOT / "outputs" / "executive_reapprops.csv")

    df = compare(enacted, executive)
    out = ROOT / "outputs" / "comparison.csv"
    df.to_csv(out, index=False)

    # Report
    print(f"\n{'='*72}")
    print("COMPARISON RESULT")
    print(f"{'='*72}")
    status_counts = df.status.value_counts()
    for s, n in status_counts.items():
        print(f"  {s:>16}  {n:>5}")
    print(f"  {'TOTAL':>16}  {len(df):>5}")
    print(f"\n  Enacted in: {len(enacted)}    (should equal continued+modified+dropped = "
          f"{status_counts.get('continued',0) + status_counts.get('modified',0) + status_counts.get('dropped',0)})")
    print(f"  Exec in:    {len(executive)}    (should equal continued+modified+new_in_exec = "
          f"{status_counts.get('continued',0) + status_counts.get('modified',0) + status_counts.get('new_in_exec',0)})")

    # Dropped breakdown
    drops = df[df.status == "dropped"]
    print(f"\n  DROPPED by program:")
    for prog, n in drops.program.value_counts().items():
        print(f"    {n:>3}  {prog}")
    print(f"\n  Total dropped amount (reapprop): ${drops.enacted_reapprop_amount.sum():,}")

    # Match-method breakdown
    print(f"\n  Match methods:")
    for m, n in df.match_method.value_counts().items():
        if m:
            print(f"    {n:>4}  {m}")

    print(f"\n  Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

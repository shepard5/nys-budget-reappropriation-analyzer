"""
Validate extractor counts against BUDGET BREAKDOWN.xlsx ground truth.

For each (year, program, fund) in Sheet1, compare the expected num_reapprops
against what the extractor produced. Show deltas.
"""

import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def load_ground_truth():
    bb = pd.read_excel(ROOT / "inputs" / "budget_breakdown.xlsx", sheet_name="Sheet1")
    bb = bb.iloc[:, :7]
    bb.columns = ["year", "area", "program", "fund", "page_start", "line_start", "expected"]
    # Normalize fund whitespace — the xlsx may have spacing differences
    bb["fund_norm"] = bb["fund"].apply(norm_fund)
    return bb


def norm_fund(s):
    """Normalize fund string: collapse whitespace, ensure exactly '; ' between parts.
    BUDGET BREAKDOWN.xlsx has inconsistent spacing around semicolons (e.g. ';Federal'
    vs '; Federal'); the extractor emits '; ' consistently."""
    s = " ".join(str(s).split())
    # Split on ';' with optional surrounding whitespace, rejoin with '; '
    parts = [p.strip() for p in s.split(";") if p.strip()]
    return "; ".join(parts)


def main():
    bb = load_ground_truth()

    for year, csv_name in [(2025, "enacted_reapprops.csv"), (2026, "executive_reapprops.csv")]:
        df = pd.read_csv(ROOT / "outputs" / csv_name)
        df["fund_norm"] = df["fund"].apply(norm_fund)

        actual = df.groupby(["program", "fund_norm"]).size().reset_index(name="actual")
        expected = bb[bb.year == year][["program", "fund_norm", "expected"]]
        merged = expected.merge(actual, on=["program", "fund_norm"], how="outer")
        merged["expected"] = merged["expected"].fillna(0).astype(int)
        merged["actual"] = merged["actual"].fillna(0).astype(int)
        merged["delta"] = merged["actual"] - merged["expected"]

        print(f"\n{'='*100}")
        print(f"YEAR {year}    (total expected={merged.expected.sum()}  actual={merged.actual.sum()}  delta={merged.delta.sum():+d})")
        print(f"{'='*100}")
        print(f"{'program':<68}  {'fund (head)':<45}  exp  act  Δ")
        for _, row in merged.iterrows():
            fund_head = row.fund_norm.split(";")[0].strip()[:45]
            marker = "  " if row.delta == 0 else ("⚠ " if abs(row.delta) > 2 else "· ")
            print(f"  {str(row.program)[:66]:<66}  {fund_head:<45}  {row.expected:>3}  {row.actual:>3}  {row.delta:+d}  {marker}")


if __name__ == "__main__":
    main()

"""
Join dropped reapprops with SFS undisbursed balance data.

Source: inputs/atl_drops_sfs.xlsx  (ATL Drops sheet) — 2283 rows across all
ATL agencies. Column 'SFS Undisbursed Funds ' (trailing space) holds the
remaining balance from the NYS SFS system.

Match key: (approp_id, chapter_year, approp_amount) — these three stable
fields uniquely identify an item across our extraction and the SFS workbook.
Non-ID items (NaN approp_id) fall back to (chapter_year, approp_amount,
reapprop_amount).

Output: outputs/dropped_with_sfs.csv — one row per dropped item with
sfs_balance, sfs_rounded (up to next $1K), and insert_eligible (bool).

Inserts are produced for items with sfs_balance >= $1,000.
"""

import math
from pathlib import Path
from typing import Optional

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent


def round_up_to_1k(x: float) -> int:
    """Round up to nearest $1,000. 0 -> 0."""
    if pd.isna(x) or x <= 0:
        return 0
    return int(math.ceil(x / 1000.0) * 1000)


def load_sfs_lookup(agency: Optional[str] = None) -> pd.DataFrame:
    """Load SFS undisbursed-balance lookup.

    If `agency` is given, filter to that agency (matches ATL Drops' `agency`
    column). If None (default), load all agencies — required for full-ATL
    runs. The returned DataFrame carries the agency key for multi-agency
    joins.
    """
    df = pd.read_excel(ROOT / "inputs" / "atl_drops_sfs.xlsx", sheet_name="ATL Drops")
    if agency is not None:
        df = df[df.agency == agency].copy()
    else:
        df = df.copy()
    # Normalize column names
    df = df.rename(columns={"SFS Undisbursed Funds ": "sfs_balance",
                             "agency": "agency_s"})
    # Cast keys
    df["approp_id_s"] = df["appropriation id"].apply(
        lambda x: "" if pd.isna(x) else str(int(float(x)))
    )
    df["chapter_year_i"] = df["chapter year"].fillna(0).astype(int)
    df["approp_amount_i"] = df["appropriation amount"].fillna(0).astype(int)
    df["reapprop_amount_i"] = df["reappropriation amount"].fillna(0).astype(int)
    return df[["agency_s", "approp_id_s", "chapter_year_i", "approp_amount_i",
               "reapprop_amount_i", "sfs_balance"]].copy()


def join_sfs(comparison_df: pd.DataFrame, sfs: pd.DataFrame) -> pd.DataFrame:
    """Attach sfs_balance + sfs_rounded + insert_eligible to each dropped row."""
    drops = comparison_df[comparison_df.status == "dropped"].copy()

    def _norm_id(x):
        if pd.isna(x) or x == "" or str(x).strip() == "":
            return ""
        try:
            return str(int(float(x)))
        except (ValueError, TypeError):
            return ""

    drops["approp_id_s"] = drops["approp_id"].apply(_norm_id)
    drops["chapter_year_i"] = drops["chapter_year"].astype(int)
    drops["approp_amount_i"] = drops["approp_amount"].astype(int)
    drops["reapprop_amount_i"] = drops["enacted_reapprop_amount"].astype(int)
    drops["agency_s"] = drops["agency"].fillna("") if "agency" in drops.columns else ""

    # Multi-pass joining, per user's note: "mostly approp ID and chapter year,
    # but sometimes you have to mix it up". We try tighter keys first, then
    # loosen progressively. Track which pass each row matched on.
    id_sfs = sfs[sfs.approp_id_s != ""].copy()
    noid_sfs = sfs[sfs.approp_id_s == ""].copy()

    def _try_merge(left: pd.DataFrame, right: pd.DataFrame, on, method_tag: str) -> pd.DataFrame:
        if len(left) == 0:
            return left.assign(sfs_balance=pd.NA, sfs_match_method="")
        dedup_right = right.drop_duplicates(subset=on, keep="first")
        merged = left.merge(
            dedup_right[on + ["sfs_balance"]], on=on, how="left"
        )
        merged["sfs_match_method"] = merged["sfs_balance"].where(
            merged["sfs_balance"].isna(), method_tag
        ).fillna("")
        return merged

    # Scope matches to the drop's agency. ATL Drops has different agencies
    # that might share approp IDs coincidentally; include agency in every key.
    # PASS A: (agency, approp_id, chapter_year, approp_amount) — tightest
    id_items = drops[drops.approp_id_s != ""].copy()
    a = _try_merge(id_items, id_sfs,
                    ["agency_s", "approp_id_s", "chapter_year_i", "approp_amount_i"],
                    "agency+id+chyr+amt")
    unmatched = a[a.sfs_balance.isna()].drop(columns=["sfs_balance", "sfs_match_method"])
    matched = a[a.sfs_balance.notna()]

    # PASS B: (agency, approp_id, chapter_year) — relax approp_amount
    b = _try_merge(unmatched, id_sfs,
                    ["agency_s", "approp_id_s", "chapter_year_i"], "agency+id+chyr")
    unmatched = b[b.sfs_balance.isna()].drop(columns=["sfs_balance", "sfs_match_method"])
    matched = pd.concat([matched, b[b.sfs_balance.notna()]], ignore_index=True)

    # PASS C: (agency, approp_id, approp_amount) — chapter year mismatch
    c = _try_merge(unmatched, id_sfs,
                    ["agency_s", "approp_id_s", "approp_amount_i"], "agency+id+amt")
    unmatched = c[c.sfs_balance.isna()].drop(columns=["sfs_balance", "sfs_match_method"])
    matched = pd.concat([matched, c[c.sfs_balance.notna()]], ignore_index=True)

    # PASS D: (agency, approp_id) alone — last resort for ID'd items
    d = _try_merge(unmatched, id_sfs,
                    ["agency_s", "approp_id_s"], "agency+id_only")
    unmatched = d[d.sfs_balance.isna()].drop(columns=["sfs_balance", "sfs_match_method"])
    matched = pd.concat([matched, d[d.sfs_balance.notna()]], ignore_index=True)

    # Recombine ID-matched + still-unmatched ID items
    id_final = pd.concat([matched, unmatched.assign(sfs_balance=pd.NA, sfs_match_method="")],
                          ignore_index=True)

    # NaN-ID items: match on (agency, chapter_year, approp_amount, reapprop_amount)
    noid_items = drops[drops.approp_id_s == ""].copy()
    noid = _try_merge(noid_items, noid_sfs,
                      ["agency_s", "chapter_year_i", "approp_amount_i", "reapprop_amount_i"],
                      "agency+noid+chyr+amt+re")

    merged = pd.concat([id_final, noid], ignore_index=True)
    # Drop helper cols
    merged = merged.drop(columns=["agency_s", "approp_id_s", "chapter_year_i",
                                  "approp_amount_i", "reapprop_amount_i"])
    merged["sfs_rounded"] = merged["sfs_balance"].apply(round_up_to_1k)
    merged["insert_eligible"] = merged["sfs_rounded"] >= 1000
    return merged


def main():
    comp = pd.read_csv(ROOT / "outputs" / "comparison.csv")
    # Load all-agency SFS data — the (agency, ...) join key ensures we only
    # match each drop to its own agency's rows.
    sfs = load_sfs_lookup(agency=None)
    print(f"SFS rows loaded (all agencies): {len(sfs)}  "
          f"agencies: {sfs.agency_s.nunique()}")

    merged = join_sfs(comp, sfs)
    out = ROOT / "outputs" / "dropped_with_sfs.csv"
    merged.to_csv(out, index=False)

    print(f"\n{'='*72}")
    print("SFS JOIN")
    print(f"{'='*72}")
    n_drops = len(merged)
    n_sfs_matched = merged.sfs_balance.notna().sum()
    n_sfs_zero = ((merged.sfs_balance.notna()) & (merged.sfs_balance == 0)).sum()
    n_sfs_tiny = ((merged.sfs_balance.notna()) & (merged.sfs_balance > 0) & (merged.sfs_balance < 1000)).sum()
    n_eligible = merged.insert_eligible.sum()
    n_nomatch = merged.sfs_balance.isna().sum()

    print(f"  Total drops:                    {n_drops}")
    print(f"    SFS row matched:              {n_sfs_matched}")
    print(f"      SFS = 0 (fully spent):      {n_sfs_zero}")
    print(f"      SFS 1..999 (below thresh):  {n_sfs_tiny}")
    print(f"      SFS >= 1,000 (ELIGIBLE):    {n_eligible}")
    print(f"    SFS row NOT matched:          {n_nomatch}")

    print(f"\n  Total eligible insert amount (rounded): ${merged[merged.insert_eligible].sfs_rounded.sum():,}")

    # Show the unmatched drops (need manual investigation)
    unmatched = merged[merged.sfs_balance.isna()]
    if len(unmatched):
        print(f"\n  UNMATCHED dropped items (need SFS data):")
        for _, r in unmatched.head(15).iterrows():
            prefix = (r.approp_id or "NO-ID")
            print(f"    {prefix:>5} chyr={r.chapter_year} approp=${int(r.approp_amount):>12,} re=${int(r.enacted_reapprop_amount):>12,} "
                  f"pg{int(r.enacted_page)}:{int(r.enacted_line)}")
        if len(unmatched) > 15:
            print(f"    ... and {len(unmatched) - 15} more")

    print(f"\n  Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

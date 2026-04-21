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
import re
from pathlib import Path
from typing import Dict, Optional

import pandas as pd


from config import ROOT


def round_up_to_1k(x: float) -> int:
    """Round up to nearest $1,000. 0 -> 0."""
    if pd.isna(x) or x <= 0:
        return 0
    return int(math.ceil(x / 1000.0) * 1000)


def _parse_prefix(s) -> str:
    """'23462 - Foo'  ->  '23462'.  Used on the SFS export's 'Budgetary
    Program' and 'Budgetary Department' cells which are formatted
    "<code> - <short description>".  Returns '' if no leading integer."""
    m = re.match(r"^\s*(\d+)", str(s))
    return m.group(1) if m else ""


def _parse_fiscal_year(s) -> Optional[int]:
    """'A200102' -> 2001.  The SFS 'Budgetary Budget Reference' column
    uses an A<STARTYR><ENDYR_SHORT> format."""
    m = re.match(r"^A(\d{4})", str(s))
    return int(m.group(1)) if m else None


def _learn_dept_to_agency_mapping(sfs_df: pd.DataFrame) -> Dict[str, str]:
    """
    When the SFS export is raw (no pre-built composite_key with an agency
    prefix), we have dept codes (3300000, 3900000, ...) but not agency
    names as they appear in the bill ("EDUCATION DEPARTMENT", ...).

    Learn the mapping from the enacted extraction: for each enacted row
    we know the agency name AND the (approp_id, year, approp_amount)
    triple.  Joining against SFS on that triple gives us (agency_name,
    dept_code) pairs; majority-vote per agency to pin down the one
    dept_code it uses.
    """
    enacted_csvs = [
        ROOT / "outputs" / "enacted_reapprops.csv",
        ROOT / "outputs" / "enacted_approps.csv",
    ]
    frames = []
    for p in enacted_csvs:
        if p.exists():
            frames.append(pd.read_csv(p))
    if not frames:
        return {}  # no enacted data yet — can't learn, fall back to no agency
    enacted = pd.concat(frames, ignore_index=True)
    if "agency" not in enacted.columns:
        return {}
    enacted = enacted[enacted["agency"].notna() & (enacted["agency"] != "")].copy()
    enacted["approp_id_s"] = enacted["approp_id"].apply(
        lambda x: "" if pd.isna(x) else str(int(float(x)))
    )
    enacted["year_i"] = enacted["chapter_year"].astype(int)
    enacted["amt_i"] = enacted["approp_amount"].astype(int)
    keys = enacted[["agency", "approp_id_s", "year_i", "amt_i"]].rename(
        columns={"approp_id_s": "approp_id_s", "year_i": "chapter_year_i",
                 "amt_i": "approp_amount_i"}
    )
    joined = keys.merge(
        sfs_df[["approp_id_s", "chapter_year_i", "approp_amount_i", "dept_code"]],
        on=["approp_id_s", "chapter_year_i", "approp_amount_i"],
        how="inner",
    )
    # 5-digit approp IDs aren't globally unique — small-dollar items
    # ($100K member-item range) collide across agencies in (id, yr, amt) space.
    # Large-amount matches are much more discriminating: a $5M+ reapprop with
    # matching (id, yr, amt) almost certainly identifies one real dept_code.
    # Majority-vote per agency using these high-signal matches only.
    big = joined[joined["approp_amount_i"] >= 1_000_000]
    # Fall back to all rows if an agency has no big matches (catches small
    # agencies whose items are all sub-$1M).
    counts_big = big.groupby(["agency", "dept_code"]).size().reset_index(name="n")
    counts_all = joined.groupby(["agency", "dept_code"]).size().reset_index(name="n")
    # Winner per agency: prefer the big-amount vote; fall back to all.
    agencies_with_big = set(big["agency"].unique())
    dept_to_agency: Dict[str, str] = {}
    for agency, group in counts_big.groupby("agency"):
        winner = group.sort_values("n", ascending=False).iloc[0]
        dept_to_agency[str(winner.dept_code)] = agency
    for agency, group in counts_all.groupby("agency"):
        if agency in agencies_with_big:
            continue
        winner = group.sort_values("n", ascending=False).iloc[0]
        # Only add if dept_code not already claimed by a big-match agency.
        if str(winner.dept_code) not in dept_to_agency:
            dept_to_agency[str(winner.dept_code)] = agency
    return dept_to_agency


def load_sfs_from_export(path: Path) -> pd.DataFrame:
    """Load a native SFS sheet export.

    Handles two formats:

    (1) PRE-BUILT COMPOSITE KEY (e.g. SFS, All Education.xlsx — an analyst
        manually added a composite_key formula):
          composite_key column = "AGENCY NAME|approp_id|year|amount"
          Undisbursed Approp Balance column = numeric SFS remaining-balance
        Parsed directly.

    (2) RAW EXPORT (1.1 Appropriation Budgetary Overview.xlsx from SFS as
        downloaded — no composite_key):
          Budgetary Budget Reference  ("A200102" → year 2001)
          Budgetary Program           ("23462 - Foo" → approp_id 23462)
          Budgetary Department        ("3300000 - SED01-..." → dept_code)
          Original Approp Amount / Current Appropriation
          Undisbursed Approp Balance
        We parse these, then learn a dept_code → agency_name mapping by
        joining against the enacted extraction's known agency names, and
        tag each SFS row with its agency.

    Returns a DataFrame with columns:
      agency_s, approp_id_s, chapter_year_i, approp_amount_i,
      reapprop_amount_i, sfs_balance
    """
    # Scan first sheet's top rows for header cells. We resolve column
    # positions by header TEXT so the loader is robust to layout changes.
    xl = pd.ExcelFile(path)

    def _find_col(raw_full: pd.DataFrame, *substr_groups, exclude=()) -> Optional[int]:
        """Return the column index whose header text (across the first ~12
        rows) matches ALL substrings in any of the provided groups and does
        NOT contain any of the `exclude` substrings. Each group is a tuple
        of substrings that must all appear in the cell."""
        exclude_lc = tuple(e.lower() for e in exclude)
        for col_idx in range(raw_full.shape[1]):
            for row_idx in range(min(12, len(raw_full))):
                val = raw_full.iat[row_idx, col_idx]
                if pd.isna(val):
                    continue
                s = str(val)
                s_lc = s.lower()
                if any(ex in s_lc for ex in exclude_lc):
                    continue
                for group in substr_groups:
                    if all(sub.lower() in s_lc for sub in group):
                        return col_idx
        return None

    chosen_sheet = None
    picked = None
    for sheet in xl.sheet_names:
        raw_full = pd.read_excel(path, sheet_name=sheet, header=None)
        ckey_col = _find_col(raw_full, ("composite_key",), ("composite", "key"))
        und_col = _find_col(raw_full, ("Undisbursed", "Balance"))
        if und_col is None:
            continue  # this sheet can't work
        # Determine mode: composite_key pre-built, or raw export
        if ckey_col is not None:
            picked = ("composite", sheet, raw_full, ckey_col, und_col)
            break
        bud_ref = _find_col(raw_full, ("Budget", "Reference"))
        # Exclude "Level N" variants — "Budgetary Program Level 2" is a
        # different column from the approp-id-bearing "Budgetary Program".
        bud_prog = _find_col(raw_full, ("Budgetary", "Program"), exclude=("level",))
        bud_dept = _find_col(raw_full, ("Budgetary", "Department"), exclude=("level",))
        orig_amt = _find_col(raw_full, ("Original", "Approp", "Amount"))
        curr_amt = _find_col(raw_full, ("Current", "Appropriation"))
        if all(c is not None for c in (bud_ref, bud_prog, bud_dept)):
            picked = ("raw", sheet, raw_full, bud_ref, bud_prog, bud_dept,
                      orig_amt, curr_amt, und_col)
            break
    if picked is None:
        raise ValueError(
            f"SFS export at {path} missing required columns "
            f"(need composite_key + Undisbursed Balance, OR raw schema "
            f"with Budget Reference / Program / Department / Approp "
            f"Amount / Undisbursed Balance)."
        )

    mode = picked[0]
    chosen_sheet = picked[1]
    raw_full = picked[2]

    # Find the first row whose cells in the detected columns are DATA
    # (not header labels) — the row where a numeric-looking amount appears.
    def _first_data_row(col_idx: int) -> int:
        for r in range(min(20, len(raw_full))):
            v = raw_full.iat[r, col_idx]
            if pd.isna(v):
                continue
            if isinstance(v, (int, float)) and v != 0:
                return r
        return 12  # fallback

    if mode == "composite":
        _, _, _, ckey_col, und_col = picked
        data_start = _first_data_row(und_col)
        sub = raw_full.iloc[data_start:, [ckey_col, und_col]].copy()
        sub.columns = ["composite_key", "sfs_balance"]
        sub = sub.dropna(subset=["composite_key"])
        parts = sub["composite_key"].astype(str).str.split("|", expand=True)
        if parts.shape[1] < 4:
            raise ValueError(f"composite_key in {path} doesn't have 4 parts")
        sub = sub[parts[3].notna()]
        parts = parts.loc[sub.index]
        sub["agency_s"] = parts[0].str.strip()
        sub["approp_id_s"] = parts[1].fillna("").astype(str).str.strip()
        sub["chapter_year_i"] = pd.to_numeric(parts[2], errors="coerce").fillna(0).astype(int)
        sub["approp_amount_i"] = pd.to_numeric(parts[3], errors="coerce").fillna(0).astype(int)
        sub["sfs_balance"] = pd.to_numeric(sub["sfs_balance"], errors="coerce")
        sub["reapprop_amount_i"] = 0
        print(f"[*] SFS export (composite_key mode): {path.name}  "
              f"sheet={chosen_sheet!r}  rows={len(sub)}  "
              f"agencies={sub['agency_s'].nunique()}")
        return sub[["agency_s", "approp_id_s", "chapter_year_i",
                    "approp_amount_i", "reapprop_amount_i", "sfs_balance"]].copy()

    # mode == "raw" — parse individual columns
    _, _, _, bud_ref, bud_prog, bud_dept, orig_amt, curr_amt, und_col = picked
    data_start = _first_data_row(und_col)
    cols = [bud_ref, bud_prog, bud_dept, und_col]
    col_names = ["bud_ref", "bud_prog", "bud_dept", "sfs_balance"]
    if orig_amt is not None:
        cols.append(orig_amt); col_names.append("orig_amt")
    if curr_amt is not None:
        cols.append(curr_amt); col_names.append("curr_amt")
    sub = raw_full.iloc[data_start:, cols].copy()
    sub.columns = col_names
    sub = sub.dropna(subset=["bud_ref", "bud_prog"])

    sub["chapter_year_i"] = sub["bud_ref"].apply(_parse_fiscal_year)
    sub["approp_id_s"] = sub["bud_prog"].apply(_parse_prefix)
    sub["dept_code"] = sub["bud_dept"].apply(_parse_prefix)
    sub["sfs_balance"] = pd.to_numeric(sub["sfs_balance"], errors="coerce")

    # Amount: prefer Original Approp Amount; fall back to Current Appropriation
    # when original is 0 (new items in the current fiscal year).
    if "orig_amt" in sub.columns:
        sub["approp_amount_i"] = pd.to_numeric(sub["orig_amt"], errors="coerce").fillna(0).astype(int)
    else:
        sub["approp_amount_i"] = 0
    if "curr_amt" in sub.columns:
        fallback_mask = sub["approp_amount_i"] == 0
        sub.loc[fallback_mask, "approp_amount_i"] = (
            pd.to_numeric(sub.loc[fallback_mask, "curr_amt"], errors="coerce").fillna(0).astype(int)
        )

    # Drop rows with no year. Keep member items (approp_id_s="", e.g. "M0001",
    # "N3773") — they're needed to match bill lines like "Afton Driving Park"
    # that have no 5-digit approp ID. The noid-join downstream will key on
    # (agency, chyr, approp_amount, reapprop_amount) for these rows.
    sub = sub.dropna(subset=["chapter_year_i"])
    sub["chapter_year_i"] = sub["chapter_year_i"].astype(int)

    # Learn dept_code → agency_name by joining against enacted extraction.
    mapping = _learn_dept_to_agency_mapping(sub)
    if not mapping:
        print(f"[!] Could not learn dept_code→agency mapping "
              f"(no enacted extraction found yet); tagging with empty agency.")
        sub["agency_s"] = ""
    else:
        sub["agency_s"] = sub["dept_code"].map(mapping).fillna("")

    sub["reapprop_amount_i"] = 0
    print(f"[*] SFS export (raw mode): {path.name}  sheet={chosen_sheet!r}  "
          f"rows={len(sub)}  dept_codes={sub['dept_code'].nunique()}  "
          f"agencies resolved={sub[sub['agency_s'] != ''].agency_s.nunique()}  "
          f"SFS rows tagged={(sub['agency_s'] != '').sum()}")
    return sub[["agency_s", "approp_id_s", "chapter_year_i",
                "approp_amount_i", "reapprop_amount_i", "sfs_balance"]].copy()


def load_sfs_from_atl_drops(agency: Optional[str] = None) -> pd.DataFrame:
    """Fallback SFS loader — use the manually-populated SFS column in
    ATL Drops.xlsx when the native SFS sheet export isn't available.
    Legacy path; prefer load_sfs_from_export()."""
    df = pd.read_excel(ROOT / "inputs" / "atl_drops_sfs.xlsx", sheet_name="ATL Drops")
    if agency is not None:
        df = df[df.agency == agency].copy()
    else:
        df = df.copy()
    df = df.rename(columns={"SFS Undisbursed Funds ": "sfs_balance",
                             "agency": "agency_s"})
    df["approp_id_s"] = df["appropriation id"].apply(
        lambda x: "" if pd.isna(x) else str(int(float(x)))
    )
    df["chapter_year_i"] = df["chapter year"].fillna(0).astype(int)
    df["approp_amount_i"] = df["appropriation amount"].fillna(0).astype(int)
    df["reapprop_amount_i"] = df["reappropriation amount"].fillna(0).astype(int)
    return df[["agency_s", "approp_id_s", "chapter_year_i",
               "approp_amount_i", "reapprop_amount_i", "sfs_balance"]].copy()


def load_sfs_lookup(agency: Optional[str] = None) -> pd.DataFrame:
    """Load SFS undisbursed-balance lookup — prefer native SFS export.

    Resolution order:
      1. inputs/sfs_export.xlsx (native SFS sheet export with composite_key
         + Undisbursed Approp Balance) — authoritative, 7,747 Education rows
      2. inputs/atl_drops_sfs.xlsx (ATL Drops with manually-populated SFS
         column) — legacy fallback, covers only the ~400 items the analyst
         looked up
    """
    sfs_native = ROOT / "inputs" / "sfs_export.xlsx"
    if sfs_native.exists():
        df = load_sfs_from_export(sfs_native)
        if agency is not None:
            df = df[df.agency_s == agency].copy()
        return df
    return load_sfs_from_atl_drops(agency=agency)


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

    # NaN-ID items: either (a) member items whose allocation rows never had
    # a 5-digit approp_id in the bill, or (b) large reapprops where the
    # extractor missed the approp_id due to formatting quirks (e.g. DOH
    # Essential Plan $2.5B, which SFS knows as approp_id 59054).
    #
    # Search the FULL SFS table, not just the noid subset — the SFS record
    # may have an approp_id even when our bill extraction didn't surface it.
    # Pass A: tightest (agency, chyr, approp_amount, reapprop_amount)
    # Pass B: loose (agency, chyr, approp_amount)
    noid_items = drops[drops.approp_id_s == ""].copy()
    na = _try_merge(noid_items, sfs,
                    ["agency_s", "chapter_year_i", "approp_amount_i", "reapprop_amount_i"],
                    "agency+noid+chyr+amt+re")
    still_unmatched = na[na.sfs_balance.isna()].drop(columns=["sfs_balance", "sfs_match_method"])
    noid_matched = na[na.sfs_balance.notna()]
    nb = _try_merge(still_unmatched, sfs,
                    ["agency_s", "chapter_year_i", "approp_amount_i"],
                    "agency+noid+chyr+amt")
    noid = pd.concat([noid_matched, nb], ignore_index=True)

    merged = pd.concat([id_final, noid], ignore_index=True)
    # Drop helper cols
    merged = merged.drop(columns=["agency_s", "approp_id_s", "chapter_year_i",
                                  "approp_amount_i", "reapprop_amount_i"])
    # Eligibility: raw SFS balance must be >= $1,000. Rounding up a $500
    # balance to $1,000 would otherwise mis-classify it as eligible.
    # Per user's rule: "anything less than $1,000 is left dropped".
    merged["sfs_rounded"] = merged["sfs_balance"].apply(round_up_to_1k)
    merged["insert_eligible"] = merged["sfs_balance"] >= 1000
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

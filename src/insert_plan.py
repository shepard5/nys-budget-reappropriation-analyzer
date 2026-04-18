"""
Build the insert plan: group eligible drops into labeled insert blocks.

Algorithm (user's "alternative method" from prompt 2):

  Anchors in 26-27 exec = continued + modified reapprops (items with a 1:1
  match to 25-26 enacted). Walk pairs of adjacent anchors in exec document
  order. For each pair (A, B), map back to enacted and inspect the enacted
  reapprops that lived between A' and B'. Eligible drops in that range form
  ONE insert, placed in 26-27 between A and B.

  Split rule: because anchors are continued items, no two survivors within a
  single (A,B) gap are separated by a continued item — those splits are
  handled automatically by picking *adjacent* anchors. Ineligible drops
  (SFS < $1K or unmatched) stay inside the insert as struck context.

Chapter-year-dropped edge case: if a survivor's (program, fund, chapter_year,
amending_year) has no counterpart chapter-year block in 26-27, the chapter
header must be INCLUDED in the insert (not struck), so the reapprop retains
its legal context.

Labels: `{exec_page}{letter}` — exec_page is 1-indexed within the exec PDF
(268-352). Letter A, B, C... per insert on that page, ordered by position.

Output: outputs/insert_plan.json
"""

import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


# PDF page offsets.
# - exec PDF covers pages 268-352, HTML page idx 0 = PDF 268.
# - enacted reapprop section covers pages 315-450, HTML idx 0 = PDF 315.
# - enacted appropriation section covers pages 264-314, HTML idx 0 = PDF 264.
EXEC_PDF_PAGE_OFFSET = 268
ENACTED_PDF_PAGE_OFFSET = {
    "reapprop": 315,
    "appropriation": 264,
}


def _compute_exec_fund_header_positions() -> Dict:
    """
    Walk the cached executive HTML and locate the <p> index where each
    (program, fund) block's FUND-FAMILY line lives (e.g., "Special Revenue
    Funds - Federal"). Used for `before_next_fund` anchor placement: the
    label should sit ABOVE the fund-header lines of the next fund, not
    between that fund's chyr header and body.

    Returns dict keyed by (program, fund) -> {
        "page_html": int,
        "p_idx": int,          # index of fund-family <p> within the page
        "line_num": int,       # visible line num of fund-family line
    }
    """
    from bs4 import BeautifulSoup
    cache_html = ROOT / "cache" / "executive_26-27.html"
    if not cache_html.exists():
        return {}
    soup = BeautifulSoup(cache_html.read_text(), "lxml")
    pages = soup.find_all("div", class_="page")

    from patterns import PROGRAM_RE, FUND_TOP_RE, CHAPTER_YEAR_RE, LINE_NUM_RE

    result: Dict = {}
    current_program = ""
    current_fund_parts: List[str] = []
    for page_idx, page in enumerate(pages):
        ps = page.find_all("p")
        for p_idx, p in enumerate(ps):
            raw = p.get_text()
            if not raw.strip():
                continue
            m = LINE_NUM_RE.match(raw)
            if not m:
                continue
            line_num = int(m.group(1))
            t = m.group(2).strip()
            if PROGRAM_RE.match(t):
                current_program = t
                current_fund_parts = []
                continue
            if FUND_TOP_RE.match(t):
                # Start of a new fund block — record this <p> as the fund-family
                # position. Then accumulate the next 1-2 sub-fund lines to build
                # the full fund string.
                fund_family_pos = {"page_html": page_idx, "p_idx": p_idx, "line_num": line_num}
                current_fund_parts = [t]
                # Peek ahead within same page for sub-fund parts.
                j = p_idx + 1
                while j < len(ps) and len(current_fund_parts) < 3:
                    pj = ps[j]
                    tj_raw = pj.get_text()
                    if not tj_raw.strip():
                        j += 1
                        continue
                    mj = LINE_NUM_RE.match(tj_raw)
                    if not mj:
                        j += 1
                        continue
                    tj = mj.group(2).strip()
                    if (PROGRAM_RE.match(tj) or FUND_TOP_RE.match(tj) or
                            CHAPTER_YEAR_RE.match(tj)):
                        break
                    current_fund_parts.append(tj)
                    j += 1
                if current_program and current_fund_parts:
                    key = (current_program, "; ".join(current_fund_parts))
                    if key not in result:
                        result[key] = fund_family_pos
    return result


def main():
    # Enacted is reapprops + appropriations, concatenated in the same order
    # compare.py used (reapprops first, then approps) so enacted_idx aligns.
    ea = pd.read_csv(ROOT / "outputs" / "enacted_reapprops.csv")
    ap_path = ROOT / "outputs" / "enacted_approps.csv"
    if ap_path.exists():
        eb = pd.read_csv(ap_path)
        enacted = pd.concat([ea, eb], ignore_index=True)
    else:
        enacted = ea
    enacted = enacted.reset_index(drop=True)
    executive = pd.read_csv(ROOT / "outputs" / "executive_reapprops.csv").reset_index(drop=True)
    comp = pd.read_csv(ROOT / "outputs" / "comparison.csv")
    sfs = pd.read_csv(ROOT / "outputs" / "dropped_with_sfs.csv")

    # Precompute exact fund-header positions in exec HTML for precise
    # `before_next_fund` anchor placement.
    exec_fund_header_pos = _compute_exec_fund_header_positions()

    # Build match map: enacted_idx -> exec_idx (only continued/modified)
    matches = comp[comp.status.isin(["continued", "modified"])]
    match_map: Dict[int, int] = {int(r.enacted_idx): int(r.exec_idx) for _, r in matches.iterrows()}

    # Reverse: exec_idx -> enacted_idx
    rev_map: Dict[int, int] = {v: k for k, v in match_map.items()}

    # Build eligibility lookup: enacted_idx -> (sfs_rounded, sfs_balance) when eligible
    eligible_map: Dict[int, dict] = {}
    for _, r in sfs[sfs.insert_eligible].iterrows():
        eligible_map[int(r.enacted_idx)] = {
            "sfs_balance": float(r.sfs_balance) if pd.notna(r.sfs_balance) else 0.0,
            "sfs_rounded": int(r.sfs_rounded),
        }

    # Sort exec reapprops by document order: (first_page, first_line)
    executive_sorted = executive.sort_values(["first_page", "first_line"]).reset_index()
    executive_sorted = executive_sorted.rename(columns={"index": "orig_exec_idx"})

    # Build list of anchor exec_idx, sorted by their matched enacted doc
    # position. Walking pairs in this order ensures non-overlapping enacted
    # ranges, even when exec-order and enacted-order diverge (e.g. when
    # chyr 2025 approp items appear in different relative positions between
    # the two documents).
    _candidate_anchors = [
        int(r.orig_exec_idx) for _, r in executive_sorted.iterrows()
        if int(r.orig_exec_idx) in rev_map
    ]
    # (Note: at this point `enacted_position` is built below; defer sort until
    # after it's available. For now, just collect; we sort right after.)
    exec_anchors: List[int] = _candidate_anchors

    # Build a set of (program, fund, chapter_year, amending_year) that exist in EXEC
    # — used to detect chapter-year-dropped edge case
    exec_chyr_set = set()
    for _, r in executive.iterrows():
        exec_chyr_set.add((r.program, r.fund, int(r.chapter_year), int(r.amending_year)))

    # Build a set of (program, fund) that exist in EXEC — fund-dropped edge case
    exec_fund_set = {(r.program, r.fund) for _, r in executive.iterrows()}

    # Sort enacted by true document order. Appropriation pages (HTML 0..50 for
    # PDF 264-314) come BEFORE reapprop pages (HTML 0..135 for PDF 315-450);
    # both use HTML page indices starting at 0 so a naive (first_page,
    # first_line) sort would interleave them. Use source as the primary key.
    SOURCE_ORDER = {"appropriation": 0, "reapprop": 1}
    if "source" in enacted.columns:
        enacted_sorted = enacted.assign(
            _src_order=enacted["source"].map(SOURCE_ORDER).fillna(1)
        ).sort_values(["_src_order", "first_page", "first_line"])
    else:
        enacted_sorted = enacted.sort_values(["first_page", "first_line"])
    enacted_order = enacted_sorted.index.tolist()
    enacted_position = {idx: pos for pos, idx in enumerate(enacted_order)}

    # Now that enacted_position exists, re-sort exec_anchors by it so pair
    # iteration walks monotonic enacted positions (no overlapping gaps).
    exec_anchors.sort(key=lambda ei: enacted_position[rev_map[ei]])

    exec_program_set = {r.program for _, r in executive.iterrows()}

    # Ordered list of unique (program, fund) in ENACTED doc order — used to
    # find the "next fund" after a missing-in-exec fund, for label placement.
    # Built per-source, because the appropriations and reapprops sections are
    # ORDERED INDEPENDENTLY within the 25-26 bill: a fund might appear in
    # both sections but at different relative positions (e.g. PK Federal Dept
    # of Ed Account shows up on enacted approps pg 43 AND reapprops pg 117).
    # For `before_next_fund`, we want the next fund that appears AFTER the
    # survivor's fund IN THE SAME SECTION.
    enacted_fund_order_by_source: Dict[str, List] = {"appropriation": [], "reapprop": []}
    _seen_by_source: Dict[str, set] = {"appropriation": set(), "reapprop": set()}
    for idx in enacted_order:
        r = enacted.loc[idx]
        src = r.source if "source" in enacted.columns else "reapprop"
        key = (r.program, r.fund)
        if key not in _seen_by_source[src]:
            _seen_by_source[src].add(key)
            enacted_fund_order_by_source[src].append(key)

    # For each structural group in exec, record the FIRST reapprop's position.
    # The "header" for that group conceptually sits just before that position.
    # Used for anchor placement when a survivor has no preceding continued item
    # in its own (program, fund) scope.
    exec_chyr_first_pos: Dict = {}
    exec_fund_first_pos: Dict = {}
    exec_program_first_pos: Dict = {}
    for _, r in executive.sort_values(["first_page", "first_line"]).iterrows():
        chyr_key = (r.program, r.fund, int(r.chapter_year), int(r.amending_year))
        fund_key = (r.program, r.fund)
        prog_key = r.program
        pos = (int(r.first_page), int(r.first_line))
        if chyr_key not in exec_chyr_first_pos:
            exec_chyr_first_pos[chyr_key] = pos
        if fund_key not in exec_fund_first_pos:
            exec_fund_first_pos[fund_key] = pos
        if prog_key not in exec_program_first_pos:
            exec_program_first_pos[prog_key] = pos

    def _pick_anchor_upper(first_survivor_idx: int):
        """
        Find the best exec anchor_upper for an insert whose first survivor is
        at enacted index `first_survivor_idx`. Respects structural scope:
        prefer the nearest preceding continued reapprop in the SAME
        (program, fund); if none, fall back to the chyr header position in exec,
        then fund header, then program header, then doc start.

        Returns (anchor_upper_dict, label_pdf_page).
        """
        surv = enacted.loc[first_survivor_idx]
        s_prog, s_fund = surv.program, surv.fund
        s_chyr = int(surv.chapter_year)
        s_amend = int(surv.amending_year)
        first_pos_in_enacted = enacted_position[first_survivor_idx]

        # 1. Walk back in enacted doc order within same (program, fund).
        #    Use the first continued/modified item's exec position.
        for pos in range(first_pos_in_enacted - 1, -1, -1):
            idx = enacted_order[pos]
            r = enacted.loc[idx]
            if r.program != s_prog or r.fund != s_fund:
                break  # left the scope
            if idx in match_map:
                ex_idx = match_map[idx]
                e = executive.loc[ex_idx]
                last_line = int(e.last_line) if pd.notna(e.last_line) else int(e.first_line)
                last_page = int(e.last_page) if pd.notna(e.last_page) else int(e.first_page)
                return {
                    "exec_idx": int(ex_idx),
                    "exec_page_html": last_page,
                    "exec_line": last_line,
                    "exec_page_pdf": last_page + EXEC_PDF_PAGE_OFFSET,
                    "anchor_kind": "continued_same_fund",
                }, last_page + EXEC_PDF_PAGE_OFFSET

        # 2. Survivor's chyr block EXISTS in exec — label goes at TOP of that
        #    block (just before the first reapprop in it).
        chyr_key = (s_prog, s_fund, s_chyr, s_amend)
        if chyr_key in exec_chyr_first_pos:
            pg, ln = exec_chyr_first_pos[chyr_key]
            return {
                "exec_idx": None,
                "exec_page_html": pg,
                "exec_line": max(ln - 1, 0),
                "exec_page_pdf": pg + EXEC_PDF_PAGE_OFFSET,
                "anchor_kind": "chyr_header",
            }, pg + EXEC_PDF_PAGE_OFFSET

        # 3. Survivor's chyr DOES NOT exist in exec — place after the nearest
        #    NEWER chyr block in the same (program, fund). Chapter-year order
        #    in the bill is newest-first, so survivor chyr 2024 sits between
        #    chyr 2025 (above) and chyr 2023 (below).
        newer_chyrs_in_fund = [
            k for k in exec_chyr_first_pos
            if k[0] == s_prog and k[1] == s_fund
            and (k[2], k[3]) > (s_chyr, s_amend)
        ]
        if newer_chyrs_in_fund:
            # Pick the OLDEST among the newer ones (closest to our year from above)
            closest = min(newer_chyrs_in_fund, key=lambda k: (k[2], k[3]))
            chyr_items = executive[
                (executive.program == closest[0]) &
                (executive.fund == closest[1]) &
                (executive.chapter_year == closest[2]) &
                (executive.amending_year == closest[3])
            ]
            last = chyr_items.sort_values(["first_page", "first_line"]).iloc[-1]
            last_line = int(last.last_line) if pd.notna(last.last_line) else int(last.first_line)
            last_page = int(last.last_page) if pd.notna(last.last_page) else int(last.first_page)
            return {
                "exec_idx": None,
                "exec_page_html": last_page,
                "exec_line": last_line,
                "exec_page_pdf": last_page + EXEC_PDF_PAGE_OFFSET,
                "anchor_kind": "after_newer_chyr",
            }, last_page + EXEC_PDF_PAGE_OFFSET

        fund_key = (s_prog, s_fund)

        # 4. Survivor's FUND is missing from exec entirely — place the label
        #    just before the FIRST reapprop of the NEXT fund (in enacted doc
        #    order, same source section + same program) that still exists
        #    in exec. This keeps the insert in its semantically correct slot
        #    within the program.
        if fund_key not in exec_fund_first_pos:
            surv_src = surv.source if "source" in enacted.columns else "reapprop"
            surv_fund_order = enacted_fund_order_by_source.get(surv_src, [])
            try:
                pos_in_order = surv_fund_order.index(fund_key)
                next_fund_key = None
                for later_key in surv_fund_order[pos_in_order + 1:]:
                    if later_key[0] != s_prog:
                        break  # left the program
                    if later_key in exec_fund_first_pos:
                        next_fund_key = later_key
                        break
                if next_fund_key is not None:
                    # Prefer the exact fund-family line position if we have it
                    # (precomputed from exec HTML); otherwise fall back to
                    # (first-reapprop line - 1) as before.
                    hdr = exec_fund_header_pos.get(next_fund_key)
                    if hdr is not None:
                        pg = hdr["page_html"]
                        ln = max(hdr["line_num"] - 1, 0)
                    else:
                        pg, first_ln = exec_fund_first_pos[next_fund_key]
                        ln = max(first_ln - 1, 0)
                    return {
                        "exec_idx": None,
                        "exec_page_html": pg,
                        "exec_line": ln,
                        "exec_page_pdf": pg + EXEC_PDF_PAGE_OFFSET,
                        "anchor_kind": "before_next_fund",
                    }, pg + EXEC_PDF_PAGE_OFFSET
            except ValueError:
                pass

        # 5. Fund exists in exec but survivor's chyr is newer than any chyr
        #    there (rare). Place at top of fund.
        if fund_key in exec_fund_first_pos:
            pg, ln = exec_fund_first_pos[fund_key]
            return {
                "exec_idx": None,
                "exec_page_html": pg,
                "exec_line": max(ln - 1, 0),
                "exec_page_pdf": pg + EXEC_PDF_PAGE_OFFSET,
                "anchor_kind": "fund_header",
            }, pg + EXEC_PDF_PAGE_OFFSET

        # 4. Program header (fund dropped; program exists).
        if s_prog in exec_program_first_pos:
            pg, ln = exec_program_first_pos[s_prog]
            return {
                "exec_idx": None,
                "exec_page_html": pg,
                "exec_line": max(ln - 1, 0),
                "exec_page_pdf": pg + EXEC_PDF_PAGE_OFFSET,
                "anchor_kind": "program_header",
            }, pg + EXEC_PDF_PAGE_OFFSET

        # 5. Doc start.
        return {
            "exec_idx": None,
            "exec_page_html": 0,
            "exec_line": 0,
            "exec_page_pdf": EXEC_PDF_PAGE_OFFSET,
            "anchor_kind": "doc_start",
        }, EXEC_PDF_PAGE_OFFSET

    # Walk pairs of adjacent exec anchors. For each pair, find enacted items
    # between them, identify eligible survivors, AND sub-split on any "blocker"
    # between consecutive survivors (so each insert is one contiguous unstruck
    # run in the 25-26 source).
    inserts: List[dict] = []

    anchor_pairs: List = []
    for i in range(len(exec_anchors) + 1):
        upper_exec = exec_anchors[i - 1] if i > 0 else None
        lower_exec = exec_anchors[i] if i < len(exec_anchors) else None
        anchor_pairs.append((upper_exec, lower_exec))

    def _is_blocker_between(prev_idx: int, curr_idx: int) -> bool:
        """
        Return True if something 'struck' sits between the two enacted
        reapprops at prev_idx and curr_idx in doc order — anything that would
        make the insert PDF show a struck block between their unstruck blocks.

        Blockers:
          1. Any non-survivor reapprop in enacted between them.
          2. Structural transition (program / fund / chapter-year change) where
             the target exists in exec (so the struck header would appear).
        """
        prev_pos = enacted_position[prev_idx]
        curr_pos = enacted_position[curr_idx]
        # Any non-survivor reapprop between?
        for p in range(prev_pos + 1, curr_pos):
            mid_idx = enacted_order[p]
            if mid_idx not in eligible_map:
                return True

        # Structural transition check
        prev_r = enacted.loc[prev_idx]
        curr_r = enacted.loc[curr_idx]
        # Program change
        if prev_r.program != curr_r.program:
            if curr_r.program in exec_program_set:
                return True  # program header in exec → struck in insert
        # Fund change (within same program)
        elif prev_r.fund != curr_r.fund:
            if (curr_r.program, curr_r.fund) in exec_fund_set:
                return True
        # Chapter-year change (within same fund)
        else:
            prev_chyr = (prev_r.program, prev_r.fund, int(prev_r.chapter_year), int(prev_r.amending_year))
            curr_chyr = (curr_r.program, curr_r.fund, int(curr_r.chapter_year), int(curr_r.amending_year))
            if prev_chyr != curr_chyr and curr_chyr in exec_chyr_set:
                return True
        return False

    def _build_insert(survivors_list: List[int], upper_exec, lower_exec) -> dict:
        """Build an insert spec given a list of enacted indices (all survivors)."""
        first_surv = survivors_list[0]
        last_surv = survivors_list[-1]
        first_page = int(enacted.loc[first_surv].first_page)
        last_page = max(int(enacted.loc[idx].last_page) for idx in survivors_list)
        # All survivors in an insert share the same source (insert_plan only
        # groups contiguous same-fund survivors, and source is implied by
        # which section they live in).
        survivor_source = (
            enacted.loc[first_surv].source if "source" in enacted.columns else "reapprop"
        )

        # Non-survivors BETWEEN these survivors in enacted order (will be struck)
        first_pos = enacted_position[first_surv]
        last_pos = enacted_position[last_surv]
        non_survivors = [
            enacted_order[p] for p in range(first_pos, last_pos + 1)
            if enacted_order[p] not in set(survivors_list)
        ]

        chapter_year_needs_header = {}
        fund_needs_header = {}
        for idx in survivors_list:
            r = enacted.loc[idx]
            chapter_year_needs_header[idx] = (
                r.program, r.fund, int(r.chapter_year), int(r.amending_year)
            ) not in exec_chyr_set
            fund_needs_header[idx] = (r.program, r.fund) not in exec_fund_set

        # Pick anchor_upper that respects (program, fund) scope of survivors.
        anchor_upper, label_pdf_page = _pick_anchor_upper(survivors_list[0])
        target_exec_page_idx = anchor_upper["exec_page_html"]
        target_exec_line = anchor_upper["exec_line"]

        return {
            "survivors": [
                {
                    "enacted_idx": int(idx),
                    "approp_id": "" if pd.isna(enacted.loc[idx].approp_id) else str(int(float(enacted.loc[idx].approp_id))),
                    "chapter_year": int(enacted.loc[idx].chapter_year),
                    "amending_year": int(enacted.loc[idx].amending_year),
                    "program": enacted.loc[idx].program,
                    "fund": enacted.loc[idx].fund,
                    "approp_amount": int(enacted.loc[idx].approp_amount),
                    "old_reapprop_amount": int(enacted.loc[idx].reapprop_amount),
                    "new_reapprop_amount": eligible_map[idx]["sfs_rounded"],
                    "sfs_balance": eligible_map[idx]["sfs_balance"],
                    "first_page": int(enacted.loc[idx].first_page),
                    "first_line": int(enacted.loc[idx].first_line),
                    "last_page": int(enacted.loc[idx].last_page),
                    "last_line": int(enacted.loc[idx].last_line),
                    "needs_chapter_header": chapter_year_needs_header[idx],
                    "needs_fund_header": fund_needs_header[idx],
                    "source": (
                        enacted.loc[idx].source
                        if "source" in enacted.columns
                        else "reapprop"
                    ),
                }
                for idx in survivors_list
            ],
            "struck_non_survivors": [
                {
                    "enacted_idx": int(idx),
                    "approp_id": "" if pd.isna(enacted.loc[idx].approp_id) else str(int(float(enacted.loc[idx].approp_id))),
                    "chapter_year": int(enacted.loc[idx].chapter_year),
                    "first_page": int(enacted.loc[idx].first_page),
                    "first_line": int(enacted.loc[idx].first_line),
                    "last_page": int(enacted.loc[idx].last_page),
                    "last_line": int(enacted.loc[idx].last_line),
                }
                for idx in non_survivors
            ],
            "source_enacted_page_range_pdf": [
                first_page + ENACTED_PDF_PAGE_OFFSET[survivor_source],
                last_page + ENACTED_PDF_PAGE_OFFSET[survivor_source],
            ],
            "source_enacted_page_range_html": [first_page, last_page],
            "anchor_upper": anchor_upper,
            "anchor_lower": {
                "exec_idx": lower_exec,
                "exec_page_html": int(executive.loc[lower_exec].first_page) if lower_exec is not None else None,
                "exec_line": int(executive.loc[lower_exec].first_line) if lower_exec is not None else None,
            },
            "label_pdf_page": label_pdf_page,
        }

    for upper_exec, lower_exec in anchor_pairs:
        upper_enacted_pos = enacted_position[rev_map[upper_exec]] if upper_exec is not None else -1
        lower_enacted_pos = enacted_position[rev_map[lower_exec]] if lower_exec is not None else len(enacted_order)
        between_idxs = [enacted_order[p] for p in range(upper_enacted_pos + 1, lower_enacted_pos)]
        survivors = [idx for idx in between_idxs if idx in eligible_map]
        if not survivors:
            continue

        # Sub-split: walk survivors in doc order, split whenever a blocker sits
        # between two consecutive survivors.
        groups: List[List[int]] = [[survivors[0]]]
        for a, b in zip(survivors, survivors[1:]):
            if _is_blocker_between(a, b):
                groups.append([b])
            else:
                groups[-1].append(b)

        for g in groups:
            inserts.append(_build_insert(g, upper_exec, lower_exec))

    # Assign labels: per exec PDF page, order insert groups by their anchor's
    # exec line; A/B/C...
    inserts_by_page: Dict[int, List[int]] = defaultdict(list)
    for i, ins in enumerate(inserts):
        inserts_by_page[ins["label_pdf_page"]].append(i)

    for page, idxs in inserts_by_page.items():
        # Sort by anchor_upper.exec_line, then by first survivor's enacted
        # (page, line) so sub-split groups appear in document order
        idxs.sort(key=lambda i: (
            inserts[i]["anchor_upper"]["exec_line"] or 0,
            inserts[i]["survivors"][0]["first_page"],
            inserts[i]["survivors"][0]["first_line"],
        ))
        for letter_i, ins_i in enumerate(idxs):
            letter = chr(ord("A") + letter_i)
            inserts[ins_i]["label"] = f"{page}{letter}"

    # Invariants (cheap runtime assertions to catch regressions)
    _all_survivor_idxs = [int(s["enacted_idx"]) for ins in inserts for s in ins["survivors"]]
    _eligible_idxs = set(eligible_map)
    assert len(_all_survivor_idxs) == len(set(_all_survivor_idxs)), \
        f"Duplicate survivor enacted_idxs in plan: {len(_all_survivor_idxs) - len(set(_all_survivor_idxs))}"
    assert set(_all_survivor_idxs) == _eligible_idxs, \
        f"Mismatch: plan={len(_all_survivor_idxs)} eligible={len(_eligible_idxs)} missing={_eligible_idxs - set(_all_survivor_idxs)} extra={set(_all_survivor_idxs) - _eligible_idxs}"
    _labels = [ins["label"] for ins in inserts]
    assert len(_labels) == len(set(_labels)), \
        f"Duplicate labels in plan: {[l for l in _labels if _labels.count(l) > 1]}"
    _plan_total = sum(s["new_reapprop_amount"] for ins in inserts for s in ins["survivors"])
    _eligible_total = sum(v["sfs_rounded"] for v in eligible_map.values())
    assert _plan_total == _eligible_total, \
        f"Dollar total mismatch: plan=${_plan_total:,} eligible=${_eligible_total:,}"

    out = ROOT / "outputs" / "insert_plan.json"
    out.write_text(json.dumps(inserts, indent=2))

    # Summary
    n_inserts = len(inserts)
    n_survivors = sum(len(ins["survivors"]) for ins in inserts)
    total_insert_amount = sum(
        s["new_reapprop_amount"] for ins in inserts for s in ins["survivors"]
    )
    n_chyr_dropped = sum(
        1 for ins in inserts for s in ins["survivors"] if s["needs_chapter_header"]
    )
    n_fund_dropped = sum(
        1 for ins in inserts for s in ins["survivors"] if s["needs_fund_header"]
    )

    print(f"\n{'='*72}")
    print("INSERT PLAN")
    print(f"{'='*72}")
    print(f"  Inserts:                        {n_inserts}")
    print(f"  Total survivors:                {n_survivors}")
    print(f"  Inserts per exec page (top 10):")
    per_page = sorted([(p, len(v)) for p, v in inserts_by_page.items()],
                      key=lambda x: -x[1])
    for p, n in per_page[:10]:
        print(f"    page {p}: {n} inserts ({' '.join(inserts[i]['label'] for i in inserts_by_page[p])})")
    print(f"\n  Survivors needing chapter-year header insert: {n_chyr_dropped}")
    print(f"  Survivors needing fund header insert:         {n_fund_dropped}")
    print(f"  Total rounded reapprop amount to re-add:      ${total_insert_amount:,}")

    # Show first 5 inserts compact
    print(f"\n  First 5 inserts:")
    for ins in inserts[:5]:
        ids = ",".join(s["approp_id"] or "?" for s in ins["survivors"])
        pr = ins["source_enacted_page_range_pdf"]
        print(f"    {ins['label']:>6s}  enacted pp {pr[0]}-{pr[1]}  "
              f"{len(ins['survivors'])} survivor(s) [{ids}]  "
              f"${sum(s['new_reapprop_amount'] for s in ins['survivors']):,}")
    print(f"\n  Saved: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

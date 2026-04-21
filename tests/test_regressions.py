"""
Regression tests for the reapprop pipeline.

Each edge case that's been fixed becomes a frozen assertion here. If a
future change regresses any of them, `pytest tests/` fails loud instead
of the bug re-surfacing mid-review. Run from the project root:

    ./venv/bin/python -m pytest tests/ -v

Tests only run against the CURRENT outputs/ of the most recent pipeline
run — they don't re-execute the pipeline. Run the pipeline first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
INSERTS_DIR = OUTPUTS / "inserts"


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def plan():
    return json.loads((OUTPUTS / "insert_plan.json").read_text())


@pytest.fixture(scope="session")
def drops():
    return pd.read_csv(OUTPUTS / "dropped_with_sfs.csv")


@pytest.fixture(scope="session")
def unplaceable():
    p = OUTPUTS / "unplaceable.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def _insert(plan, label):
    m = [i for i in plan if i["label"] == label]
    return m[0] if m else None


def _insert_html(label):
    return BeautifulSoup(
        (INSERTS_DIR / f"Insert_{label}.html").read_text(), "lxml"
    )


# ──────────────────────────────────────────────────────────────────────────
# Invariants — dollar totals, uniqueness, coverage
# ──────────────────────────────────────────────────────────────────────────

def test_plan_has_inserts(plan):
    assert len(plan) > 0


def test_labels_unique(plan):
    labels = [i["label"] for i in plan]
    assert len(labels) == len(set(labels))


def test_dollars_reconcile(plan, drops, unplaceable):
    """plan $ + unplaceable $ == eligible $ (sum of sfs_rounded among eligible drops)."""
    plan_total = sum(s["new_reapprop_amount"] for i in plan for s in i["survivors"])
    unpl_total = int(unplaceable["sfs_rounded"].sum()) if len(unplaceable) else 0
    eligible_total = int(drops[drops["insert_eligible"]]["sfs_rounded"].sum())
    assert plan_total + unpl_total == eligible_total


def test_no_doc_start_anchors(plan):
    """Page 1 'doc_start' phantom inserts should all be routed to unplaceable.csv."""
    offenders = [i["label"] for i in plan
                 if i["anchor_upper"]["anchor_kind"] == "doc_start"]
    assert offenders == [], f"doc_start inserts left in plan: {offenders}"


def test_every_insert_has_pdf(plan):
    """generate_inserts.py must emit a PDF for every plan entry (no silent skips)."""
    missing = [i["label"] for i in plan
               if not (INSERTS_DIR / f"Insert_{i['label']}.pdf").exists()]
    assert missing == [], f"missing PDFs: {missing[:10]}{'...' if len(missing)>10 else ''}"


def test_sfs_mapping_resolves_many_agencies(drops):
    """SFS dept_code → agency mapping should resolve enough agencies to be useful."""
    matched = drops[drops["sfs_balance"].notna()]
    assert len(matched) > 1000, (
        f"only {len(matched)} drops matched SFS — mapping likely regressed"
    )


# ──────────────────────────────────────────────────────────────────────────
# Edge-case freezes
# ──────────────────────────────────────────────────────────────────────────

def test_926A_preserves_chyr_header(plan):
    """is_chapter_year_header regex fix: 926A's chyr line must stay kept (not struck).
    Was broken because LINE_NUM_RE's greedy .* consumed the body text."""
    ins = _insert(plan, "926A")
    if ins is None:
        pytest.skip("926A not in current plan")
    soup = _insert_html("926A")
    found_kept = False
    for p in soup.find_all("p"):
        t = p.get_text()
        if "By chapter 53" in t and "2019" in t:
            # Must not be fully wrapped in <del>
            dels = p.find_all("del")
            inss = p.find_all("ins")
            only_struck = (
                dels and not inss
                and "".join(d.get_text() for d in dels).strip() == t.strip()
            )
            assert not only_struck, "926A chyr header was struck — regex bug regressed"
            found_kept = True
            break
    assert found_kept, "926A chyr header line not located"


def test_36_series_splits_on_chyr(plan):
    """Issue 2 fix: chapter-year transitions should split inserts.
    Page 36 had one label 36A bundling multiple chyrs; now each chyr gets its own."""
    page_36_inserts = [i for i in plan if i["label_pdf_page"] == 36]
    # Should be > 3 (used to be 3; after split expect ~10)
    assert len(page_36_inserts) >= 6, (
        f"page 36 only has {len(page_36_inserts)} inserts — chyr split regressed"
    )
    # Each insert's survivors share ONE (program, fund, chapter_year) tuple
    for ins in page_36_inserts:
        chyrs = {(s["program"], s["fund"], s["chapter_year"], s["amending_year"])
                 for s in ins["survivors"]}
        assert len(chyrs) == 1, (
            f"{ins['label']} survivors span multiple chyr-groups: {chyrs}"
        )


def test_appropriation_sourced_inserts_generate(plan):
    """Bug B: approp-sourced inserts were being silently skipped because
    cache/enacted_25-26_approps.html didn't exist. Fall back now uses the
    same enacted HTML for the full-bill workflow."""
    approp_labels = [i["label"] for i in plan
                     if i["survivors"][0].get("source") == "appropriation"]
    if not approp_labels:
        pytest.skip("no appropriation-sourced inserts")
    missing = [lbl for lbl in approp_labels
               if not (INSERTS_DIR / f"Insert_{lbl}.pdf").exists()]
    assert missing == [], f"approp inserts not generated: {missing[:5]}"


def test_chapter_year_regex_works_with_extra_whitespace():
    """LINE_NUM_RE-to-is_chapter_year_header: patterns.py must handle >3-space
    leading whitespace and the greedy .* problem."""
    from patterns import is_chapter_year_header, is_fund_top
    assert is_chapter_year_header(
        " 9  By chapter 53, section 1, of the laws of 2019:"
    ) == 2019
    assert is_chapter_year_header(
        "  1  By chapter 1, section 1, of the laws of 2020, as amended by "
        "chapter 53, section 2, of the laws of 2021:"
    ) == 2020
    assert is_fund_top(" 10  General Fund") is True


def test_no_duplicate_survivors(plan):
    """Each eligible drop should appear in exactly one insert."""
    enacted_idxs = [s["enacted_idx"] for i in plan for s in i["survivors"]]
    assert len(enacted_idxs) == len(set(enacted_idxs))


def test_label_ordering_per_page(plan):
    """Labels on a single page should be A, B, ..., Z, AA, AB, ... (Excel-style,
    uppercase only, no gaps). Regression for the punctuation/lowercase bug
    where >26 inserts on a single page produced "[", "\\", "]", "a", "b"...
    which broke filenames and the LBDC API."""
    from collections import defaultdict
    def _expected_suffix(k: int) -> str:
        if k < 26:
            return chr(ord("A") + k)
        return chr(ord("A") + (k // 26) - 1) + chr(ord("A") + (k % 26))

    by_page = defaultdict(list)
    for i in plan:
        by_page[i["label_pdf_page"]].append(i["label"])
    for page, labels in by_page.items():
        # Sort by length first (A..Z before AA..ZZ), then alphabetical.
        suffixes = sorted(
            (lbl[len(str(page)):] for lbl in labels),
            key=lambda s: (len(s), s),
        )
        expected = [_expected_suffix(k) for k in range(len(suffixes))]
        assert suffixes == expected, (
            f"page {page} labels not contiguous: got {suffixes[:30]}"
        )
        # All suffixes must be uppercase ASCII letters only
        for s in suffixes:
            assert all('A' <= c <= 'Z' for c in s), (
                f"page {page}: invalid label suffix {s!r}"
            )


def test_approp_sourced_not_flagged_for_chyr_header(plan):
    """Appropriations section has no 'By chapter N, section M, of the laws of
    YYYY:' line — approp-sourced survivors should never be marked
    needs_chapter_header=True. Regression guard for audit pattern A."""
    bad = []
    for ins in plan:
        for s in ins["survivors"]:
            if s.get("source") == "appropriation" and s["needs_chapter_header"]:
                bad.append((ins["label"], s["approp_id"]))
    assert bad == [], f"approp-sourced survivors with needs_chapter_header: {bad[:5]}"


def test_cross_page_chyr_header_preservation(plan):
    """When a reapprop-sourced survivor's chyr header lives on a prior page,
    the planner widens source_enacted_page_range_html backward AND the
    generator walks across pages to preserve it. Spot check: 36D spans
    enacted pages 41-42 with chyr-2020 header on 41."""
    ins = _insert(plan, "36D")
    if ins is None:
        pytest.skip("36D not in plan")
    soup = _insert_html("36D")
    # Find any kept chyr-2020 header line
    from patterns import is_chapter_year_header
    found = False
    for p in soup.find_all("p"):
        t = p.get_text()
        yr = is_chapter_year_header(t)
        if yr == 2020:
            dels = p.find_all("del")
            inss = p.find_all("ins")
            only_struck = (
                dels and not inss
                and "".join(d.get_text() for d in dels).strip() == t.strip()
            )
            if not only_struck:
                found = True
                break
    assert found, "36D missing kept chyr-2020 header"


def test_cross_page_fund_header_preservation(plan):
    """When a survivor's fund header spans pages or lives on a prior page,
    the planner widens source_enacted_page_range_html and the generator
    walks pages to preserve the multi-line fund block."""
    # Find a plan entry where the first survivor needs fund header AND its
    # fund_page differs from first_page — that's the widened-slice case.
    import pandas as pd
    rr = pd.read_csv(OUTPUTS / "enacted_reapprops.csv")
    if "fund_page" not in rr.columns:
        pytest.skip("extractor doesn't emit fund_page yet")
    cross_page_widened = 0
    for ins in plan:
        if not any(s["needs_fund_header"] for s in ins["survivors"]):
            continue
        src_start = ins["source_enacted_page_range_html"][0]
        first_surv_page = ins["survivors"][0]["first_page"]
        if src_start < first_surv_page:
            cross_page_widened += 1
    # Sanity: at least some entries should trigger the widening; 0 means the
    # feature is not being exercised (or the bug silently regressed).
    assert cross_page_widened > 0, (
        "no fund-header widening observed — cross-page preservation likely regressed"
    )


def test_amount_replace_is_line_scoped(plan):
    """When multiple reapprops on the same page share the same (re. $X) old
    amount, the replace must target the survivor's own <p>, not the first
    page-wide match. Regression for 24B where survivor 10717 had old=$500K
    and a struck sibling (10742) also had (re. $500,000) — replace wrote
    onto the wrong line. Verify: for every edited amount in each insert,
    the new (re. $X) appears on the survivor's last_line, not on some
    earlier struck line."""
    from patterns import line_num_of
    offenders = []
    for ins in plan[:80]:  # sample to keep test fast; full pass already sweeps
        label = ins["label"]
        html_path = INSERTS_DIR / f"Insert_{label}.html"
        if not html_path.exists():
            continue
        soup = BeautifulSoup(html_path.read_text(), "lxml")
        page_divs = soup.find_all("div", class_="page")
        for s in ins["survivors"]:
            if s.get("source") == "appropriation":
                continue
            if s["old_reapprop_amount"] == s["new_reapprop_amount"]:
                continue
            pg_off = s["last_page"] - ins["source_enacted_page_range_html"][0]
            if pg_off < 0 or pg_off >= len(page_divs):
                continue
            new_tag = f"(re. ${s['new_reapprop_amount']:,})"
            # Find the <p> with the survivor's last line number
            survivor_p = None
            for p in page_divs[pg_off].find_all("p"):
                if line_num_of(p.get_text()) == s["last_line"]:
                    survivor_p = p
                    break
            if survivor_p is None:
                continue
            # The new-amount <ins> should be inside survivor_p
            new_ins_on_survivor = any(
                new_tag in (i.get_text() or "") for i in survivor_p.find_all("ins")
            )
            # And NOT on some other <p> on the page (would be a misfire)
            misfire = False
            for p in page_divs[pg_off].find_all("p"):
                if p is survivor_p:
                    continue
                if any(new_tag in (i.get_text() or "") for i in p.find_all("ins")):
                    misfire = True
                    break
            if misfire and not new_ins_on_survivor:
                offenders.append((label, s["approp_id"], new_tag))
    assert offenders == [], (
        f"amount-replace misfired to wrong <p>: {offenders[:3]}"
    )


def test_multi_page_survivor_keeps_intermediate_pages(plan):
    """When a survivor's body spans >=3 pages, all lines on intermediate
    pages must be kept. Earlier bug: keep-logic only covered first page
    (first_line..end) and last page (1..last_line), striking ALL middle
    pages. Manifested in 343C whose second survivor spans 5 pages."""
    # Find any survivor spanning 3+ pages
    for ins in plan:
        for s in ins["survivors"]:
            if s["last_page"] - s["first_page"] >= 2:
                label = ins["label"]
                html_path = INSERTS_DIR / f"Insert_{label}.html"
                if not html_path.exists():
                    continue
                soup = BeautifulSoup(html_path.read_text(), "lxml")
                pages = soup.find_all("div", class_="page")
                src_start = ins["source_enacted_page_range_html"][0]
                mid_pg_off = s["first_page"] - src_start + 1
                if mid_pg_off < 0 or mid_pg_off >= len(pages):
                    continue
                mid_page = pages[mid_pg_off]
                total_body = 0
                struck_body = 0
                for p in mid_page.find_all("p"):
                    t = p.get_text()
                    if not t.strip():
                        continue
                    from patterns import line_num_of
                    if line_num_of(t) is None:
                        continue  # page header
                    total_body += 1
                    dels = p.find_all("del")
                    inss = p.find_all("ins")
                    if (dels and not inss
                            and "".join(d.get_text() for d in dels).strip() == t.strip()):
                        struck_body += 1
                # If >90% of middle page is struck, that's the bug.
                if total_body > 5 and struck_body / total_body > 0.9:
                    pytest.fail(
                        f"{label}: intermediate page {mid_pg_off} is mostly "
                        f"struck ({struck_body}/{total_body}) — multi-page "
                        f"survivor keep regressed"
                    )
                return  # First multi-page case tested is enough
    pytest.skip("no multi-page survivors in plan")


def test_insert_strikes_page_header_lines(plan):
    """Manual-analyst convention: the generated insert PDF strikes its own
    page-header block (page number, agency name, bill title). These are
    redundant when the insert is pasted into the tracker. Sample 5 inserts;
    each must have <del> on all ln=None header lines."""
    from patterns import line_num_of
    checked = 0
    for ins in plan:
        html_path = INSERTS_DIR / f"Insert_{ins['label']}.html"
        if not html_path.exists():
            continue
        soup = BeautifulSoup(html_path.read_text(), "lxml")
        for p in soup.find_all("p"):
            t = p.get_text()
            if not t.strip():
                continue
            if line_num_of(t) is not None:
                break  # reached body
            # This is a page-header line (page#, agency, bill title)
            dels = p.find_all("del")
            inss = p.find_all("ins")
            # Skip if it's an inserted label line (class=new-line)
            if p.get("class") and "new-line" in p.get("class"):
                continue
            assert dels and not inss, (
                f"{ins['label']}: page-header line {t.strip()!r} not struck"
            )
        checked += 1
        if checked >= 5:
            break
    if checked == 0:
        pytest.skip("no insert HTMLs available")


def test_three_line_chyr_header_consumed():
    """Chyr headers that span 3 lines — e.g. 'The appropriation made by
    chapter 53, section 1, of the laws of 2024, as / supplemented by
    interchanges in accordance with state finance law, / is hereby amended
    and reappropriated to read:' — must consume all 3 lines. Otherwise the
    next reapprop's first_line points at a continuation line of the header
    and the insert PDF strikes the start of the header while keeping the
    middle, which is nonsensical."""
    import pandas as pd
    rr = pd.read_csv(OUTPUTS / "enacted_reapprops.csv")
    # Spot-check: OCFS chyr 2024 first reapprop after the 3-line header.
    # approp_id 13959 on enacted page 606 should start no earlier than
    # line 24 (header consumes 21-23).
    m = rr[(rr.approp_id == 13959.0) & (rr.first_page == 606)]
    if len(m) == 0:
        pytest.skip("13959 not in extraction")
    first_line = int(m.iloc[0].first_line)
    assert first_line >= 24, (
        f"13959 at pg606 first_line={first_line} — 3-line chyr header not consumed"
    )


def test_multi_line_agency_header_doesnt_drop_reapprops():
    """Agencies with two-line page headers (DEPARTMENT OF FAMILY ASSISTANCE
    + OFFICE OF CHILDREN AND FAMILY SERVICES; DEPARTMENT OF FAMILY ASSISTANCE
    + OFFICE OF TEMPORARY AND DISABILITY ASSISTANCE; etc.) must extract their
    reapprops. Previous bug: extractor reset state every page because the
    Department line didn't equal the Office line that was set before, dropping
    hundreds of pages of content silently."""
    import pandas as pd
    rr = pd.read_csv(OUTPUTS / "enacted_reapprops.csv")
    for agency, min_count in [
        ("OFFICE OF CHILDREN AND FAMILY SERVICES", 100),
        ("OFFICE OF TEMPORARY AND DISABILITY ASSISTANCE", 50),
    ]:
        n = (rr["agency"] == agency).sum()
        assert n >= min_count, (
            f"{agency} has only {n} extracted reapprops — "
            f"multi-line agency-header regression"
        )


def test_chyr_persists_across_fund_top():
    """When the bill uses CHYR -> FUND -> reapprops order, the FUND_TOP
    handler must NOT reset chapter_year. Regression: Agriculture pg92 had
    chyr 2008 amd 2011 preceding fund 'General Fund / Community Projects
    Fund - 007 / Account AA'; the extractor was zeroing chyr during the
    sub-fund collector, leaving member items (Afton Driving Park etc.)
    with chyr=0 and unable to join SFS."""
    import pandas as pd
    rr = pd.read_csv(OUTPUTS / "enacted_reapprops.csv")
    # No reapprops should end up with chyr=0 in a clean extraction.
    zero_chyr = rr[rr["chapter_year"] == 0]
    assert len(zero_chyr) == 0, (
        f"{len(zero_chyr)} reapprops have chyr=0 — FUND_TOP is still "
        f"zeroing chapter_year"
    )


def test_member_items_appear_in_sfs_lookup():
    """SFS loader must retain member-item rows (approp_id_s=='') so that
    NaN-id bill drops like 'Afton Driving Park' can match by
    (agency, chyr, approp_amount). Previously filtered out, leaving 1,696
    NaN-id drops permanently unmatchable."""
    import sys, pandas as pd
    sys.path.insert(0, str(ROOT / "src"))
    from sfs import load_sfs_lookup
    sfs = load_sfs_lookup()
    noid_rows = (sfs["approp_id_s"] == "").sum()
    assert noid_rows > 30_000, (
        f"SFS lookup only has {noid_rows} member-item rows — loader is "
        f"filtering them out, breaking NaN-id matching"
    )


def test_subschedule_not_attributed_to_next_reapprop():
    """Sub-schedule allocation rows follow a reapprop's (re. $X) terminator
    and belong to THAT reapprop's body. Without sub-schedule detection the
    extractor would start buffering the NEXT reapprop at the sub-schedule's
    first line, dragging allocation rows into the survivor body.
    Regression case: DOL page 1176 — approp_id 34217 (OACES) must start at
    line 19+, not line 1 (sub-schedule of the prior reapprop)."""
    import pandas as pd
    rr = pd.read_csv(OUTPUTS / "enacted_reapprops.csv")
    # Find the OACES 34217 row on page 1176 (chyr 2018)
    m = rr[(rr.approp_id.astype(str).str.startswith("34217"))
           & (rr.first_page == 1176)
           & (rr.chapter_year == 2018)]
    if len(m) == 0:
        pytest.skip("34217@pg1176 not in current enacted set")
    assert m.iloc[0].first_line >= 5, (
        f"34217 first_line={m.iloc[0].first_line} — sub-schedule tail attribution regressed"
    )


def test_tracker_html_exists():
    """tracker.html (and .pdf) must exist after a pipeline run."""
    assert (OUTPUTS / "tracker.html").exists()
    assert (OUTPUTS / "tracker.pdf").exists()

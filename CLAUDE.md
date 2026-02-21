# CLAUDE.md — NYS Budget Reappropriation Analyzer

## Project Overview

Automated comparison tool for New York State budget documents. Compares enacted 25-26 budget PDFs against the 26-27 executive budget (30-day amendments) to identify discontinued spending authority — appropriations and reappropriations not carried forward.

The analysis output powers a manual legislative review workflow where analysts create "inserts" (reappropriation language to add back into the executive budget) using the LBDC PDF Editor, then sign off with the Senate before submission.

## How to Run

```bash
pip install pdfplumber pandas PyMuPDF
python nys_budget_analyzer.py <enacted_pdf> <executive_pdf> --output-dir <output_dir>
```

PDFs are gitignored (too large). They live locally at:
- `Reapprops 26-27/ATL 25-26.pdf` — Enacted ATL budget (SFY 2025-26)
- `Reapprops 26-27/ATL 26-27 Executive.pdf` — **30-day amendments** (S.9003-A/A.10003-A, 1,304 pages)

## Current State

**v8 output is current.** Generated from 30-day amendments PDF. Fund/account page-boundary bug has been fixed (inline tracking instead of per-page pre-scan). Output lives at `Reapprops 26-27/output_atl_v8/`.

Expected output files:

| File | What It Is |
|------|-----------|
| `discontinued_all.csv` | All discontinued items — the primary working list |
| `enacted_budget_data.csv` | Full extracted enacted records with bill_language |
| `executive_budget_data.csv` | Full extracted executive records |
| `discontinued_insertion_locations.csv` | Estimated exec page/line for each discontinued item |
| `analysis_summary.json` | Summary statistics by agency |

## Active Workflow: Page-by-Page Manual Review

The primary workflow is a **page-by-page manual review** of enacted budget pages against the executive reappropriations. One enacted page at a time:

1. Show all extracted items on the page with their status (continued/discontinued/insert candidate/skip)
2. Show the **raw enacted page text** so the user can spot anything the extractor missed
3. Show the corresponding **executive page** for insert placement context
4. Flag items without approp IDs that the extractor missed
5. User makes inserts in LBDC PDF Editor, says "next" to advance

### Education ATL Review Status

- **Scope**: Education Department ATL only (Capital deferred)
- **No 5-year filter** — all chapter years included
- **Formula aid exclusions**: GSPS, apportionment, transportation aid, school aid, elementary/secondary block grants
- **Education enacted pages**: ~264-450 in ATL 25-26.pdf (new appropriations ~264-314, reappropriations ~316-450)
- **Status**: Page-by-page walkthrough in progress with 30-day amendments as executive baseline.

### Insert Rules (IMPORTANT — persist across sessions)

An **insert** is a small piece of reappropriation language (1-3 consecutive clauses) added back into the executive budget at a specific location. Key rules:

1. **Insert naming**: `INSERT {exec_page}{letter}` — e.g., INSERT 268A. Multiple inserts on the same exec page get sequential letters (A, B, C...).

2. **What gets an insert**: Discontinued items (dropped from enacted, not in exec). For ChYr 2025 items, only if SFS undisbursed balance > $0. For older ChYrs, all items get inserts regardless (no 5-year filter this year).

3. **Placement**: Each insert goes in the **same program** in the executive that the item was under in the enacted, ordered by fund/account, then chapter year (newest first). Items are placed **between their enacted neighbors** that exist in the exec.

4. **Hierarchical context**: If any part of the dropped item's hierarchical chain is **not present in the exec** (fund type, fund name, account name, or even the entire program), that hierarchy must be **included in the insert** (not struck through). The insert adds the missing structural headers so the item has proper context in the exec. For example, if "Love Your Library Account - 22119" doesn't exist in exec, the insert includes the full fund/account header lines above the appropriation line.

5. **Sequential placement for missing sections**: A new fund/account section goes in the same sequential position relative to its neighbors in the enacted. E.g., Love Your Library (SRF-Other) goes just above the next SRF-Other section (LGRMM 20501) in the Cultural Education program area.

6. **Exclusions**:
   - STAR items (21709, 23494): Skip — executive programs, don't reinsert
   - Formula aid items: Include as inserts (can always remove later)

7. **Insert granularity**: Each insert is typically 1 item or a few consecutive items from the same enacted section. NOT large batches. If 3 consecutive items on the same enacted page are all dropped, they can be one insert. Non-consecutive items or items from different sections are separate inserts.

8. **What the user needs**: Just the insert name (e.g., "INSERT 273A") and which items go in it. User looks up the bill language themselves in the LBDC PDF Editor.

### Executive Program Structure (Education ATL Reappropriations)

```
Adult Career & Continuing Ed: exec pages 268-271
  GF / Local Assistance 10000: pg 268-269 (ChYr 2025→2022)
  SRF-Federal / Fed Dept of Ed 25210: pg 270 (ChYr 2025→2023)
  SRF-Other / Vocational Rehab 23051: pg 271 lines 1-24 (ChYr 2025→2021)
Cultural Education: exec pages 271-274
  GF / Local Assistance: pg 271 ln25 - 272
  SRF-Federal / Fed Operating Grants 25456: pg 272
  SRF-Other / LGRMM 20501: pg 273
Higher Education: exec pages 274-281
  GF / Local Assistance: pg 274-280
  SRF-Federal / Fed Dept of Ed 25210: pg 281 lines 1-43
PreK-12 Education: exec pages 281-352
  SRF-Federal / Fed Dept of Ed 25210: pg 281-345
  general fund / Fed Dept of Ed 25210: pg 334-350
  SRF-Federal / Fed HHS 25122: pg 351
  SRF-Federal / Fed Operating Grants 25456: pg 352
```

### Walkthrough Progress

**Next page to process: 275** (pages 272-274 have no Education items or no discontinued)

Completed pages and inserts:

| Enacted Pg | Insert | Items | Exec Pg | Notes |
|-----------|--------|-------|---------|-------|
| 264 | INSERT 268A | 23462 ($750K) | 268 | Between 21856 (ln9) and 21854 (ln11) |
| 265 | INSERT 268B | 56145 ($500K) | 268 | After 23410 (ln22); both are GF/LA ChYr 2025 same section as 268A |
| 266 | — | 21847 ($1.7M) | — | $0 SFS, skip. Bare-dollar items: public radio 57044 $0 SFS skip; Brooklyn PL 57045 $0 SFS skip |
| 266→267 | INSERT 272A (TBD) | 57047 social work in libraries ($150K) | ~272 | Bare-dollar item, SFS $150K. Cultural Ed GF/LA section |
| 267 | INSERT 273A (TBD) | 23373 Love Your Library ($100K) | ~273 | New fund/account header needed (Love Your Library 22119 not in exec). Goes above LGRMM 20501 |
| 268 | INSERT 274A | 21831 ($16.3M, SFS $5.7M) | 274 | After 21830 (ln11), before 21832 (ln17). 21842 also dropped but $0 SFS |
| 269 | — | 21843, 23437, 21836, 21837 | — | All $0 SFS, skip. St. Bonaventure 57048 (bare-dollar) is continued in exec |
| 270 | INSERT 275A | 23379 ($350K) + 23380 ($750K) + 23382 ($200K) | 275 | 3 consecutive items after 23344 (ln10). ATL Drops said $0 but SFS confirms full undisbursed |
| 271 | INSERT 281A (TBD) | 21744 ($5.2M, SFS $5.2M) | ~281 | Office of Mgmt Services Program — entire program not in exec reapprops. Full hierarchy needed. Goes between Higher Ed and PreK-12 |

### SFS Lookup Methodology (IMPORTANT — persist across sessions)

**Two SFS data sources** — always check BOTH since ATL Drops can be stale:

1. **ATL Drops** (`Reapprops 26-27/BPS to v7 check/ATL Drops.xlsx`): Quick lookup by composite_key. Column `SFS Undisbursed Funds` (strip trailing space). Only has items with approp IDs.

2. **SFS All Education** (`~/Downloads/SFS, All Education.xlsx`, Sheet1, header=row 6): Full SFS data with 7,748 rows. Key columns:
   - `Budgetary Program` — contains approp ID and name (e.g., "57044 - Public Radio Stations")
   - `Budgetary Budget Reference` — fiscal year reference (e.g., "A202526" = ChYr 2025)
   - Column index 20 = `Undisbursed Approp Balance` (rename from `Unnamed: 20`)
   - Financial columns need renaming: indices 10-24 map to: Original Approp Amount, Current Appropriation, Unreserved, Reserved, Pre-Encumbrances, Encumbrances, LTD KK Expenditures, Remaining Unreserved Balance, LTD Modified Accrual, LTD Cash Ledger Disbursements, Undisbursed Approp Balance, MTD KK Expenditures, MTD Cash Ledger Disbursements, YTD KK Expenditures, YTD Cash Ledger Disbursements

3. **For bare-dollar items (no approp ID)**: Search SFS by program name keywords or by amount + program level. The SFS has the approp ID in the `Budgetary Program` column even when the enacted text doesn't show it.

**CRITICAL**: ATL Drops showed $0 for 23379/23380/23382 but SFS showed full undisbursed balances. Always verify with SFS when ATL Drops says $0 for items you'd expect to have funds.

### Per-Page Walkthrough Process

For each enacted page:
1. Show extracted items with status (continued/dropped) and SFS balance
2. Read raw enacted page text to spot bare-dollar items the extractor missed
3. For bare-dollar items: search exec by bill_language keywords; if not in exec, search SFS by program name/amount to get undisbursed balance
4. For items needing inserts: identify exec page placement using enacted neighbor mapping
5. Assign insert name and note which items go in it
6. User says "next" to advance

### Last Year's Inserts (Reference)

Located at `~/Downloads/ATL/` — 46 insert PDFs from last year's cycle. Shows:
- Blue strikethrough = existing exec text (context)
- "Insert XXXN" label marks where new text goes
- Each insert is 1-3 reappropriation clauses at a specific spot
- Multiple inserts per exec page get separate letters (293B, 293C, 293D, etc.)
- Full annotated exec section: `SED ATL 2-HOUSE Reapprops-lbdc_pdf_editor-2025-02-26-1740.pdf`

## Priority Task: Older-Than-5-Years Tracking (NEW THIS YEAR)

**Boss has prioritized this.** Per the reference guide Step 2c:

- Review reappropriations from **SFY 21-22 and earlier** (chapter_year <= 2021) in ATL and Capital budgets
- This is a **tracking exercise only** — no inserts
- Use SFS to check undisbursed balances
- For any with a balance, record: chapter year, program name, initial amount, current (undisbursed) amount
- Compile into a spreadsheet for the deputy

This applies to **all agencies**, not just Education.

## Known Extractor Issues

### Items without approp IDs (not captured)

The extractor only captures items with `(XXXXX)` five-digit appropriation IDs. Items formatted as bare dollar amounts without parenthesized IDs are missed. Known Education examples:

| Page | Item | Amount |
|------|------|--------|
| 265 | Rockland Independent Living Center / BRIDGES | $50,000 |
| 266 | Public radio stations | $4,000,000 |
| 266 | Brooklyn Public Library Center for Brooklyn History | $100,000 |
| 269 | St. Bonaventure University HEOP | $2,490,000 |
| 306 | Vocational Education & Extension Board of Suffolk County | $150,000 |
| 306 | Literacy, Inc | $50,000 |
| 306 | Multicultural High School / JROTC Academy | $270,000 |

### Extractor bug — items with valid IDs missed

| Page | Approp ID | Amount | Notes |
|------|-----------|--------|-------|
| 297 | 55909 | $19,700,000 | Has valid ID but not extracted |
| 297 | 55933 | $1,500,000 | Has valid ID but not extracted |

These need investigation in `nys_budget_analyzer.py` extraction logic.

## Key Reference Data

- **Last year's inserts**: `25-26 ATL Reapprop Inserts and Sections/` (gitignored, local only) — ~46 insert PDFs, 157 unique approp IDs, all Education
- **BPS adds**: `Reapprops 26-27/BPS to v7 check/AA ATL Adds.xlsx` — legislative adds from BPS system
- **ATL Drops with SFS**: `Reapprops 26-27/BPS to v7 check/ATL Drops.xlsx` — discontinued items with SFS undisbursed balances
- **STAR items** (23494, 21709): NOT in last year's inserts — executive programs, don't reinsert

## Repo Structure

```
nys_budget_analyzer.py          # Main analysis engine (extraction, 4-pass comparison, reporting)
bps_adds_matcher.py             # BPS legislative adds matcher (LLM-assisted, Anthropic API)
highlight_discontinued.py       # PDF highlighting tool for manual review
requirements.txt                # Python dependencies
README.md                       # Full docs + reappropriations reference guide
CLAUDE.md                       # This file — Claude Code session context
Reapprops 26-27/
  BPS to v7 check/              # BPS matching data + ATL Drops with SFS
```

## Pending Work

1. **Continue page-by-page Education ATL review** — next page: 275 (pages 272-274 need checking for items)
2. **Older-than-5-years tracking** (boss priority) — filter for chapter_year <= 2021, match SFS balances, compile spreadsheet for deputy
3. **Investigate extractor bug** on page 297 (55909/55933 have valid IDs but weren't captured)
4. **Capital re-run** (deferred until ATL complete)

# CLAUDE.md — NYS Budget Reappropriation Analyzer

## Project Overview

Automated comparison tool for New York State budget documents. Compares enacted 25-26 budget PDFs against executive 26-27 budget proposals to identify discontinued spending authority — appropriations and reappropriations not carried forward.

The analysis output powers a manual legislative review workflow where analysts create "inserts" (reappropriation language to add back into the executive budget) using the LBDC PDF Editor, then sign off with the Senate before submission.

## How to Run

```bash
pip install pdfplumber pandas PyMuPDF
python nys_budget_analyzer.py <enacted_pdf> <executive_pdf> --output-dir <output_dir>
```

PDFs are gitignored (too large). They live locally at:
- `Reapprops 26-27/ATL 25-26.pdf` — Enacted ATL budget
- `Reapprops 26-27/ATL 26-27 Executive.pdf` — Executive ATL proposal
- `Reapprops 26-27/STOPS 25-26.pdf` / `STOPS 26-27 Executive.pdf` — State Operations
- `Reapprops 26-27/CAP 25-26.pdf` / `CAP 26-27 Executive.pdf` — Capital

## Current v7 Output

All output is in `Reapprops 26-27/output_{atl,capital,stateops}_v7/`. Key files:

| File | What It Is |
|------|-----------|
| `discontinued_all.csv` | All discontinued items — the primary working list |
| `enacted_budget_data.csv` | Full extracted enacted records with bill_language |
| `executive_budget_data.csv` | Full extracted executive records |
| `discontinued_insertion_locations.csv` | Estimated exec page/line for each discontinued item |
| `education_adds_workbook.xlsx` | Consolidated Education ATL workbook (400 items) |
| `education_chyr2025_tracking.xlsx` | ChYr 2025 tracking sheet with SFS balances (95 items) |
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
- **378 actionable items** (400 discontinued - 22 formula aids excluded)
- **Formula aid exclusions**: GSPS, apportionment, transportation aid, school aid, elementary/secondary block grants
- **Education enacted pages**: ~264-314 in ATL 25-26.pdf

### Pages Completed

| Enacted Page | Exec Page | Inserts | Manual Flags |
|-------------|-----------|---------|-------------|
| 264 | 229 | Insert 229A: 23462 (independent living centers, $750K) | None |
| 265 | 230 | Insert 230A: 56145 (adult literacy, $500K) | BRIDGES $50K (no approp ID) |

**Next page to review: 266**

### ChYr 2025 Insert Summary

48 items with SFS undisbursed balance >= $1K need inserts (from `education_chyr2025_tracking.xlsx`). 44 are fully spent ($0 SFS). 1 under $1K. 2 missing SFS data.

SFS balance data comes from `Reapprops 26-27/BPS to v7 check/ATL Drops.xlsx` (note: column name has trailing space: `'SFS Undisbursed Funds '` — strip it).

## Priority Task: Older-Than-5-Years Tracking (NEW THIS YEAR)

**Boss has prioritized this.** Per the reference guide Step 2c:

- Review reappropriations from **SFY 21-22 and earlier** (chapter_year <= 2021) in ATL and Capital budgets
- This is a **tracking exercise only** — no inserts
- Use SFS to check undisbursed balances
- For any with a balance, record: chapter year, program name, initial amount, current (undisbursed) amount
- Compile into a spreadsheet for the deputy

This applies to **all agencies**, not just Education. The v7 `discontinued_all.csv` and `enacted_budget_data.csv` files contain the chapter_year data needed to filter for <= 2021 items. SFS balances in the ATL Drops file can be matched.

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
  output_atl_v7/                # ATL analysis output (current)
  output_capital_v7/            # Capital analysis output (current)
  output_stateops_v7/           # State Ops analysis output (current)
  BPS to v7 check/              # BPS matching data + ATL Drops with SFS
```

## Pending Work

1. **Continue page-by-page Education ATL review** from page 266 through ~314
2. **Older-than-5-years tracking** (priority) — filter for chapter_year <= 2021, match SFS balances, compile spreadsheet
3. **Capital re-run** with bill_language + insertion locations (deferred until ATL complete)
4. **Investigate extractor bug** on page 297 (55909/55933 have valid IDs but weren't captured)
5. **Other agencies** — Education is first, others follow same workflow

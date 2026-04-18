# REAPPROPS v9 — NYS Budget Reappropriation Insert Pipeline

Clean-slate rebuild targeting the actual deliverables:

1. **Insert PDFs** — per label, the original 25-26 enacted page(s) re-rendered
   via LBDC with everything struck except survivor appropriations (the ones
   with SFS undisbursed balance ≥ $1,000, rounded up). Each insert gets a
   label like `272A` — the 26-27 exec page it targets + letter for ordering.
2. **Tracker PDF** — the 26-27 exec reappropriation section with tracked
   `Insert NNNA` markers inserted inline at each insert's anchor position
   (matched by program → fund → chapter_year → predecessor/successor approp).

Scope: **Education ATL only.** Capital, StateOps, and older-than-5-years
tracking are out of scope.

The pipeline processes TWO enacted sources:
- 25-26 **reapprops** section (pp 315-450) — carry-overs from prior chapter years
- 25-26 **appropriations** section (pp 264-314) — new ChYr 2025 spending

A drop is any item in either section that isn't reappropriated in the 26-27
exec. Appropriation-sourced drops produce inserts whose source text is the
appropriation line with ` ... (re. $SFS_ROUNDED)` appended as a tracked
insertion, converting it into reapprop form.

## Inputs (symlinked to inputs/)

| Symlink | Source | Role |
|---|---|---|
| `enacted_25-26.pdf` | `organized_projects/REAPPROPS/25-26 ATL Reapprops ... pg315-450...pdf` | 25-26 enacted reappropriations (Education) |
| `executive_26-27.pdf` | `organized_projects/REAPPROPS/26-27 ATL Reapprops ... pg268-352...pdf` | 26-27 executive A-print reappropriations (Education) |
| `budget_breakdown.xlsx` | `organized_projects/REAPPROPS/BUDGET BREAKDOWN.xlsx` | Ground-truth hierarchy: program × fund × year → start page/line + reapprop count |
| `atl_drops_sfs.xlsx` | `nys-budget-reappropriation-analyzer/Reapprops 26-27/BPS to v7 check/ATL Drops.xlsx` | SFS undisbursed balances for dropped items |

## Ground truth (from BUDGET BREAKDOWN Sheet1)

- 2025 enacted: **592** reapprops across 4 programs, 16 fund-combos (pp 315-450)
- 2026 exec: **362** reapprops across 4 programs, 14 fund-combos (pp 268-352)
- One fund entirely dropped in 2026 (2025's `Teen Health Education Account - 20200`)

Totals-by-program and per-fund counts in Sheet1 are the validation target for
the extractor. If the extractor's counts don't match, the extractor is wrong.

## Pipeline design

```
inputs/*.pdf
   │
   ▼  src/upload_and_cache.py              (LBDC API — run once)
cache/*.html
   │
   ▼  src/extract.py                       (regex + structural parse)
outputs/enacted_reapprops.csv + executive_reapprops.csv
   │
   ▼  src/compare.py                       (match on composite key)
outputs/comparison.csv  (continued | modified | dropped | new_in_exec)
   │
   ▼  src/sfs.py                           (join ATL Drops.xlsx)
outputs/dropped_with_sfs.csv  (eligible = sfs >= $1K, rounded up)
   │
   ▼  src/insert_plan.py                   (group survivors, assign labels)
outputs/insert_plan.json
   │
   ├─▶  src/generate_inserts.py            (LBDC API — per insert)
   │     outputs/inserts/Insert_{label}.pdf
   │
   └─▶  src/generate_tracker.py            (LBDC API — one tracker)
         outputs/tracker.pdf
```

## Extractor rules (to implement)

Anchor: `.. ` (any 1-47 dots) followed by an amount.

Omit these false positives:
- Agency header schedule (`General Fund ....................... 34,967,122,850  3,051,325,000`)
- Program headers (`ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM ..... 229,925,000`)
- Subtotal lines (`Program account subtotal .................. 98,596,000`)

Structural context (tracked while walking the doc):
- Program = all-caps string ending in `PROGRAM`
- Fund / account = lines before `By chapter 53...:` block; resets chapter year
- Chapter year = `By chapter <N>, section <N>, of the laws of YYYY` with optional
  `as amended by chapter <M>, section <M>, of the laws of YYYY`. Chapter/section
  numbers vary — extract only enacted year + amending year.
- Blank line between chapter-year blocks within the same fund.
- 2025 chapter-year block (if present) always first inside a fund.

Each reapprop: `(program, fund, approp_id, chapter_year, amending_year,
 reapprop_amount, orig_amount, bill_language, page, line_start, line_end)`.

## Insert-plan rules

For each dropped reapprop where SFS undisbursed ≥ $1,000:
- `sfs_rounded = ceil(sfs / 1000) * 1000`

Group into labels by walking 25-26 in document order:
- **Upper anchor**: previous continued-reapprop OR chapter-year header OR fund
  header OR program header — whichever comes first above a survivor.
- **Lower anchor**: next such marker below. *Accumulate* survivors until the
  lower anchor; non-survivor drops (SFS < $1K) inside the run stay as struck
  text in the insert, not as separate labels. Non-survivor *continued* items
  between two survivors DO split into separate labels.
- **Chapter-year-dropped edge case**: if the exec has no block for this
  `(program, fund, chapter_year)`, the chapter-year header must be INCLUDED
  in the insert (else the reapprop loses its legal context).
- Labels: `{exec_page}{letter}` — letter by order on that exec page.

Tracker: for each insert, tracked-insert `Insert {label}` as a new line
immediately after the upper-anchor line in the exec HTML.

## Status

- [x] Workspace scaffolded
- [x] Inputs symlinked
- [x] LBDC client/document copied verbatim (verified code)
- [x] HTML cache populated (`cache/*.html`, 607K + 377K chars)
- [x] Extractor — 592/362 reapprops, 100% match on all 30 (program, fund, year) cells
- [x] Extractor validation vs BUDGET BREAKDOWN
- [x] Comparator — 160 continued, 114 modified, 318 dropped, 88 new_in_exec
- [x] SFS join — 306/318 matched (96%); 248 eligible for insert (SFS ≥ $1K)
- [x] Insert plan — 124 labels, 297 survivors, $5,254,008,000 to re-add
      (22 appropriation-sourced + 102 reapprop-sourced). Runtime invariants
      enforce unique labels, dollar reconciliation, and 1:1 survivor-to-
      plan coverage; any regression crashes the plan build.
- [x] Insert PDFs — 124 signed PDFs in `outputs/inserts/Insert_*.pdf`
      (appropriation-sourced inserts append ` ... (re. $X)` as tracked
      insertion after the source amount to convert approp → reapprop form.
      Page header lines preserved as-is, not struck.)
- [x] Tracker PDF — `outputs/tracker.pdf` (85 pages, 124 tracked labels inline)
- [x] Match vs manual sign-off — **92/102 labels overlap (90%)**. Remaining
      10% are grouping judgment calls (user confirmed manual grouping was
      loose). 32 v9-only labels; most are federal reapprops the manual
      ignored by policy. See `outputs/label_diff.csv`.

## How to re-run end-to-end

```bash
cd /Users/samscott/Desktop/REAPPROPS_v9
source venv/bin/activate
python src/upload_and_cache.py    # 3 LBDC uploads, cached locally
python src/extract.py             # reapprops (enacted + exec) -> CSVs
python src/extract_approps.py     # 25-26 appropriations -> CSV
python tests/validate_counts.py   # reapprop count sanity vs BUDGET BREAKDOWN
python src/compare.py             # unified enacted vs exec -> comparison.csv
python src/sfs.py                 # -> dropped_with_sfs.csv (SFS ≥ $1K rounded)
python src/insert_plan.py         # -> insert_plan.json (124 labels)
python src/generate_inserts.py    # -> outputs/inserts/Insert_*.pdf
python src/generate_tracker.py    # -> outputs/tracker.pdf
```

## Residual limitations

- **27 drops without SFS match** (~24 are NaN-approp-id items where the bill
  text lists a bare dollar amount without a parenthesized ID — a known
  extractor limitation per CLAUDE.md). See `outputs/dropped_with_sfs.csv`
  where `sfs_balance` is empty.
- **4 ID'd drops with no SFS data** (21701 STAR $20.4B chyr 2025, 21771 PK
  $500K chyr 2025, 57024 PK $50K chyr 2024, and one other): ATL Drops.xlsx
  has these rows but the SFS column is blank. This is a data-source gap,
  not a pipeline bug. Manually acquire SFS figures if needed.
- **Formula-aid exclusions not applied** (GSPS, apportionment, school aid,
  etc. per CLAUDE.md). v9 emits inserts for all eligible drops; filter
  manually at review time or add a skip list to `insert_plan.py`.
- **Page-header cosmetic**: page number / agency / bill-title lines are
  preserved (not struck) in insert PDFs per user rule; visually differs
  from last year's manually-produced inserts where they were struck.

## Audit trail

A substantial code + data audit was performed with the following findings
addressed:

- Consolidated regex patterns into `src/patterns.py` (no more drift between
  extract.py / extract_approps.py / generate_inserts.py).
- Fixed fund-name wrap detection in both extractors — PDF-wrapped fund names
  like "New York State Local Government Records Management Improvement Fund"
  were being split across multiple fund parts.
- Derived PDF page offsets from input filenames (no hardcoded magic numbers).
- Tightened NaN-ID text-similarity matching: restrict candidates to same
  (program, fund); fall back to cross-context only with a higher threshold.
- Runtime invariants in `insert_plan.py`: unique labels, dollar total equals
  SFS-eligible total, every eligible drop appears in exactly one insert.
- Hard-assert SOURCE_ORDER has entries for all source values.
- Refined `before_next_fund` anchor placement to use exact fund-family line
  positions from exec HTML rather than an approximation.

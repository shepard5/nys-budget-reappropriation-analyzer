# NYS Budget Reappropriation Analyzer

Automated comparison tool for New York State budget documents. Compares enacted budget PDFs against executive budget proposals to identify discontinued spending authority -- appropriations and reappropriations that were not carried forward.

Built for use in live state legislative budget analysis.

## What It Does

The NYS budget process involves enacted appropriations that may be reappropriated in subsequent executive budget proposals. When an enacted item does **not** appear in the executive budget, it represents **discontinued spending authority** -- funding the executive has chosen not to carry forward.

This tool automates that comparison across hundreds of pages of budget PDFs, producing structured CSV output of all discontinued items with full provenance (agency, appropriation ID, chapter citation, amounts, page numbers, and source text).

## Supported Budget Types

- **State Operations (STOPS)** -- agency operating budgets with budgetary sub-account filtering
- **Aid to Localities (ATL)** -- grants and aid programs
- **Capital Projects (CAP)** -- infrastructure and capital spending

## Quick Start

### Dependencies

```bash
pip install pdfplumber pandas PyMuPDF
```

### Run Analysis

```bash
python nys_budget_analyzer.py <enacted_pdf> <executive_pdf> --output-dir <output_directory>
```

**Example -- State Operations:**
```bash
python nys_budget_analyzer.py \
  "Reapprops 26-27/STOPS 25-26.pdf" \
  "Reapprops 26-27/STOPS 26-27 Executive.pdf" \
  --output-dir "Reapprops 26-27/output_stateops"
```

**Example -- Aid to Localities:**
```bash
python nys_budget_analyzer.py \
  "Reapprops 26-27/ATL 25-26.pdf" \
  "Reapprops 26-27/ATL 26-27 Executive.pdf" \
  --output-dir "Reapprops 26-27/output_atl"
```

### Highlight Discontinued Items in PDF

```bash
python highlight_discontinued.py <enacted_pdf> <discontinued_csv> <output_pdf> --type all
```

Produces a highlighted copy of the enacted PDF with yellow (reappropriations) and orange (appropriations) annotations on every discontinued item for manual review.

## Output Files

| File | Description |
|------|-------------|
| `discontinued_all.csv` | All discontinued items (appropriations + reappropriations) |
| `discontinued_reappropriations.csv` | Discontinued reappropriations only |
| `discontinued_appropriations.csv` | Discontinued appropriations only |
| `enacted_budget_data.csv` | Full extracted enacted budget records |
| `executive_budget_data.csv` | Full extracted executive budget records |
| `analysis_summary.json` | Summary statistics by agency with dollar amounts |
| `verification_report.txt` | Extraction and comparison verification details |

## Methodology

### Record Extraction

Budget records are extracted from PDF text using pattern matching on NYS bill language structure:

- **Reappropriations** identified by `(re. $X,XXX)` markers with chapter citations (`By chapter X, section Y, of the laws of YYYY`)
- **Appropriations** identified by `(XXXXX)` five-digit appropriation IDs with associated dollar amounts
- **Cross-page continuity** -- text buffers persist across page boundaries so records split between pages are captured correctly
- **Budgetary sub-line filtering** (State Operations only) -- expenditure category codes (Personal Service 50xxx, Contractual 51xxx, Travel 54xxx, Equipment 56xxx, etc.) are separated from real appropriation IDs to prevent false positives

### Comparison Logic

Each enacted record is matched against executive records using a **two-pass composite key approach**:

1. **Pass 1 (Exact match):** `agency | appropriation_id | chapter_year | appropriation_amount` -- matches records with identical identifying fields
2. **Pass 2 (Relaxed match):** `agency | appropriation_id | appropriation_amount` -- drops `chapter_year` to catch appropriation records where the fiscal year prefix differs between enacted and executive budgets

Records matched in either pass are classified as **continued** (same reappropriation amount) or **modified** (different amount). Unmatched enacted records are classified as **discontinued**.

### Composite Key Design

The matching key intentionally includes `appropriation_amount` because the same `appropriation_id` can appear multiple times within a single budget (different chapter years, different line items). Including the amount disambiguates these distinct records and prevents false matches that would hide genuine discontinuations.

## Validation

The tool has been validated against manual PDF cross-referencing:

- **State Operations:** 1,244 enacted records extracted, 193 discontinued identified across 21 agencies ($1.15B). 10/10 spot-checked items confirmed correct.
- **Aid to Localities:** 6,698 enacted records, 1,121 discontinued across 20 agencies ($116.5B). Output verified stable across multiple runs with zero false negatives.

## Project Structure

```
nys_budget_analyzer.py      # Main analysis engine (extraction, comparison, reporting)
highlight_discontinued.py   # PDF highlighting tool for manual review
requirements.txt            # Python dependencies
```

## Requirements

- Python 3.8+
- pdfplumber (PDF text extraction)
- pandas (CSV output and data handling)
- PyMuPDF / fitz (PDF highlighting)

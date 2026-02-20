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
  --output-dir "Reapprops 26-27/output_stateops_v7"
```

**Example -- Aid to Localities:**
```bash
python nys_budget_analyzer.py \
  "Reapprops 26-27/ATL 25-26.pdf" \
  "Reapprops 26-27/ATL 26-27 Executive.pdf" \
  --output-dir "Reapprops 26-27/output_atl_v7"
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
| `likely_reorganized.csv` | Fuzzy text matches (needs manual review) |
| `missing_id_records.csv` | Extracted records with no appropriation ID |
| `enacted_budget_data.csv` | Full extracted enacted budget records with bill language |
| `executive_budget_data.csv` | Full extracted executive budget records |
| `analysis_summary.json` | Summary statistics by agency with dollar amounts |
| `verification_report.txt` | Extraction and comparison verification details |
| `reconstruction_validation.txt` | Round-trip reconstruction coverage report |
| `reconstruction_mismatches.csv` | Items missing from or extra in reconstruction |
| `discontinued_insertion_locations.csv` | Estimated executive PDF page/line for each discontinued item |

## Methodology

### Record Extraction

Budget records are extracted from PDF text using pattern matching on NYS bill language structure:

- **Reappropriations** identified by `(re. $X,XXX)` markers with chapter citations (`By chapter X, section Y, of the laws of YYYY`)
- **Appropriations** identified by `(XXXXX)` five-digit appropriation IDs with associated dollar amounts
- **Cross-page continuity** -- text buffers persist across page boundaries so records split between pages are captured correctly
- **Budgetary sub-line filtering** (State Operations only) -- expenditure category codes (Personal Service 50xxx, Contractual 51xxx, Travel 54xxx, Equipment 56xxx, etc.) are separated from real appropriation IDs to prevent false positives
- **Underline detection** (executive budgets) -- PyMuPDF identifies underlined text (thin filled rectangles in the PDF drawing layer) which indicates new or changed language in executive proposals

### Comparison Logic (4-Pass Matching)

Each enacted record is matched against executive records using a **four-pass composite key approach** with progressive relaxation:

1. **Pass 1 (Exact full):** `agency | appropriation_id | chapter_year | appropriation_amount | account` -- gold standard match with full composite key including fund account
2. **Pass 2 (Drop account):** `agency | appropriation_id | chapter_year | appropriation_amount` -- catches cases where the same appropriation moves between fund accounts
3. **Pass 3 (Drop amount, scored):** `agency | appropriation_id | chapter_year` + text similarity scoring -- catches funding changes where the amount was restated; candidates scored by Jaccard token overlap on bill language to avoid wrong-account grabs (threshold: 0.60)
4. **Pass 4 (Fuzzy text):** `agency | chapter_year` + bill language similarity -- catches cases where both the appropriation ID and amount changed but the legislative text describes the same program (threshold: 0.75)

All lookups use `defaultdict(list)` to prevent silent overwrites when multiple records share a key. A `claimed_exec_keys` set prevents double-matching across passes.

Records matched in any pass are classified as **continued** (same reappropriation amount) or **modified** (different amount). Pass 4 matches are classified as **likely reorganized** for manual review. Unmatched enacted records are classified as **discontinued**.

### Composite Key Design

The matching key intentionally includes `account` and `appropriation_amount` because the same `appropriation_id` can appear multiple times within a single budget (different chapter years, different fund accounts, different line items). Including account ensures each line item is uniquely identified -- empirically verified to produce zero collisions for real appropriation IDs across all three budget types.

### Reconstruction Validation

A round-trip proof verifies extraction and matching accuracy:

```
reconstructed = exec_reapprops(chyr <= 2024) + discontinued + missing_id
```

The reconstructed set should equal the enacted 25-26 reappropriation section. Coverage results:

| Budget Type | Enacted Reapprops | Coverage | Missing | Extras |
|---|---|---|---|---|
| State Ops | 1,130 | **100.0%** | 0 | 8 |
| Capital | 3,240 | **100.0%** | 0 | 118 |
| ATL | 6,339 | **99.8%** | 13 | 52 |

### Insertion Location Estimation

For each discontinued item, the tool estimates where it would be inserted in the executive PDF by finding the nearest enacted neighbors (predecessor/successor in document order) that survived into the executive, and using their executive page/line positions as anchors.

## Validation (v7)

| Budget Type | Enacted Records | Discontinued | Agencies | Amount |
|---|---|---|---|---|
| State Ops | 1,251 | 393 | 26 | $1.38B |
| Capital | 3,531 | 313 | 25 | $2.12B |
| ATL | 7,505 | 2,283 | 36 | $133B |

## Project Structure

```
nys_budget_analyzer.py      # Main analysis engine (extraction, 4-pass comparison, reporting)
bps_adds_matcher.py         # BPS legislative adds matcher (LLM-assisted)
highlight_discontinued.py   # PDF highlighting tool for manual review
requirements.txt            # Python dependencies
```

## Requirements

- Python 3.8+
- pdfplumber (PDF text extraction)
- pandas (CSV output and data handling)
- PyMuPDF / fitz (PDF highlighting and underline detection)

---

## Reappropriations Reference Guide -- SFY 2026-27

### Scope

- **Review**: Aid to Localities (ATL) and Capital budgets only -- not State Operations (unless your deputy requests it)
- **Standard review period**: SFY 22-23 forward (last 5 years) -- full insert process
- **New this year**: Track unspent reapprops from **SFY 21-22 and earlier** (tracking only -- no inserts)

### Do Not Insert

| Category | Notes |
|----------|-------|
| Anything older than 5 years | Track unspent balances instead (see Step 2c) |
| Vetoed items from last year | Check the veto list on WebLRS |
| Executive programs/initiatives dropped by the Executive | That's their call |
| Senate adds from prior years | Unless your deputy says otherwise |
| SFS balance under $1,000 | Will likely be spent before enactment |

### Step-by-Step Process

#### Step 1 -- Download Bill Copy from LRS

1. Go to **LRS** > "Budget" tab
2. Select the year > "Appropriation Bills"
3. Select ATL or CAP bill (use the link with **bill numbers only**, not the one with titles)
4. Select **"LBDC PDF Editor"** > use the dropdown to pick your agency
5. Download two sets:
   - **2025-26 Enacted Budget**
   - **2026-27 Executive Proposal A-print (30-day)**

#### Step 2a -- Find Dropped Reappropriations

- Open both sets side by side on screen
- Compare last year's enacted reapprops against this year's Executive reapprops
- Go program by program -- flag anything in last year's enacted that is **missing** from the Executive proposal
- Track using digital highlights, comments, or a spreadsheet

#### Step 2b -- Check Last Year's Appropriations

- Last year's **new appropriations** should appear as reappropriations this year
- They'll be at the **top** of the reapprop list for each program
- If a new appropriation from last year isn't reappropriated -- flag it

#### Step 2c -- Track Older Unspent Reappropriations (SFY 21-22 and Earlier)

This is a **tracking exercise only** -- no inserts.

- Review reapprops from SFY 21-22 and earlier in your agencies' ATL and Capital budgets
- Use SFS (same process as Step 4) to check balances
- For any with an undisbursed balance, record:

| Field | What to Capture |
|-------|-----------------|
| Chapter Year | Original fiscal year of the appropriation |
| Program Name | The program the funding was for |
| Initial Amount | Original appropriation amount |
| Current Amount | Undisbursed balance remaining in SFS |

- Compile into a spreadsheet and share with your deputy

#### Step 3 -- Meet With Your Deputy

- Share your list of dropped/reduced reapprops
- Deputy will tell you which are Assembly adds worth pursuing, which to ignore, and which to look up in SFS

#### Step 4 -- Look Up Flagged Items in SFS

1. Log in to **SFS** > select **"SFS Analytics"**
2. Click **"Budget Reports"** > **"DW620 - Appropriation Budget Overview"**
3. Set filters:
   - **Fiscal Year**: 2026
   - **Through Date**: most recent (bottom option)
   - **Budgetary Department**: your agency (number matches BPS)
   - **Budgetary Fund**: "Local Assistance Account" (ATL) or "Capital Projects Fund" (CAP)
   - **Budgetary Fund Level 4**: General Fund
4. Click **"Apply"** > **"Export"** > export as Excel

**Excel cleanup:**

1. Delete rows above the headers (keep the two header rows)
2. Freeze top panes
3. Hide all columns except:
   - Budgetary Fund
   - Budgetary Budget Reference (original year)
   - Budgetary Program Level 2
   - Budgetary Program
   - Budgetary Account
   - Budgetary Department
   - Original Approp Amount
   - Current Appropriation (reapprop amount)
   - LTD KK Expenditures (money spent)
   - **Undisbursed Approp Balance** -- the key column
4. Add filters to the header row
5. Filter by the **Budgetary Program number** from the bill copy
6. Match the **year** and **original amount** to confirm you have the right line
7. Read the **Undisbursed Approp Balance**:
   - **$0** -- all spent, no action needed
   - **> $1,000** -- candidate for insert (round up to nearest $1,000)
   - **< $1,000** -- skip

#### Step 5 -- Create the Insert (LBDC PDF Editor)

1. Open the **2025-26 Enacted Budget** in the LBDC PDF Editor
2. Select **"Multiuser Editing"** > choose **blue**
3. **Delete everything** on the page that is NOT the dropped reapprop
   - Use **Backspace** to delete, or **"Strike/Return"** to strikethrough and add a new line
   - Remove every word, number, even the old page number
4. Above the first line of the reapprop text, type: **"Insert [Exec Budget Page #] A"**
5. If the SFS balance is lower than the bill copy amount, strikethrough the old number and type the new one (rounded up to $1,000s)

**Rules:**
- Reapprop amount **cannot exceed** the original appropriation amount
- Multiple inserts on the same page: label **A**, **B**, **C**, etc.
- Always use the LBDC PDF Editor -- no other programs
- Edit language directly within the existing reapprop

#### Step 6 -- Mark the Destination Page

1. Open the corresponding page in the **2026-27 Executive Proposal**
2. Type **"Insert [Page #] A"** directly above where the insert should go
3. Make sure it's in the **correct program** -- don't place it in a neighboring program's section

#### Step 7 -- Senate Sign-Off

- Both houses must have **identical** bill copy
- Email your reapprop files to your Senate counterpart for two-way sign-off
- Review digitally, page by page:

| Scenario | Action |
|----------|--------|
| No changes on the page | Sign off |
| Insert added | Sign off on the page with insert |
| Disagreement | Use a **Hold Page** (replaces disputed page until reconciled) |
| Language change | **Must be a Hold Page** -- even if Senate agrees, language changes require 3-way agreement (Assembly, Senate, Executive) |

- Forward emails at every step so the sign-off chain is preserved
- Hold pages get resolved later in the three-way budget process
- If you change anything after initial submission, **re-sign-off** with your Senate counterpart before resubmitting

#### Step 8 -- Submit

- Send completed digital files to your deputy for review
- Deputy submits to Shelby

### Submission Checklist

- [ ] Every single page of the Executive budget for your agency included (even unchanged ones)
- [ ] Using the **A-print bill copy** (not the original Executive proposal)
- [ ] Pages in **sequential order**
- [ ] Every page signed off (analyst on each page; deputy on first page)
- [ ] All edits clean and legible -- bill drafting copies exactly what you mark up
- [ ] Matches your Senate counterpart's submission
- [ ] Email sign-off chain preserved (forwarded at every step)
- [ ] Insert files and destination files saved to the **share drive**
- [ ] Completed files sent to deputy before final submission to Shelby
- [ ] Any post-submission changes re-signed-off with Senate counterpart

### Summary Page Update

- First page of each agency shows **total appropriations and reappropriations by fund**
- As you add back reapprops, **increase these totals** accordingly
- Update the **All Funds** total
- Track what you've added back so this step is straightforward

### Key Reminders

1. **Be organized** -- the #1 tip from seasoned analysts
2. **Work closely with your deputy** -- they identify Assembly adds and set priorities
3. **Don't reinsert executive programs** -- even if the Senate pushes for it, use a Hold Page
4. **Check the veto list** on WebLRS before inserting anything
5. **Round up** to nearest $1,000 when reinserting
6. **Older reapprops (SFY 21-22 and earlier)**: track unspent balances (chapter year, program name, initial amount, current amount) -- no inserts, just tracking
7. **Language changes = 3-way agreement** -- always a Hold Page, even if Senate agrees
8. **Ask for help** -- screen shares are available if you're stuck
9. **Deadline**: end of February for two-way sign-off with Senate (may shift)

### Common Questions

| Question | Answer |
|----------|--------|
| Do I have to check every program? | Yes -- every agency and every program in ATL and Capital |
| What if inserts are on the same page? | Put both on the same page, label A and B |
| What does the SFS balance mean? | SFS shows money the state has **disbursed** to the program, not necessarily what the program has spent downstream |
| What about Senate adds? | Generally don't insert; check with deputy. If Senate insists on executive programs, use a Hold Page |
| Deadline? | End of February for two-way sign-off (may shift based on Senate readiness) |
| Why are we tracking old reapprops this year? | New direction -- tracking unspent balances on SFY 21-22 and earlier to get a full picture of outstanding money |
| Do I use SFS the same way for older reapprops? | Yes, same lookup process -- just record the info instead of creating inserts |

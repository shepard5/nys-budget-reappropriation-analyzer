# LBDC Budget Bill Data Structure Map

Universal mapping between spreadsheet data and LBDC HTML bill format across all three budget types (ATL, State Operations, Capital), all agencies, and all budget years.

**Validated against real bill**: A8803-X (ATL 2024-25) — 1,359 pages, 41 agencies, 5,247 reappropriations extracted with 100% hierarchy completeness (agency, program, fund, chapter_year for every record).

## The LBDC HTML Format

Every budget bill, regardless of type, renders to the same HTML structure via the LBDC API (`POST /extract-html/`):

```html
<div class="page" contenteditable="true">
  <p>                          315                          12553-09-5</p>  <!-- page header -->
  <p style="min-height: 14.75px;"> </p>                                    <!-- blank line -->
  <p>                       EDUCATION DEPARTMENT</p>                        <!-- agency -->
  <p style="min-height: 14.75px;"> </p>
  <p>            AID TO LOCALITIES - REAPPROPRIATIONS   2025-26</p>         <!-- budget type + fiscal year -->
  <p style="min-height: 14.75px;"> </p>
  <p> 1  ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM</p>        <!-- program (ATL only) -->
  <p style="min-height: 14.75px;"> </p>
  <p> 2    General Fund</p>                                                 <!-- fund type -->
  <p> 3    Local Assistance Account - 10000</p>                             <!-- account -->
  <p style="min-height: 14.75px;"> </p>
  <p> 4  By chapter 53, section 1, of the laws of 2024:</p>                <!-- chapter citation -->
  <p> 5    For case services provided ...</p>                               <!-- bill language -->
  <p> 6      (21713) ..................................... 54,000,000</p>   <!-- approp ID + amount -->
  <p> 7      ......................................... (re. $47,038,000)</p> <!-- reapprop anchor -->
</div>
```

**Every line is a `<p>` tag.** No semantic classes distinguish structural elements from content. Detection is entirely regex-based on text content.

---

## Universal Document Hierarchy

All three budget types share the same nesting structure:

```
Level 1: Agency          (ALL CAPS header, e.g., "EDUCATION DEPARTMENT")
Level 2: Budget Type     ("AID TO LOCALITIES - REAPPROPRIATIONS 2025-26")
Level 3: Program         (ALL agencies in ATL — e.g., "COMMUNITY SERVICES PROGRAM")
                         (STOPS/CAP may skip directly to Fund)
Level 4: Fund            ("General Fund", "Special Revenue Funds - Federal", "Capital Projects Fund")
Level 5: Account         ("Local Assistance Account - 10000", "State Purposes Account - 10050")
Level 6: Chapter Year    ("By chapter 53, section 1, of the laws of 2024:")
Level 7: Reappropriation (bill language + amounts + (re. $X) anchor)
```

**Key finding from A8803-X validation**: Programs are NOT ATL-only. ALL 41 agencies in the ATL bill have program sub-divisions (105 unique program names). Examples:
- OFFICE FOR THE AGING → COMMUNITY SERVICES PROGRAM
- DCJS → CRIME PREVENTION AND REDUCTION STRATEGIES PROGRAM
- DOH → 13 programs (ADMINISTRATION, AIDS INSTITUTE, CENTER FOR COMMUNITY HEALTH, etc.)
- DOT → 10 programs (MASS TRANSPORTATION, RURAL TRANSIT, etc.)

Some agencies wrap to 2 lines (e.g., "JUSTICE CENTER FOR THE PROTECTION" / "OF PEOPLE WITH SPECIAL NEEDS").

### Where the hierarchy maps in HTML

| Level | HTML Detection | Persistence |
|-------|---------------|-------------|
| Agency | ALL-CAPS line at p_idx 2-3, with multi-line merge | Persists until new agency header |
| Budget Type | Regex: `STATE OPERATIONS\|AID TO LOCALITIES\|CAPITAL PROJECTS` | Set once per document |
| Program | Dynamic: pre-scanned from SCHEDULE sections (105 names in ATL) | Resets chapter state (fund carries over) |
| Fund | `General Fund`, `Special Revenue Funds - ...`, `Capital Projects Fund` | Resets chapter state |
| Account | `Xxx Account - NNNNN` pattern | Completes the fund block |
| Chapter Year | `By chapter N, section N, of the laws of YYYY:` | Groups reappropriations |
| Reappropriation | `(re. $X,XXX,XXX)` anchor | Terminal — emits a record |

---

## Spreadsheet ↔ HTML Field Mapping

### Core Fields (Universal — All Budget Types)

| Spreadsheet Field | Source in HTML | Extraction Method | Generation Method |
|---|---|---|---|
| `agency` | ALL-CAPS line, typically line 2 of page header | Regex `^([A-Z][A-Z\s,\-&\.]{8,}[A-Z])$` with exclusion list | Emit as ALL-CAPS `<p>` line |
| `budget_type` | Header line: "AID TO LOCALITIES", "STATE OPERATIONS", "CAPITAL PROJECTS" | Regex on page header | Emit as header `<p>` with fiscal year |
| `fund_type` | Fund header line(s) | Regex: `General Fund\|Special Revenue Funds - ...\|Capital Projects Fund` | Emit as `<p>` line(s) |
| `account` | Account line with 5-digit code | Regex: `[A-Za-z]+ Account - \d{5}` | Emit as `<p>` line below fund type |
| `appropriation_id` | 5-digit ID in parentheses | Regex: `\((\d{5})\)` | Embed in bill language as `(NNNNN)` |
| `chapter_year` | Year from chapter citation | Regex: `of the laws of (\d{4})` | Part of chapter citation line |
| `chapter_citation` | Full "By chapter..." line | Regex: `^By\s+chapter\s+\d+` (may span multiple `<p>` tags) | Emit as `<p>` line(s) ending with `:` |
| `appropriation_amount` | Dollar amount with dot leaders | Regex: amount before `(re.` anchor | Format as `N,NNN,NNN` with dot leaders |
| `reappropriation_amount` | Amount inside `(re. $X)` | Regex: `\(re\.\s*\$\s*([\d,]+)\s*\)` | Format as `(re. $N,NNN,NNN)` |
| `bill_language` | All `<p>` text from chapter citation through `(re. $X)` | Accumulated text buffer between structural markers | Emit as sequence of `<p>` tags with line numbers |
| `page_number` | Page header: `NNN NNNNN-NN-N` | First `<p>` on each page div | Auto-paginated by LBDC PDF generator |
| `fiscal_year` | From budget type header | Regex: `(\d{4})-(\d{2})` | Part of budget type header |
| `record_type` | Presence/absence of `(re. $X)` | `(re. $X)` = reappropriation; no `(re.)` = appropriation | Determines whether to include `(re.)` suffix |

### ATL-Specific Fields

| Spreadsheet Field | Source in HTML | Notes |
|---|---|---|
| `program` | ALL-CAPS program name line | Only ATL has programs within an agency. Exact match against known list. |
| `bill_language` | Full multi-line text | ATL has the richest bill language — descriptions, provisos, conditions spanning 5-20 lines |

### State Operations-Specific Fields

| Spreadsheet Field | Source in HTML | Notes |
|---|---|---|
| `program` | N/A — STOPS has no program headers | Hierarchy goes Agency → Fund → Account → Chapter |
| `bill_language` | Minimal or absent in v7 output | STOPS reapprops are typically 1-2 lines: just `(NNNNN) ... amount ... (re. $X)` |
| `budgetary_account_code` | 5-digit code like `50100`, `51000` | **STOPS-unique.** Sub-line items under a parent approp ID. Prefixes: 50xxx=Personal Service, 51xxx=Contractual, 54xxx=Travel, 56xxx=Equipment, 57xxx=Supplies, 58xxx=Indirect, 60xxx=Fringe |
| `budgetary_account_description` | Text before the budgetary code | E.g., "Personal service--regular", "Contractual services", "Travel" |
| `parent_appropriation_id` | Inferred from context | The real 5-digit approp ID that the budgetary sub-line belongs to |

### Capital-Specific Fields

| Spreadsheet Field | Source in HTML | Notes |
|---|---|---|
| `program` | N/A — Capital has no program headers | Like STOPS: Agency → Fund → Account → Chapter |
| `bill_language` | Present in v7 `discontinued_all.csv` | Capital bill language tends to be medium-length (3-8 lines), describing project scope |
| `account` | Often "Unknown" in v7 | Capital accounts are less consistently detected — often just "Capital Projects Fund" without specific account |

---

## Budget Type Structural Differences

### ATL (Aid to Localities)

```
EDUCATION DEPARTMENT                              ← Agency
  AID TO LOCALITIES - REAPPROPRIATIONS 2025-26    ← Budget type
    ADULT CAREER AND CONTINUING EDUCATION ...     ← Program (ATL-unique)
      General Fund                                ← Fund type
      Local Assistance Account - 10000            ← Account
        By chapter 53, section 1, of the laws of 2024:   ← Chapter citation
          For case services provided on or after
          October 1, 2022 to disabled individuals...
          (21713) .................... 54,000,000  ← Approp ID + amount
          ........................ (re. $47,038,000)  ← Reapprop anchor
```

**Key ATL characteristics:**
- Programs are subdivisions within an agency (Education has 4)
- Bill language is verbose — descriptions, provisos, conditions
- Items without approp IDs exist (bare dollar amounts)
- Some items span 15-20 `<p>` tags
- ~6,339 reappropriations, ~1,166 appropriations (enacted 25-26)
- 41 agencies

### State Operations (STOPS)

```
DEPARTMENT OF AGRICULTURE AND MARKETS             ← Agency
  STATE OPERATIONS - REAPPROPRIATIONS 2025-26     ← Budget type
    General Fund                                  ← Fund type (no program level)
    State Purposes Account - 10050                ← Account
      By chapter 50, section 1, of the laws of 2024:   ← Chapter citation
        For services and expenses related to the
        administration program (81001).            ← Parent approp ID
        Personal service--regular (50100) ... 9,900,000  ← Budgetary sub-line
        .................................. (re. $5,873,000)
        Contractual services (51000) ... 2,000,000       ← Another sub-line
        .................................. (re. $1,500,000)
```

**Key STOPS characteristics:**
- No program level — agency goes directly to fund
- **Budgetary sub-accounts** (50xxx-60xxx) are nested under parent approp IDs
- A single approp ID (e.g., 81001) can have multiple budgetary sub-lines
- Bill language is minimal — often just the amount line
- The chapter citation + description belong to the parent; sub-lines inherit context
- ~1,130 reappropriations, ~121 appropriations (enacted 25-26)
- 33 agencies

### Capital Projects (CAP)

```
ADIRONDACK PARK AGENCY                            ← Agency
  CAPITAL PROJECTS - REAPPROPRIATIONS 2025-26     ← Budget type
    Capital Projects Fund                         ← Fund type (no program level)
    Miscellaneous Gifts Account - 20100           ← Account
      By chapter 54, section 1, of the laws of 2022:   ← Chapter citation
        For services and expenses related to ...
        (81010) .................... 29,000,000    ← Approp ID + amount
        ........................ (re. $29,000,000) ← Reapprop anchor
```

**Key CAP characteristics:**
- No program level — agency goes directly to fund
- No budgetary sub-accounts — each reappropriation is a standalone item
- Bill language is medium-length (project descriptions)
- Typically uses chapter 54 (capital budget bill) not chapter 53 (operating)
- Chapter years can go back very far (1987, 1990, etc.)
- Account detection is weaker — many "Unknown" in v7
- ~3,240 reappropriations, ~291 appropriations (enacted 25-26)
- 35 agencies

---

## Universal Data Schema

The canonical record that can represent any budget type:

```python
@dataclass
class BudgetReappropriation:
    # ── Hierarchy (document position) ──
    agency: str                    # "EDUCATION DEPARTMENT" — all types
    budget_type: str               # "AID TO LOCALITIES" | "STATE OPERATIONS" | "CAPITAL PROJECTS"
    program: str                   # ATL only; "" for STOPS/CAP
    fund_type: str                 # "General Fund", "Special Revenue Funds - Federal", etc.
    account: str                   # "Local Assistance Account - 10000"

    # ── Identification ──
    appropriation_id: str          # 5-digit ID: "21713"
    chapter_year: int              # 2024
    chapter_citation: str          # "By chapter 53, section 1, of the laws of 2024:"
    fiscal_year: str               # "2025-26"
    record_type: str               # "reappropriation" | "appropriation"

    # ── Amounts ──
    appropriation_amount: int      # Original amount: 54000000
    reappropriation_amount: int    # Reapprop amount: 47038000 (0 for appropriations)

    # ── Text ──
    bill_language: str             # Full text from chapter citation through (re. $X)

    # ── STOPS-specific (budgetary sub-accounts) ──
    parent_appropriation_id: str   # For budgetary sub-lines: the real approp ID
    budgetary_account_code: str    # "50100", "51000", etc. (empty for ATL/CAP)
    budgetary_account_desc: str    # "Personal service--regular" (empty for ATL/CAP)

    # ── Position in HTML ──
    page_idx: int                  # 0-based page div index
    p_start: int                   # First <p> index within page
    p_end: int                     # Last <p> index within page (line with re.$)
    global_p_start: int            # Global <p> index across all pages
    global_p_end: int              # Global <p> index across all pages

    # ── Amendment metadata ──
    amendment_type: str            # "basic" | "amended" | "added" | "transferred" | "directive"
    amending_year: int             # Year of amending chapter (0 if not amended)

    # ── Source ──
    source_file: str               # "ATL 25-26.pdf"
```

---

## Spreadsheet → HTML Generation Rules

### Page Structure

Each page in the generated HTML is a `<div class="page">` containing ~50-56 numbered lines. The LBDC PDF generator handles pagination, page headers (`NNN NNNNN-NN-N`), and line numbering. You provide the content; it wraps into pages.

### Line Numbering

Bill lines are numbered 1-56 within each page. The format is:
```
<p> 1  Content text here</p>
<p> 2    Indented content</p>
<p>10  Line ten (no leading space)</p>
```

Single-digit numbers have a leading space. Content is indented 2-4 spaces after the number.

### Generating a Complete Reappropriation Block

Given spreadsheet data, emit this sequence of `<p>` tags:

```
1. Chapter citation (may span 1-3 <p> tags):
   "By chapter {ch_num}, section {sec_num}, of the laws of {chapter_year}:"

   For amendments:
   "The appropriation[s] made by chapter {ch_num}, section {sec_num}, of the
    laws of {orig_year} as amended by chapter {amend_ch} of the laws of
    {amending_year}, is/are hereby reappropriated and amended to read:"

2. Bill language lines (1-20 <p> tags):
   Description text, provisos, conditions...

3. Amount line (1-2 <p> tags):
   For items WITH approp ID:
     "({approp_id}) .............. {approp_amount:,}"
   For items WITHOUT approp ID:
     "{approp_amount:,}"

4. Reappropriation anchor (same or next <p>):
   ".............................. (re. ${reapprop_amount:,})"
```

### Generating Structural Headers

When inserting into an executive bill, you may need to emit missing structural blocks:

```python
# Program header (ATL only):
"ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM"

# Fund header (2 lines):
"General Fund"
"Local Assistance Account - 10000"

# Or for multi-part funds:
"Special Revenue Funds - Federal"
"Federal Miscellaneous Operating Grants Fund"
"Office for the Aging Federal Grants Account - 25300"
```

### Dot Leader Formatting

Dollar amounts use dot leaders to fill the line to a standard width:
```
({approp_id}) .......... {amount:>12,}
```

The number of dots varies to right-justify the amount at approximately column 60. The reapprop anchor follows similarly:
```
........................ (re. ${reapprop_amount:,})
```

### STOPS Budgetary Sub-Line Format

State Operations sub-lines use a distinct format:
```
{description} ({budgetary_code}) ... {amount:,} ........... (re. ${reapprop:,})
```

Example:
```
Personal service--regular (50100) ... 9,900,000 ..... (re. $5,873,000)
Contractual services (51000) ... 2,000,000 .......... (re. $1,500,000)
```

---

## HTML → Spreadsheet Extraction Rules

### Universal Anchor: `(re. $X)`

The single most reliable pattern across all budget types. Every reappropriation terminates with `(re. $X,XXX,XXX)`. This is the extraction anchor.

### Extraction State Machine

```
for each <p> tag:
  1. Is it a page header? → skip
  2. Is it a program header? → update state.program, reset fund/chapter (ATL only)
  3. Is it a fund header line? → accumulate fund_lines
     - If account line found → complete fund block, reset chapter
  4. Is it a chapter citation? → update state.chapter_year, state.chapter_citation
     - May span multiple <p> tags (continue until line ends with ":")
  5. Does it contain (re. $X)? → EMIT reappropriation
     - Collect all buffered lines as bill_language
     - Extract approp_id, amounts from accumulated text
     - Record position (page_idx, p_start, p_end)
  6. Otherwise → accumulate in pending_buffer
```

### Budget-Type-Specific Extraction Notes

| Step | ATL | STOPS | CAP |
|------|-----|-------|-----|
| Program detection | Match against `KNOWN_PROGRAMS` list | Skip — no programs | Skip — no programs |
| Approp ID extraction | `(\d{5})` — exclude budgetary codes | Must distinguish parent ID from budgetary codes (50xxx-60xxx) | `(\d{5})` — straightforward |
| Bill language accumulation | Full text from chapter citation to `(re.)` | Minimal — often just amount line | Medium — project descriptions |
| Cross-page handling | Records can span page boundaries; buffer carries over | Budgetary sub-lines share parent ID across pages; `pending_parent_approp_id` carries over | Similar to ATL |
| Amount format | `(NNNNN) ... N,NNN,NNN ... (re. $N,NNN,NNN)` | `Description (NNNNN) ... N,NNN,NNN ... (re. $N,NNN,NNN)` | `N,NNN,NNN ... (re. $N,NNN,NNN)` (ID often on prior line) |

---

## Matching Key Composition

For comparing enacted vs. executive (used by `compare.py`):

| Pass | Key Fields | Notes |
|------|-----------|-------|
| 1 (exact) | `(program, fund, chapter_year, approp_id)` | Best match — all four must agree |
| 2 (fund-flex) | `(program, chapter_year, approp_id)` | Handles fund name variations |
| 3 (no-ID) | `(program, fund, chapter_year, approp_amount)` + text similarity | For items without approp IDs |
| 4 (fallback) | `(program, chapter_year)` + Jaccard > 0.6 | Last resort text matching |

For STOPS/CAP where `program=""`, passes 1-2 effectively become `(fund, chapter_year, approp_id)`.

---

## Known Edge Cases and Gaps

### Items Without Approp IDs

Some ATL items have bare dollar amounts without `(NNNNN)`:
```
For services and expenses of public radio stations ... 4,000,000
                                          (re. $4,000,000)
```

These are extractable (the `(re. $X)` anchor still works) but have no `approp_id` for matching. The comparison engine falls back to amount + text similarity.

### Multi-Line Chapter Citations

Chapter citations can span 2-3 `<p>` tags:
```
By chapter 53, section 1, of the laws of 2023 as amended by
chapter 53, section 1, of the laws of 2024 and as further amended
by chapter 58, section 1, of the laws of 2025:
```

Detection: starts with `By chapter` or `The appropriation[s] made by chapter`, continues until line ends with `:` or contains `to read`.

### STOPS Parent-Child Relationship

A single chapter citation + description can have multiple budgetary sub-lines:
```
By chapter 50, section 1, of the laws of 2024:
For services and expenses related to the agricultural business
services program (10901).                              ← parent approp
Personal service--regular (50100) ... 9,900,000        ← child 1
                          (re. $5,873,000)
Contractual services (51000) ... 500,000               ← child 2
                          (re. $500,000)
```

The parent-child relationship is positional, not marked in HTML.

### Inconsistent Fund Type Casing

Fund types appear in inconsistent casing across pages:
- `General Fund` vs `general fund`
- `Special Revenue Funds - Federal` vs `special revenue funds - federal`
- `Capital Projects Fund` vs `CAPITAL PROJECTS FUND`

Normalization (lowercase + trim) is required for matching.

### Inconsistent Account Names

Capital budgets frequently have `Unknown` accounts. The account line may be missing or formatted differently. The pattern `Account - NNNNN` doesn't always match.

### LBDC API Constraints

- **Upload**: PDFs must be LBDC-compatible format (NYS legislative bills). Standard PDFs may fail.
- **No batch API**: Each PDF must be uploaded individually.
- **HTML ↔ PDF round-trip**: HTML → PDF via `/generate-pdf/` always works. But the HTML is not a perfect representation of the PDF layout — pagination and line breaks may shift.
- **No authentication required**: The API is public with CSRF tokens only.

---

## Summary: What's Universal vs. What Varies

### Universal (works for all three)
- `(re. $X)` anchor detection
- Chapter citation parsing
- Fund/Account header detection
- Approp ID extraction via `(\d{5})`
- Amount parsing
- Page/line position tracking
- Structural element recording

### ATL-Only
- Program headers (`KNOWN_PROGRAMS` — needs extension per agency)
- Rich bill language (multi-paragraph descriptions)
- Items without approp IDs (bare amounts)

### STOPS-Only
- Budgetary sub-accounts (50xxx-60xxx codes)
- Parent-child approp ID tracking
- Minimal bill language
- `BudgetaryAccountRecord` data class

### CAP-Only
- Deep chapter year history (back to 1987)
- Weak account detection ("Unknown")
- Typically chapter 54 (capital bill) vs. chapter 53/50 (operating)

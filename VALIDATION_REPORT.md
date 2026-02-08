# NYS Budget Reappropriation Analysis - Validation Report

## Test Date: 2025-02-05

## Files Compared
- **Enacted:** 2025 Enacted.pdf (1,058 pages)
- **Executive:** 2026 Executive.pdf (1,077 pages)

## Extraction Results
| Metric | Enacted | Executive |
|--------|---------|-----------|
| Total Records | 5,297 | 5,529 |
| Reappropriations | 4,487 | 4,700 |
| Appropriations | 807 | 829 |

## Comparison Results (Reappropriations Only)
| Status | Count | Description |
|--------|-------|-------------|
| Continued | 1,935 | Same amount in both budgets |
| Modified | 1,095 | Different amount in executive |
| Discontinued | 1,457 | Not in executive budget |

## Validation Checks

### 1. Deduplication ✅
- 1,875 total discontinued items
- 1,875 unique composite keys
- **0 duplicates** (vs old script with 3x duplicates)

### 2. Random Sample Verification ✅
5 random discontinued reappropriations verified:
- All existed in enacted budget
- None existed in executive budget
- Correctly flagged as discontinued

### 3. Continued Items Verification ✅
- Items in both budgets correctly matched
- Same amounts = CONTINUED
- Different amounts = MODIFIED

### 4. Mathematical Consistency ✅
- 4,487 enacted reappropriations
- 3,030 in both budgets (continued + modified)
- 1,457 only in enacted (discontinued)
- 4,487 - 3,030 = 1,457 ✓

## Output Fields Validated
- ✅ agency
- ✅ budget_type
- ✅ fund_type
- ✅ account
- ✅ appropriation_id (5-digit)
- ✅ chapter_year (from "By chapter X, section Y, of the laws of YYYY")
- ✅ appropriation_amount
- ✅ reappropriation_amount
- ✅ bill_language (full text)
- ✅ page_number
- ✅ line_number
- ✅ composite_key (for deduplication)

## Known Limitations
1. **Appropriation extraction** is limited - captures ~807 vs expected ~2,000
2. Script focuses on reappropriation sections (`(re. $X)` marker)
3. Appropriations without reapprop marker are captured but less comprehensively

## Files
- `nys_budget_analyzer.py` - Main script
- `nys_budget_analyzer_v1_reapprops_only.py` - Backup of validated version
- `full_test_output/` - Test results

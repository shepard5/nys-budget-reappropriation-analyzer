"""
Build Inserts Pipeline
======================
Formalized pipeline to generate insert groups, exec mappings, and Excel workbooks
from the extraction CSVs and anchor JSONs.

Fixes the following bugs from the REPL-session pipeline:
  Bug 1: approp_id float/string contamination (.0 suffix) in enacted elements
  Bug 2: enacted_to_exec_loc.json keyed by line_end instead of line_start
  Bug 3: exec_below uses chapter_year match instead of sequential document order
  Bug 4: 30 self-referencing anchors (enacted_above == enacted_below)
  Bug 5: Sentinel page 999 for unmappable exec_below

Usage:
    python build_inserts.py
"""

import json
import pickle
import re
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Tuple, Optional, Any


BASE_DIR = Path("/Users/samscott/Desktop/REAPPROPS")
DROPS_DIR = BASE_DIR / "Drops"

# Minimum SFS undisbursed balance to count as "has balance" (in dollars)
MIN_UNDISBURSED = 1000

# Regex for extracting amending year from "as amended/added/transferred by ... of the laws of YYYY"
RE_AMENDING_YEAR = re.compile(
    r'as\s+(?:amended|added|transferred)\s+by\s+chapter\s+\d+.*?of\s+the\s+laws\s+of\s+(\d{4})',
    re.IGNORECASE
)


# =============================================================================
# AMENDMENT PARSING
# =============================================================================

def parse_amendment_info(text: str) -> dict:
    """Parse amendment fields from chapter year citation text.

    Returns dict with:
        is_amended: bool
        amending_year: int (0 if not amended)
        amendment_type: str ('basic'|'amended'|'added'|'transferred'|'directive')

    Citation formats (chapter/section numbers vary — only year matters):
      1. Basic:       "By chapter XX, section X, of the laws of XXXX:"
      2. Amended:     "By chapter XX, ..., as amended by chapter XX, ..., of the laws of YYYY:"
      3. Added:       "By chapter XX, ..., as added by chapter XX, ..., of the laws of YYYY:"
      4. Transferred: "By chapter XX, ..., as transferred by chapter XX, ..., of the laws of YYYY:"
      5. Directive:   "The appropriation(s) made by chapter XX, ..., is hereby amended..."
                      (may also contain "as amended by ..." internally)
    """
    t = text.lower().strip()

    is_directive = t.startswith('the appropriation')

    # Extract amending year if present
    m = RE_AMENDING_YEAR.search(text)
    amending_year = int(m.group(1)) if m else 0

    # Classify amendment type
    if is_directive:
        amendment_type = 'directive'
        is_amended = True  # Directives are always amendments
    elif 'as amended by' in t:
        amendment_type = 'amended'
        is_amended = True
    elif 'as added by' in t:
        amendment_type = 'added'
        is_amended = True
    elif 'as transferred by' in t:
        amendment_type = 'transferred'
        is_amended = True
    else:
        amendment_type = 'basic'
        is_amended = False

    return {
        'is_amended': is_amended,
        'amending_year': amending_year,
        'amendment_type': amendment_type,
    }


# =============================================================================
# HIERARCHY INDEXING
# =============================================================================

def build_hierarchy_indices(anchors: list) -> dict:
    """Assign numeric indices to programs and funds based on document order.

    Returns dict with:
        program_idx: dict mapping program_name -> int (1-based)
        fund_idx: dict mapping (program_name, fund_name) -> int (1-based within program)
        program_names: list of program names in document order
        fund_names: dict mapping program_name -> list of fund names in order
    """
    program_idx = {}
    fund_idx = {}
    program_names = []
    fund_names = defaultdict(list)

    sorted_anchors = sorted(anchors, key=lambda a: (a['page'], a['line_start']))

    for a in sorted_anchors:
        prog = a.get('program', '')
        if a['type'] == 'program' and prog not in program_idx:
            program_idx[prog] = len(program_idx) + 1
            program_names.append(prog)

        if a['type'] == 'fund':
            fund_key = (prog, a.get('fund', ''))
            if fund_key not in fund_idx:
                fund_idx[fund_key] = len(fund_names[prog]) + 1
                fund_names[prog].append(a.get('fund', ''))

    return {
        'program_idx': program_idx,
        'fund_idx': fund_idx,
        'program_names': program_names,
        'fund_names': dict(fund_names),
    }


def build_element_tuples(elements: list, hierarchy: dict) -> None:
    """Add tuple_key = (program_idx, fund_idx, chapter_year, amending_year) to every element.

    Modifies elements in place. Reapprops and chapter year anchors inherit their
    fund context from the most recent fund anchor in document order.
    """
    prog_idx = hierarchy['program_idx']
    fund_idx = hierarchy['fund_idx']

    current_prog = ''
    current_fund = ''

    for elem in elements:
        # Update context when we hit program/fund anchors
        if elem['type'] == 'anchor_program':
            current_prog = elem.get('program', '')
        elif elem['type'] == 'anchor_fund':
            current_fund = elem.get('fund', '')

        # Use element's own program/fund if available, else use context
        ep = elem.get('program', '') or current_prog
        ef = elem.get('fund', '') or current_fund

        pidx = prog_idx.get(ep, 0)
        fidx = fund_idx.get((ep, ef), 0)
        cy = elem.get('chapter_year', 0)
        ay = elem.get('amending_year', 0)

        elem['tuple_key'] = (pidx, fidx, cy, ay)
        elem['program_idx'] = pidx
        elem['fund_idx'] = fidx


# =============================================================================
# HIERARCHICAL RANGES
# =============================================================================

def build_hierarchical_ranges(elements: list, hierarchy: dict) -> dict:
    """Compute page/line ranges at each hierarchy level.

    Each level persists from its start to the next sibling at the same or higher
    level. This mirrors the budget document structure where:
      Program range → contains Fund ranges → contains Chapter Year ranges → contains Reapprops

    Returns nested dict:
        { program_name: {
            'idx': int,
            'range': (start_page, start_line, end_page, end_line),
            'funds': {
                fund_name: {
                    'idx': int,
                    'range': (start_page, start_line, end_page, end_line),
                    'chapter_years': [
                        {
                            'chapter_year': int,
                            'amending_year': int,
                            'amendment_type': str,
                            'is_amended': bool,
                            'range': (start_page, start_line, end_page, end_line),
                            'text': str,
                        }, ...
                    ]
                }
            }
        }}
    """
    prog_idx = hierarchy['program_idx']
    fund_idx = hierarchy['fund_idx']

    # Collect anchors only (sorted by position)
    anchors = [e for e in elements if e['type'].startswith('anchor_')]
    anchors.sort(key=lambda e: e['sort_key'])

    # Also need document boundary
    all_pos = [e['sort_key'] for e in elements]
    doc_end = max(all_pos) if all_pos else (0, 0)
    # Use line_end of last element for true document end
    last_elem = max(elements, key=lambda e: e['sort_key'])
    doc_end_page = last_elem.get('page_end', last_elem['page'])
    doc_end_line = last_elem.get('line_end', last_elem['line_start'])

    result = {}

    # Pass 1: Identify program ranges
    program_anchors = [a for a in anchors if a['type'] == 'anchor_program']
    for i, pa in enumerate(program_anchors):
        prog_name = pa['program']
        start = (pa['page'], pa['line_start'])

        # End: next program anchor start, or doc end
        if i + 1 < len(program_anchors):
            next_pa = program_anchors[i + 1]
            end = (next_pa['page'], next_pa['line_start'] - 1)
        else:
            end = (doc_end_page, doc_end_line)

        result[prog_name] = {
            'idx': prog_idx.get(prog_name, 0),
            'range': (start[0], start[1], end[0], end[1]),
            'funds': {},
        }

    # Build a position-based index into the full anchors list for quick lookup
    anchor_pos_to_idx = {}
    for idx, a in enumerate(anchors):
        anchor_pos_to_idx[(a['page'], a['line_start'], a['type'])] = idx

    # Pass 2: Identify fund ranges within each program
    fund_anchors = [a for a in anchors if a['type'] == 'anchor_fund']
    for fa in fund_anchors:
        prog_name = fa['program']
        fund_name = fa['fund']
        start = (fa['page'], fa['line_start'])

        # Find position in full anchors list, then search forward
        fa_idx = anchor_pos_to_idx.get((fa['page'], fa['line_start'], 'anchor_fund'), 0)
        end = None
        for j in range(fa_idx + 1, len(anchors)):
            if anchors[j]['type'] in ('anchor_fund', 'anchor_program'):
                end = (anchors[j]['page'], anchors[j]['line_start'] - 1)
                break
        if end is None:
            prog_range = result.get(prog_name, {}).get('range', (0, 0, doc_end_page, doc_end_line))
            end = (prog_range[2], prog_range[3])

        if prog_name in result:
            result[prog_name]['funds'][fund_name] = {
                'idx': fund_idx.get((prog_name, fund_name), 0),
                'range': (start[0], start[1], end[0], end[1]),
                'chapter_years': [],
            }

    # Pass 3: Identify chapter year ranges within each fund
    chyr_anchors = [a for a in anchors if a['type'] == 'anchor_chapter_year']
    for ca in chyr_anchors:
        prog_name = ca['program']
        fund_name = ca['fund']
        start = (ca['page'], ca['line_start'])

        # Find position in full anchors list, then search forward for end
        ca_idx = anchor_pos_to_idx.get((ca['page'], ca['line_start'], 'anchor_chapter_year'), 0)
        end = None
        for j in range(ca_idx + 1, len(anchors)):
            nxt = anchors[j]
            if nxt['type'] in ('anchor_fund', 'anchor_program'):
                end = (nxt['page'], nxt['line_start'] - 1)
                break
            if nxt['type'] == 'anchor_chapter_year':
                end = (nxt['page'], nxt['line_start'] - 1)
                break

        if end is None:
            # Last chapter year in last fund — use fund end or program end
            fund_data = result.get(prog_name, {}).get('funds', {}).get(fund_name)
            if fund_data:
                end = (fund_data['range'][2], fund_data['range'][3])
            else:
                end = (doc_end_page, doc_end_line)

        chyr_entry = {
            'chapter_year': ca.get('chapter_year', 0),
            'amending_year': ca.get('amending_year', 0),
            'amendment_type': ca.get('amendment_type', 'basic'),
            'is_amended': ca.get('is_amended', False),
            'range': (start[0], start[1], end[0], end[1]),
            'text': ca.get('text', ''),
        }

        if prog_name in result and fund_name in result[prog_name]['funds']:
            result[prog_name]['funds'][fund_name]['chapter_years'].append(chyr_entry)

    return result


# =============================================================================
# EXEC PRESENCE ANNOTATION
# =============================================================================

def annotate_exec_presence(enacted_elements: list, exec_anchors: list) -> None:
    """Mark each enacted structural anchor with has_exec_counterpart.

    An element can only serve as an anchor for insert placement if it exists
    in the executive budget. Chapter year headers that only exist in enacted
    are insert CONTENT (missing headers), not group boundaries.

    For chapter year anchors, we check if ANY variant of that (program, fund,
    chapter_year) exists in exec — the specific amendment variant doesn't need
    to match for the chapter year section to "exist" in exec.
    """
    exec_programs = set(
        a['program'] for a in exec_anchors if a['type'] == 'program'
    )
    exec_funds = set(
        (a['program'], a['fund']) for a in exec_anchors if a['type'] == 'fund'
    )
    exec_chyrs = set(
        (a['program'], a['fund'], a['chapter_year'])
        for a in exec_anchors if a['type'] == 'chapter_year'
    )

    annotated = 0
    for elem in enacted_elements:
        if elem['type'] == 'anchor_program':
            elem['has_exec_counterpart'] = elem['program'] in exec_programs
            annotated += 1
        elif elem['type'] == 'anchor_fund':
            elem['has_exec_counterpart'] = (elem['program'], elem['fund']) in exec_funds
            annotated += 1
        elif elem['type'] == 'anchor_chapter_year':
            elem['has_exec_counterpart'] = (
                elem['program'], elem['fund'], elem['chapter_year']
            ) in exec_chyrs
            annotated += 1

    return annotated


# =============================================================================
# DATA LOADING
# =============================================================================

def clean_approp_id(val) -> Optional[str]:
    """Clean approp_id: remove .0 suffix, convert to string or None."""
    if pd.isna(val) or val is None or str(val).strip() in ('', 'nan', 'None'):
        return None
    s = str(val).strip()
    # Remove .0 suffix from pandas float conversion
    if s.endswith('.0'):
        s = s[:-2]
    return s


def load_extraction_csvs():
    """Load all extraction CSVs from extract_reapprops.py output."""
    enacted_df = pd.read_csv(BASE_DIR / "enacted_25_26_reapprops.csv")
    exec_df = pd.read_csv(BASE_DIR / "executive_26_27_reapprops.csv")
    continued_df = pd.read_csv(BASE_DIR / "continued_modified_reapprops.csv")
    dropped_df = pd.read_csv(BASE_DIR / "dropped_reapprops.csv")

    # Clean approp_ids across all DataFrames
    for df in [enacted_df, exec_df, continued_df, dropped_df]:
        df['approp_id'] = df['approp_id'].apply(clean_approp_id)

    return enacted_df, exec_df, continued_df, dropped_df


def load_anchors():
    """Load structural anchor JSONs."""
    with open(BASE_DIR / "enacted_anchors.json") as f:
        enacted_anchors = json.load(f)
    with open(BASE_DIR / "exec_anchors.json") as f:
        exec_anchors = json.load(f)
    return enacted_anchors, exec_anchors


# =============================================================================
# ELEMENT LIST BUILDING
# =============================================================================

def build_enacted_elements(enacted_df, continued_df, dropped_df, enacted_anchors):
    """Build the enacted elements list: reapprops + structural anchors.

    Each element is a dict with at minimum:
      sort_key: (page, line_start)
      type: 'reapprop' | 'anchor_program' | 'anchor_fund' | 'anchor_chapter_year'
      program, fund, chapter_year
    Reapprops also have: approp_id, reapprop_amount, approp_amount, bill_language,
      chapter_citation, page, line_start, line_end, status, sfs_balance, has_undisbursed
    """
    elements = []

    # Build lookup for status and SFS from continued and dropped
    # Key: (approp_id, chapter_year, page, line_start)
    continued_keys = set()
    for _, row in continued_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        continued_keys.add((aid, int(row['chapter_year']), int(row['page_number']),
                           int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0))

    dropped_lookup = {}
    for _, row in dropped_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        key = (aid, int(row['chapter_year']), int(row['page_number']),
               int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0)
        sfs = row.get('sfs_undisbursed_balance', 0)
        if pd.isna(sfs):
            sfs = 0
        dropped_lookup[key] = float(sfs)

    # Build reapprop elements from enacted_df
    for _, row in enacted_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        page = int(row['page_number'])
        ls = int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0
        le = int(row['line_number_end']) if pd.notna(row['line_number_end']) else ls
        cy = int(row['chapter_year'])

        key = (aid, cy, page, ls)
        if key in continued_keys:
            status = 'continued_or_modified'
            sfs_balance = 0
        elif key in dropped_lookup:
            status = 'dropped'
            sfs_balance = dropped_lookup[key]
        else:
            # Fallback: try matching without line_start (for edge cases)
            is_cont = any(ck[0] == aid and ck[1] == cy and ck[2] == page for ck in continued_keys)
            if is_cont:
                status = 'continued_or_modified'
                sfs_balance = 0
            else:
                status = 'dropped'
                sfs_balance = 0

        has_undisbursed = sfs_balance >= MIN_UNDISBURSED

        elements.append({
            'sort_key': (page, ls),
            'type': 'reapprop',
            'program': row['program'],
            'fund': row['fund'],
            'chapter_year': cy,
            'approp_id': aid,
            'reapprop_amount': int(row['reapprop_amount']),
            'approp_amount': int(row['approp_amount']) if pd.notna(row['approp_amount']) else None,
            'page': page,
            'line_start': ls,
            'line_end': le,
            'bill_language': row['bill_language'] if pd.notna(row['bill_language']) else '',
            'chapter_citation': row['chapter_citation'] if pd.notna(row['chapter_citation']) else '',
            'status': status,
            'sfs_balance': sfs_balance,
            'has_undisbursed': has_undisbursed,
        })

    # Add structural anchors
    for anchor in enacted_anchors:
        atype = anchor['type']
        elem = {
            'sort_key': (anchor['page'], anchor['line_start']),
            'type': f'anchor_{atype}',
            'anchor_type': atype,
            'program': anchor.get('program', ''),
            'fund': anchor.get('fund', ''),
            'chapter_year': anchor.get('chapter_year', 0),
            'text': anchor.get('text', ''),
            'page': anchor['page'],
            'line_start': anchor['line_start'],
            'line_end': anchor.get('line_end', anchor['line_start']),
            'page_end': anchor.get('page_end', anchor['page']),
        }
        # Parse amendment info for chapter year anchors
        if atype == 'chapter_year':
            amend = parse_amendment_info(anchor.get('text', ''))
            elem.update(amend)
        elements.append(elem)

    # Sort by document order
    elements.sort(key=lambda e: e['sort_key'])

    # Propagate amending_year from chapter year anchors to reapprops.
    # Walk in document order — each reapprop inherits from the most recent
    # chapter year anchor above it.
    current_amending_year = 0
    current_is_amended = False
    current_amendment_type = 'basic'
    for elem in elements:
        if elem['type'] == 'anchor_chapter_year':
            current_amending_year = elem.get('amending_year', 0)
            current_is_amended = elem.get('is_amended', False)
            current_amendment_type = elem.get('amendment_type', 'basic')
        elif elem['type'] == 'reapprop':
            elem['amending_year'] = current_amending_year
            elem['is_amended'] = current_is_amended
            elem['amendment_type'] = current_amendment_type

    return elements


def build_exec_elements(exec_df, exec_anchors):
    """Build the executive elements list: reapprops + structural anchors."""
    elements = []

    for _, row in exec_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        page = int(row['page_number'])
        ls = int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0
        le = int(row['line_number_end']) if pd.notna(row['line_number_end']) else ls

        elements.append({
            'sort_key': (page, ls),
            'type': 'reapprop',
            'program': row['program'],
            'fund': row['fund'],
            'chapter_year': int(row['chapter_year']),
            'approp_id': aid,
            'reapprop_amount': int(row['reapprop_amount']),
            'approp_amount': int(row['approp_amount']) if pd.notna(row['approp_amount']) else None,
            'page': page,
            'line_start': ls,
            'line_end': le,
            'bill_language': row['bill_language'] if pd.notna(row['bill_language']) else '',
            'chapter_citation': row['chapter_citation'] if pd.notna(row['chapter_citation']) else '',
        })

    for anchor in exec_anchors:
        atype = anchor['type']
        elem = {
            'sort_key': (anchor['page'], anchor['line_start']),
            'type': f'anchor_{atype}',
            'anchor_type': atype,
            'program': anchor.get('program', ''),
            'fund': anchor.get('fund', ''),
            'chapter_year': anchor.get('chapter_year', 0),
            'text': anchor.get('text', ''),
            'page': anchor['page'],
            'line_start': anchor['line_start'],
            'line_end': anchor.get('line_end', anchor['line_start']),
            'page_end': anchor.get('page_end', anchor['page']),
        }
        # Parse amendment info for chapter year anchors
        if atype == 'chapter_year':
            amend = parse_amendment_info(anchor.get('text', ''))
            elem.update(amend)
        elements.append(elem)

    elements.sort(key=lambda e: e['sort_key'])

    # Propagate amending_year from chapter year anchors to reapprops
    current_amending_year = 0
    current_is_amended = False
    current_amendment_type = 'basic'
    for elem in elements:
        if elem['type'] == 'anchor_chapter_year':
            current_amending_year = elem.get('amending_year', 0)
            current_is_amended = elem.get('is_amended', False)
            current_amendment_type = elem.get('amendment_type', 'basic')
        elif elem['type'] == 'reapprop':
            elem['amending_year'] = current_amending_year
            elem['is_amended'] = current_is_amended
            elem['amendment_type'] = current_amendment_type

    return elements


# =============================================================================
# ENACTED-TO-EXEC MAPPING
# =============================================================================

def build_enacted_to_exec_mapping(continued_df, exec_df):
    """Build mapping from enacted (page, line_start) to exec (page, line_start, line_end).

    Uses the continued/modified records — for each, finds the matching exec record
    by approp_id + chapter_year to get exec page/line numbers.

    Fixes Bug 2: Keys are now (page, line_start), not (page, line_end).
    """
    # Build exec lookup: (approp_id, chapter_year) -> (page, line_start, line_end)
    # Handle potential duplicates by also keying on fund
    exec_lookup = {}
    for _, row in exec_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        cy = int(row['chapter_year'])
        fund = row['fund']
        page = int(row['page_number'])
        ls = int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0
        le = int(row['line_number_end']) if pd.notna(row['line_number_end']) else ls

        # Primary key: (approp_id, chapter_year, fund)
        exec_lookup[(aid, cy, fund)] = (page, ls, le)
        # Fallback key: (approp_id, chapter_year)
        if (aid, cy) not in exec_lookup:
            exec_lookup[(aid, cy)] = (page, ls, le)

    # Also build a fallback lookup for items without approp_id
    # Key: (exec_page, chapter_year, reapprop_amount) -> (page, line_start, line_end)
    exec_by_page_amount = {}
    for _, row in exec_df.iterrows():
        page = int(row['page_number'])
        cy = int(row['chapter_year'])
        amt = int(row['reapprop_amount'])
        ls = int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0
        le = int(row['line_number_end']) if pd.notna(row['line_number_end']) else ls
        exec_by_page_amount[(page, cy, amt)] = (page, ls, le)

    # Build the mapping
    mapping = {}  # "page_linestart" -> [exec_page, exec_line_start, exec_line_end]

    for _, row in continued_df.iterrows():
        aid = clean_approp_id(row['approp_id'])
        cy = int(row['chapter_year'])
        fund = row['fund']
        enacted_page = int(row['page_number'])
        enacted_ls = int(row['line_number_start']) if pd.notna(row['line_number_start']) else 0

        # Look up exec location
        exec_loc = exec_lookup.get((aid, cy, fund)) or exec_lookup.get((aid, cy))

        # Fallback for items without approp_id: use exec_page + chapter_year + amount
        if exec_loc is None and pd.notna(row.get('exec_page')):
            exec_page = int(row['exec_page'])
            amt = int(row['reapprop_amount']) if pd.notna(row.get('exec_reapprop_amount')) else int(row['reapprop_amount'])
            exec_loc = exec_by_page_amount.get((exec_page, cy, amt))
            if exec_loc is None:
                # Last resort: use exec_page with no line number
                exec_loc = (exec_page, None, None)

        if exec_loc:
            key = f"{enacted_page}_{enacted_ls}"
            mapping[key] = list(exec_loc)

    return mapping


# =============================================================================
# ANCHOR MATCHING: ENACTED ANCHORS -> EXEC ANCHORS
# =============================================================================

def build_anchor_mapping(enacted_anchors, exec_anchors):
    """Map structural anchors from enacted to exec using amendment-aware keys.

    Primary key: (type, program, fund, chapter_year, amending_year, amendment_type)
    Fallback: (type, program, fund, chapter_year) for program/fund anchors and
              cases where the exact amendment variant doesn't match but the chapter
              year section exists.

    Returns:
        mapping: dict of key -> exec anchor dict
        exec_anchor_map: dict of key -> exec anchor (for lookup in _find_exec_position)
    """
    # Build exec lookup with amendment-aware keys
    exec_by_full_key = {}  # (type, program, fund, chyr, amend_year, amend_type) -> anchor
    exec_by_base_key = {}  # (type, program, fund, chyr) -> anchor (first match)

    for a in exec_anchors:
        atype = a['type']
        prog = a.get('program', '')
        fund = a.get('fund', '')
        chyr = a.get('chapter_year', 0)

        # Parse amendment info for chapter year anchors
        amend = parse_amendment_info(a.get('text', '')) if atype == 'chapter_year' else {}
        amend_year = amend.get('amending_year', 0)
        amend_type = amend.get('amendment_type', 'basic')

        full_key = (atype, prog, fund, chyr, amend_year, amend_type)
        base_key = (atype, prog, fund, chyr)

        exec_by_full_key[full_key] = a
        if base_key not in exec_by_base_key:
            exec_by_base_key[base_key] = a

    # Build mapping: try full key first, fallback to base key
    mapping = {}
    for a in enacted_anchors:
        atype = a['type']
        prog = a.get('program', '')
        fund = a.get('fund', '')
        chyr = a.get('chapter_year', 0)

        amend = parse_amendment_info(a.get('text', '')) if atype == 'chapter_year' else {}
        amend_year = amend.get('amending_year', 0)
        amend_type = amend.get('amendment_type', 'basic')

        full_key = (atype, prog, fund, chyr, amend_year, amend_type)
        base_key = (atype, prog, fund, chyr)

        # Try full key first (exact amendment match)
        if full_key in exec_by_full_key:
            mapping[base_key] = exec_by_full_key[full_key]
        elif base_key in exec_by_base_key:
            # Fallback: same chapter year exists in exec, different amendment variant
            mapping[base_key] = exec_by_base_key[base_key]

    # The returned exec_anchor_map uses base keys for compatibility with
    # _find_exec_position() which looks up by (type, program, fund, chapter_year)
    return mapping, exec_by_base_key


# =============================================================================
# INSERT GROUPING
# =============================================================================

def group_drops_into_inserts(enacted_elements, enacted_to_exec_map):
    """Group dropped items with undisbursed balances into inserts.

    Walk enacted elements in document order. Between each pair of "anchor points"
    (continued reapprops or structural headers), collect all undisbursed drops.

    An anchor point is:
      - A continued/modified reappropriation
      - A structural anchor (program, fund, chapter_year header)

    Fixes Bug 4: For isolated drops, finds nearest structural/continued anchors
    instead of self-referencing.
    """
    groups = []

    # Classify each element as an anchor or a potential drop.
    # CRITICAL: Only elements that exist in the executive budget are anchors.
    # Enacted-only structural headers are insert content, not group boundaries.
    def is_anchor(elem):
        """An anchor is a continued reapprop or a structural anchor with an exec counterpart."""
        if elem['type'] == 'reapprop':
            return elem.get('status') == 'continued_or_modified'
        # Structural anchor — only an anchor if it exists in exec
        return elem.get('has_exec_counterpart', False)

    # Walk elements in document order and collect drops between anchors
    current_anchor_above = None
    current_drops = []  # Undisbursed drops being accumulated
    current_zero_drops = []  # Zero-balance drops (for skip-line tracking)

    for elem in enacted_elements:
        if is_anchor(elem):
            # If we have accumulated drops, create a group
            if current_drops:
                groups.append({
                    'anchor_above': current_anchor_above,
                    'anchor_below': elem,
                    'anchor_below_type': _anchor_type_label(elem),
                    'undisbursed_drops': list(current_drops),
                    'zero_balance_drops': list(current_zero_drops),
                    'missing_chapter_headers': [],  # filled in later
                })
                current_drops = []
                current_zero_drops = []

            current_anchor_above = elem
        else:
            # This is a dropped reapprop
            if elem.get('has_undisbursed'):
                current_drops.append(elem)
            elif elem.get('status') == 'dropped':
                # Zero-balance drop — track for skip-line info
                if current_drops:
                    # If we already have undisbursed drops, this goes into the
                    # zero_balance list (between undisbursed items)
                    current_zero_drops.append(elem)
                else:
                    # No undisbursed drops yet — this is just a skipped item before
                    # the next real insert content. Don't start a group for it.
                    current_zero_drops.append(elem)

    # Handle trailing drops at end of document
    if current_drops:
        groups.append({
            'anchor_above': current_anchor_above,
            'anchor_below': None,  # End of document
            'anchor_below_type': 'end_of_document',
            'undisbursed_drops': list(current_drops),
            'zero_balance_drops': list(current_zero_drops),
            'missing_chapter_headers': [],
        })

    # Merge consecutive groups that would go to the same exec insertion point.
    # This happens when multiple dropped chapter years sit between the same pair
    # of continued reapprops (or structural anchors with exec presence).
    groups = _merge_adjacent_groups(groups, enacted_to_exec_map)

    # Split any groups that still contain items from different funds.
    # This can happen when the extractor assigns a different fund to an item
    # that sits within a fund section (e.g., cross-page items).
    groups = _split_mixed_fund_groups(groups)

    return groups


def _split_mixed_fund_groups(groups):
    """Split groups that contain undisbursed drops from different funds.

    When a group has items from multiple funds (e.g., due to extraction artifacts),
    split it into separate groups — one per fund — preserving the document order.
    Each sub-group inherits the original group's anchor_above/below.
    """
    result = []
    for g in groups:
        funds = set(d.get('fund', '') for d in g['undisbursed_drops'])
        if len(funds) <= 1:
            result.append(g)
            continue

        # Split by fund, preserving document order
        fund_drops = defaultdict(list)
        fund_zero = defaultdict(list)
        for d in g['undisbursed_drops']:
            fund_drops[d.get('fund', '')].append(d)
        for d in g.get('zero_balance_drops', []):
            fund_zero[d.get('fund', '')].append(d)

        # Create a sub-group for each fund
        for fund in sorted(fund_drops.keys(),
                           key=lambda f: min(d['sort_key'] for d in fund_drops[f])):
            result.append({
                'anchor_above': g['anchor_above'],
                'anchor_below': g['anchor_below'],
                'anchor_below_type': g.get('anchor_below_type', ''),
                'undisbursed_drops': fund_drops[fund],
                'zero_balance_drops': fund_zero.get(fund, []),
                'missing_chapter_headers': [
                    mh for mh in g.get('missing_chapter_headers', [])
                    if mh.get('fund', '') == fund
                ],
            })

    return result


def _merge_adjacent_groups(groups, enacted_to_exec_map):
    """Merge consecutive groups whose anchor_above and anchor_below share the same
    nearest continued-reapprop ancestors.

    Two groups are mergeable if:
    1. Group A's anchor_below is a structural anchor (not a continued reapprop)
    2. Group B's anchor_above is that same structural anchor (or a structural anchor
       that itself has no exec mapping)
    3. They share the same (program, fund) context

    After merging, the combined group uses A's anchor_above and B's anchor_below.
    """
    if not groups:
        return groups

    def has_exec_mapping(elem):
        """Check if an element has a direct exec mapping."""
        if elem is None:
            return False
        if elem['type'] == 'reapprop' and elem.get('status') == 'continued_or_modified':
            key = f"{elem['page']}_{elem['line_start']}"
            return key in enacted_to_exec_map
        return False

    merged = [groups[0]]

    for g in groups[1:]:
        prev = merged[-1]

        prev_below = prev.get('anchor_below')
        curr_above = g.get('anchor_above')

        can_merge = False

        # HARD RULE: Never merge across fund or program boundaries.
        # Fund/program anchors represent hierarchical section changes and must
        # always serve as insert boundaries — even if they exist in exec.
        crosses_boundary = False
        if prev_below is not None and prev_below.get('type') in ('anchor_fund', 'anchor_program'):
            crosses_boundary = True
        if curr_above is not None and curr_above.get('type') in ('anchor_fund', 'anchor_program'):
            crosses_boundary = True

        # Also check if any element BETWEEN prev's anchor_above and g's anchor_below
        # is a fund/program anchor (could have been skipped as a zero-balance boundary)
        if not crosses_boundary:
            # Check if the drops in prev and g have different funds
            prev_funds = set(d.get('fund', '') for d in prev.get('undisbursed_drops', []))
            curr_funds = set(d.get('fund', '') for d in g.get('undisbursed_drops', []))
            if prev_funds and curr_funds and prev_funds != curr_funds:
                crosses_boundary = True

        if not crosses_boundary:
            # Check if we can merge: prev's anchor_below is the same as g's anchor_above
            # AND that shared boundary is NOT a continued reapprop with exec mapping
            if (prev_below is not None and curr_above is not None and
                    prev_below.get('sort_key') == curr_above.get('sort_key')):
                # Same element as boundary — merge if it's not a mapped continued reapprop
                if not has_exec_mapping(prev_below):
                    can_merge = True

            # Also merge if the boundary anchor is a chapter_year header without exec presence
            if (prev_below is not None and curr_above is not None and
                    not can_merge and
                    prev_below.get('type', '').startswith('anchor_') and
                    curr_above.get('type', '').startswith('anchor_') and
                    not has_exec_mapping(prev_below)):
                # Check same program/fund context
                if (prev_below.get('program') == curr_above.get('program') and
                        prev_below.get('fund') == curr_above.get('fund')):
                    can_merge = True

        if can_merge:
            # Merge g into prev
            prev['anchor_below'] = g['anchor_below']
            prev['anchor_below_type'] = g.get('anchor_below_type', '')
            prev['undisbursed_drops'].extend(g['undisbursed_drops'])
            prev['zero_balance_drops'].extend(g['zero_balance_drops'])
            prev['missing_chapter_headers'].extend(g.get('missing_chapter_headers', []))
        else:
            merged.append(g)

    return merged


def _anchor_type_label(elem):
    """Get a descriptive label for an anchor element."""
    if elem['type'] == 'reapprop':
        return 'continued_reapprop'
    return elem.get('anchor_type', elem['type'])


# =============================================================================
# CHAPTER YEAR HEADER DETECTION
# =============================================================================

def compute_missing_header_placement(missing_header: dict, exec_ranges: dict,
                                      exec_elements: list) -> dict:
    """Determine where a missing chapter year header should be inserted in exec.

    Uses the hierarchical range model and chapter year ordering rules:
      1. ChYr 2025 is always first after a fund header — inserts go BELOW it
      2. Most recent years first (descending chapter year order)
      3. Non-amended before amended within the same chapter year
      4. Amended entries ordered by amending_year ascending

    Args:
        missing_header: dict with chapter_year, program, fund, amending_year, amendment_type
        exec_ranges: hierarchical range dict from build_hierarchical_ranges()
        exec_elements: sorted exec element list

    Returns:
        dict with insert_after_page, insert_after_line, placement_method
    """
    prog = missing_header['program']
    fund = missing_header['fund']
    target_chyr = missing_header['chapter_year']
    target_amend_year = missing_header.get('amending_year', 0)
    target_is_amended = missing_header.get('is_amended', target_amend_year > 0)

    prog_data = exec_ranges.get(prog)
    if not prog_data:
        return {'insert_after_page': None, 'insert_after_line': None,
                'placement_method': 'no_program_in_exec'}

    fund_data = prog_data['funds'].get(fund)
    if not fund_data:
        # Fund doesn't exist in exec — graduate to program level.
        # Place after the last fund section in this program.
        last_fund_end = (0, 0)
        for fd in prog_data['funds'].values():
            fend = (fd['range'][2], fd['range'][3])
            if fend > last_fund_end:
                last_fund_end = fend
        if last_fund_end > (0, 0):
            return {'insert_after_page': last_fund_end[0],
                    'insert_after_line': last_fund_end[1],
                    'placement_method': 'graduated_to_program'}
        return {'insert_after_page': prog_data['range'][2],
                'insert_after_line': prog_data['range'][3],
                'placement_method': 'graduated_to_program'}

    # Fund exists in exec — find the right position within it
    chyr_sections = fund_data['chapter_years']
    if not chyr_sections:
        # Fund exists but has no chapter year sections — place after fund header
        return {'insert_after_page': fund_data['range'][0],
                'insert_after_line': fund_data['range'][1],
                'placement_method': 'after_fund_header'}

    # Walk chapter year sections in document order.
    # Ordering rule: 2025 first, then descending by chapter year,
    # non-amended before amended within same year.
    #
    # Our missing header should go AFTER the last chapter year section that is
    # "more recent" or "higher priority" than the missing one.
    # Priority: higher chapter year > same chapter year non-amended > same year lower amending year.

    predecessor = None  # Last section that should come BEFORE our missing header

    for cy_sec in chyr_sections:
        cy = cy_sec['chapter_year']
        cy_amended = cy_sec['is_amended']
        cy_amend_year = cy_sec['amending_year']

        if cy > target_chyr:
            # This section is more recent — our insert goes after it
            predecessor = cy_sec
        elif cy == target_chyr:
            if not target_is_amended and not cy_amended:
                # Same year, both non-amended — shouldn't happen (it'd be a duplicate)
                predecessor = cy_sec
            elif not target_is_amended and cy_amended:
                # We're non-amended, this is amended — we go BEFORE it (non-amended first)
                break
            elif target_is_amended and not cy_amended:
                # We're amended, this is non-amended — we go after it
                predecessor = cy_sec
            elif target_is_amended and cy_amended:
                # Both amended — compare amending years
                if cy_amend_year < target_amend_year:
                    predecessor = cy_sec
                else:
                    break
        else:
            # cy < target_chyr — this section is older, our insert goes before it
            break

    if predecessor:
        # Insert after predecessor's range end
        return {'insert_after_page': predecessor['range'][2],
                'insert_after_line': predecessor['range'][3],
                'placement_method': 'between_chyrs'}
    else:
        # No predecessor found — insert at very start of fund section (after header)
        # But never before a 2025 section
        if chyr_sections and chyr_sections[0]['chapter_year'] == 2025:
            # Place after the 2025 section
            return {'insert_after_page': chyr_sections[0]['range'][2],
                    'insert_after_line': chyr_sections[0]['range'][3],
                    'placement_method': 'after_2025_section'}
        return {'insert_after_page': fund_data['range'][0],
                'insert_after_line': fund_data['range'][1],
                'placement_method': 'after_fund_header'}


def detect_missing_chapter_headers(groups, enacted_elements, exec_anchors, exec_ranges=None):
    """For each insert group, check if chapter year headers need to be included.

    A chapter year header is "missing" from the exec budget if:
    1. The dropped item's chapter_year section header exists in the enacted budget
    2. But does NOT exist in the executive budget for the same (program, fund, chapter_year)

    This means the insert must include the header text.

    If exec_ranges is provided, also computes precise placement for each missing header.
    """
    # Build set of (program, fund, chapter_year) that exist in exec
    exec_chyr_set = set()
    for a in exec_anchors:
        if a['type'] == 'chapter_year':
            exec_chyr_set.add((a['program'], a['fund'], a['chapter_year']))

    # Build lookup for enacted chapter_year anchor text and amendment info
    enacted_chyr_lookup = {}
    for elem in enacted_elements:
        if elem['type'] == 'anchor_chapter_year':
            key = (elem['program'], elem['fund'], elem['chapter_year'])
            enacted_chyr_lookup[key] = {
                'text': elem.get('text', ''),
                'amending_year': elem.get('amending_year', 0),
                'amendment_type': elem.get('amendment_type', 'basic'),
                'is_amended': elem.get('is_amended', False),
            }

    for group in groups:
        missing_headers = []
        seen_chyrs = set()

        for drop in group['undisbursed_drops']:
            chyr_key = (drop['program'], drop['fund'], drop['chapter_year'])
            if chyr_key in seen_chyrs:
                continue
            seen_chyrs.add(chyr_key)

            if chyr_key not in exec_chyr_set:
                # This chapter year section doesn't exist in exec — header needed
                enacted_info = enacted_chyr_lookup.get(chyr_key, {})
                citation = enacted_info.get('text', drop.get('chapter_citation', ''))

                mh = {
                    'chapter_year': drop['chapter_year'],
                    'program': drop['program'],
                    'fund': drop['fund'],
                    'citation': citation,
                    'amending_year': enacted_info.get('amending_year', 0),
                    'amendment_type': enacted_info.get('amendment_type', 'basic'),
                    'is_amended': enacted_info.get('is_amended', False),
                }

                # Compute precise exec placement if ranges available
                if exec_ranges:
                    placement = compute_missing_header_placement(
                        mh, exec_ranges, []  # exec_elements not needed for range-based placement
                    )
                    mh['exec_placement'] = placement

                missing_headers.append(mh)

        group['missing_chapter_headers'] = missing_headers


# =============================================================================
# EXEC LOCATION MAPPING
# =============================================================================

def map_groups_to_exec(groups, enacted_to_exec_map, enacted_elements, exec_elements,
                       enacted_anchor_to_exec_map, exec_anchor_map):
    """Map each insert group's enacted anchors to exec page/line positions.

    Fixes Bug 3: exec_below found by sequential document order, not chapter_year match.
    Fixes Bug 5: Falls back to next fund/program header when no exec_below in section.

    Step 6 integration: When exec_above is None and the group has missing chapter
    year headers with computed exec placements, uses the placement data instead of
    the generic _find_prev_exec_element_before() fallback.
    """
    # Build a sorted list of ALL exec elements for sequential lookup
    exec_sorted = sorted(exec_elements, key=lambda e: e['sort_key'])

    mapped_groups = []
    insert_id = 0

    for group in groups:
        insert_id += 1
        anchor_above = group['anchor_above']
        anchor_below = group['anchor_below']

        # Map enacted_above to exec
        exec_above = _find_exec_position(anchor_above, enacted_to_exec_map,
                                         enacted_anchor_to_exec_map)

        # Map enacted_below to exec
        if anchor_below is None:
            # End of document — use last exec element
            exec_below = {
                'type': 'end_of_document',
                'page': exec_sorted[-1]['page'],
                'line_start': exec_sorted[-1].get('line_end', exec_sorted[-1]['line_start']),
                'desc': 'end of document',
            }
        else:
            exec_below = _find_exec_position(anchor_below, enacted_to_exec_map,
                                             enacted_anchor_to_exec_map)

        # Validate: exec_below should be AFTER exec_above (Bug 3 fix)
        if exec_above and exec_below:
            above_pos = (exec_above.get('page', 0), exec_above.get('line_start', 0) or 0)
            below_pos = (exec_below.get('page', 0), exec_below.get('line_start', 0) or 0)

            if below_pos <= above_pos and exec_below.get('type') != 'end_of_document':
                # Bug 3: exec_below is before or at exec_above — find next sequential
                exec_below = _find_next_exec_element_after(above_pos, exec_sorted)

        # Bug 5: If exec_below is still None or sentinel, find next structural anchor
        if exec_below is None or exec_below.get('page', 0) >= 999:
            if exec_above:
                above_pos = (exec_above.get('page', 0), exec_above.get('line_start', 0) or 0)
                exec_below = _find_next_exec_element_after(above_pos, exec_sorted)

            if exec_below is None:
                # Absolute fallback: last element in exec
                last = exec_sorted[-1]
                exec_below = {
                    'type': 'end_of_document',
                    'page': last['page'],
                    'line_start': last.get('line_end', last['line_start']),
                    'desc': 'end of document (fallback)',
                }

        # If exec_above is None, try to use missing header placement data.
        # This is more precise than the generic _find_prev_exec_element_before()
        # because it respects chapter year ordering rules within fund sections.
        if exec_above is None:
            placement = _get_best_missing_header_placement(group)
            if placement and placement.get('insert_after_page') is not None:
                exec_above = {
                    'type': 'missing_header_placement',
                    'page': placement['insert_after_page'],
                    'line_start': placement['insert_after_line'],
                    'line_end': placement['insert_after_line'],
                    'desc': f"placement: {placement['placement_method']}",
                }
                # Also derive exec_below from the placement if exec_below is None
                if exec_below is None or exec_below.get('type') == 'end_of_document':
                    above_pos = (placement['insert_after_page'],
                                 placement['insert_after_line'])
                    next_elem = _find_next_exec_element_after(above_pos, exec_sorted)
                    if next_elem:
                        exec_below = next_elem

        # Final fallback: if exec_above is still None, use generic prev-element search
        if exec_above is None and exec_below:
            below_pos = (exec_below.get('page', 0), exec_below.get('line_start', 0) or 0)
            exec_above = _find_prev_exec_element_before(below_pos, exec_sorted)

        mapped_groups.append({
            'insert_id': insert_id,
            'num_undisbursed': len(group['undisbursed_drops']),
            'num_zero_balance': len(group['zero_balance_drops']),
            'num_missing_headers': len(group['missing_chapter_headers']),
            'enacted_above': anchor_above,
            'enacted_below': anchor_below,
            'exec_above': exec_above,
            'exec_below': exec_below,
            'undisbursed_drops': group['undisbursed_drops'],
            'zero_balance_drops': group['zero_balance_drops'],
            'missing_chapter_headers': group['missing_chapter_headers'],
        })

    return mapped_groups


def _get_best_missing_header_placement(group):
    """Get the best exec placement from a group's missing chapter year headers.

    When a group has multiple missing headers, pick the one with the most precise
    placement (between_chyrs > after_2025_section > after_fund_header > graduated).
    Among equally precise placements, pick the one that would go first in document
    order (earliest page/line), since the insert should start there.
    """
    placements = []
    for mh in group.get('missing_chapter_headers', []):
        p = mh.get('exec_placement')
        if p and p.get('insert_after_page') is not None:
            placements.append(p)

    if not placements:
        return None

    # Rank by precision: between_chyrs is most precise
    method_rank = {
        'between_chyrs': 0,
        'after_2025_section': 1,
        'after_fund_header': 2,
        'graduated_to_program': 3,
        'no_program_in_exec': 4,
    }

    placements.sort(key=lambda p: (
        method_rank.get(p.get('placement_method', ''), 99),
        p['insert_after_page'],
        p['insert_after_line'] or 0,
    ))

    return placements[0]


def _find_exec_position(enacted_elem, enacted_to_exec_map, enacted_anchor_to_exec_map):
    """Find the exec page/line for an enacted element.

    Tries:
    1. enacted_to_exec_map lookup (for continued reapprops)
    2. enacted_anchor_to_exec_map lookup (for structural anchors)
    """
    if enacted_elem is None:
        return None

    if enacted_elem['type'] == 'reapprop':
        # Look up in continued mapping
        key = f"{enacted_elem['page']}_{enacted_elem['line_start']}"
        if key in enacted_to_exec_map:
            exec_loc = enacted_to_exec_map[key]
            return {
                'type': 'continued_reapprop',
                'page': exec_loc[0],
                'line_start': exec_loc[1],
                'line_end': exec_loc[2] if len(exec_loc) > 2 else exec_loc[1],
                'desc': f"ID={enacted_elem.get('approp_id')} ChYr={enacted_elem.get('chapter_year')}",
            }
        # Fallback: try by approp_id + chapter_year in the anchor map
        return None

    else:
        # Structural anchor — look up by (type, program, fund, chapter_year)
        anchor_type = enacted_elem.get('anchor_type', enacted_elem['type'].replace('anchor_', ''))
        key = (anchor_type, enacted_elem.get('program', ''),
               enacted_elem.get('fund', ''), enacted_elem.get('chapter_year', 0))
        if key in enacted_anchor_to_exec_map:
            exec_anchor = enacted_anchor_to_exec_map[key]
            return {
                'type': f'anchor_{anchor_type}',
                'page': exec_anchor['page'],
                'line_start': exec_anchor['line_start'],
                'line_end': exec_anchor.get('line_end', exec_anchor['line_start']),
                'desc': exec_anchor.get('text', f'{anchor_type}'),
            }
        return None


def _find_next_exec_element_after(position, exec_sorted):
    """Find the first exec element strictly after the given (page, line) position.

    This is the Bug 3 fix — sequential document order instead of chapter_year match.
    """
    for elem in exec_sorted:
        elem_pos = (elem['page'], elem.get('line_start', 0))
        if elem_pos > position:
            desc = ''
            if elem['type'] == 'reapprop':
                desc = f"ID={elem.get('approp_id')} ChYr={elem.get('chapter_year')}"
            else:
                desc = elem.get('text', elem['type'])[:60]
            return {
                'type': elem['type'],
                'page': elem['page'],
                'line_start': elem.get('line_start', 0),
                'line_end': elem.get('line_end', elem.get('line_start', 0)),
                'desc': desc,
            }
    return None


def _find_prev_exec_element_before(position, exec_sorted):
    """Find the last exec element strictly before the given (page, line) position.

    Used when exec_above is None — finds the nearest exec element before exec_below.
    """
    best = None
    for elem in exec_sorted:
        elem_pos = (elem['page'], elem.get('line_start', 0))
        if elem_pos < position:
            best = elem
        else:
            break  # sorted, so we can stop
    if best:
        desc = ''
        if best['type'] == 'reapprop':
            desc = f"ID={best.get('approp_id')} ChYr={best.get('chapter_year')}"
        else:
            desc = best.get('text', best['type'])[:60]
        return {
            'type': best['type'],
            'page': best['page'],
            'line_start': best.get('line_start', 0),
            'line_end': best.get('line_end', best.get('line_start', 0)),
            'desc': desc,
        }
    return None


# =============================================================================
# EXCEL OUTPUT GENERATION
# =============================================================================

def generate_insert_lookup(mapped_groups, enacted_elements, exec_anchors):
    """Generate INSERT_LOOKUP.xlsx with 4 sheets."""
    inserts_rows = []
    details_rows = []
    chyr_sections = []
    missing_headers = []

    for g in mapped_groups:
        iid = g['insert_id']
        exec_above = g.get('exec_above') or {}
        exec_below = g.get('exec_below') or {}

        # Build chapter_years list
        chyrs = sorted(set(d['chapter_year'] for d in g['undisbursed_drops']))
        chyrs_str = ', '.join(str(c) for c in chyrs)

        # Compute total undisbursed balance
        total_balance = sum(d.get('sfs_balance', 0) for d in g['undisbursed_drops'])

        # Enacted page/line range
        all_drops = g['undisbursed_drops']
        if all_drops:
            enacted_pages = _format_enacted_range(all_drops)
        else:
            enacted_pages = ''

        # Build insert label
        exec_page = exec_above.get('page', '?')
        # Label: "Insert {exec_page} {letter}" where letter is assigned later
        label = f"Insert {exec_page}"

        # Skip lines (zero-balance drops within the insert range)
        skip_lines = []
        for zd in g['zero_balance_drops']:
            skip_lines.append(f"p{zd['page']} L{zd['line_start']}-{zd['line_end']}")
        skip_str = ', '.join(skip_lines) if skip_lines else None

        # Chapter headers to include
        header_strs = []
        for mh in g['missing_chapter_headers']:
            header_strs.append(mh['citation'])
        headers_str = ' | '.join(header_strs) if header_strs else None

        inserts_rows.append({
            'insert_id': iid,
            'insert_label': label,
            'program': all_drops[0]['program'] if all_drops else '',
            'fund': all_drops[0]['fund'] if all_drops else '',
            'chapter_years': chyrs_str,
            'num_undisbursed_drops': len(all_drops),
            'total_undisbursed_balance': total_balance,
            'exec_insert_after_page': exec_above.get('page'),
            'exec_insert_after_line': exec_above.get('line_end', exec_above.get('line_start')),
            'exec_insert_before_page': exec_below.get('page'),
            'exec_insert_before_line': exec_below.get('line_start'),
            'enacted_page_range': enacted_pages,
            'zero_balance_lines_to_skip': skip_str,
            'chapter_headers_to_include': headers_str,
            'num_missing_headers': len(g['missing_chapter_headers']),
        })

        # Detail rows
        for drop in all_drops:
            details_rows.append({
                'insert_id': iid,
                'insert_label': label,
                'approp_id': drop.get('approp_id'),
                'chapter_year': drop['chapter_year'],
                'program': drop['program'],
                'fund': drop['fund'],
                'reapprop_amount': drop['reapprop_amount'],
                'approp_amount': drop.get('approp_amount'),
                'sfs_balance': drop.get('sfs_balance', 0),
                'sfs_rounded': _round_sfs(drop.get('sfs_balance', 0)),
                'enacted_page': drop['page'],
                'enacted_line_start': drop['line_start'],
                'enacted_line_end': drop['line_end'],
                'bill_language': drop.get('bill_language', ''),
            })

        # Missing headers (with exec placement data from Step 6)
        for mh in g['missing_chapter_headers']:
            placement = mh.get('exec_placement', {})
            missing_headers.append({
                'insert_id': iid,
                'insert_label': label,
                'type': 'chapter_year_header',
                'program': mh.get('program', ''),
                'chapter_year': mh['chapter_year'],
                'fund': mh['fund'],
                'is_amended': mh.get('is_amended', False),
                'amending_year': mh.get('amending_year', 0),
                'amendment_type': mh.get('amendment_type', 'basic'),
                'citation': mh['citation'],
                'exec_insert_after_page': placement.get('insert_after_page'),
                'exec_insert_after_line': placement.get('insert_after_line'),
                'placement_method': placement.get('placement_method', ''),
            })

    # Build chapter year sections from enacted anchors
    for elem in enacted_elements:
        if elem['type'] == 'anchor_chapter_year':
            chyr_sections.append({
                'program': elem['program'],
                'fund': elem['fund'],
                'chapter_year': elem['chapter_year'],
                'page': elem['page'],
                'line_start': elem['line_start'],
                'line_end': elem.get('line_end', elem['line_start']),
            })

    # Assign letter suffixes to labels (multiple inserts on same exec page)
    _assign_insert_labels(inserts_rows, details_rows, missing_headers)

    # Build exec fund/chapter_year ranges from exec_elements
    exec_fund_ranges = _build_exec_fund_ranges(exec_anchors)

    # Write Excel
    output_path = DROPS_DIR / "INSERT_LOOKUP.xlsx"
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(inserts_rows).to_excel(writer, sheet_name='INSERTS', index=False)
        pd.DataFrame(details_rows).to_excel(writer, sheet_name='INSERT DETAILS', index=False)
        pd.DataFrame(chyr_sections).to_excel(writer, sheet_name='CHAPTER YEAR SECTIONS', index=False)
        if missing_headers:
            pd.DataFrame(missing_headers).to_excel(writer, sheet_name='MISSING HEADERS', index=False)
        pd.DataFrame(exec_fund_ranges).to_excel(writer, sheet_name='EXEC FUND RANGES', index=False)

    print(f"  INSERT_LOOKUP.xlsx: {len(inserts_rows)} inserts, {len(details_rows)} detail rows, {len(exec_fund_ranges)} exec fund ranges")
    return inserts_rows, details_rows


def _assign_insert_labels(inserts_rows, details_rows, missing_headers):
    """Assign letter suffixes to inserts on the same exec page (e.g., 'Insert 268 A', 'Insert 268 B')."""
    page_counts = defaultdict(int)
    page_labels = {}

    for row in inserts_rows:
        page = row.get('exec_insert_after_page', '?')
        page_counts[page] += 1

    # Assign letters
    page_current = defaultdict(int)
    for row in inserts_rows:
        page = row.get('exec_insert_after_page', '?')
        if page_counts[page] > 1:
            idx = page_current[page]
            letter = chr(65 + idx)  # A, B, C...
            page_current[page] += 1
            row['insert_label'] = f"Insert {page} {letter}"
        else:
            row['insert_label'] = f"Insert {page}"

        # Update detail and missing header rows
        iid = row['insert_id']
        for d in details_rows:
            if d['insert_id'] == iid:
                d['insert_label'] = row['insert_label']
        for m in missing_headers:
            if m['insert_id'] == iid:
                m['insert_label'] = row['insert_label']


def generate_insert_editor(inserts_rows, details_rows):
    """Generate INSERT_EDITOR.xlsx — the human-facing editor worksheet.

    Includes a Status column for tracking progress and a MANUAL INSERTS sheet
    documenting inserts already placed in the LBDC PDF Editor.
    """
    editor_rows = []

    # Manual inserts already placed in the LBDC PDF Editor.
    # These were entered during the page-by-page review and may include items
    # not in our automated extraction (e.g., bare-amount items without approp IDs).
    # Format: (label, exec_page, after_line, notes)
    manual_inserts = [
        ('268A', 268, 11, ''),
        ('268B', 268, 24, ''),
        ('272A', 272, 7, ''),
        ('273A', 273, 4, 'plus one extra space'),
        ('273B', 273, 5, 'plus one extra space, immediately after 273A'),
        ('273C', 273, 7, 'plus one extra space'),
        ('275C', 275, 16, ''),
        ('275A', 275, None, ''),
        ('281*', 281, None, 'untouched (last on page)'),
        ('290E', 290, None, ''),
    ]

    # Build manual insert lookup for matching: (page, after_line) -> label
    manual_by_page_line = {}
    manual_by_page = defaultdict(list)
    for label, pg, line, notes in manual_inserts:
        if line is not None:
            manual_by_page_line[(pg, line)] = label
        manual_by_page[pg].append((label, line, notes))

    for ins in inserts_rows:
        iid = ins['insert_id']
        label = ins['insert_label']

        # Get details for this insert
        details = [d for d in details_rows if d['insert_id'] == iid]

        # Build reapprops summary
        reapprop_lines = []
        for d in details:
            line = (f"ID {d['approp_id'] or '?'} ChYr {d['chapter_year']}  "
                    f"p{d['enacted_page']} L{d['enacted_line_start']}-{d['enacted_line_end']}  "
                    f"re.${d['reapprop_amount']:,.0f}  (SFS ${d['sfs_balance']:,.0f})")
            reapprop_lines.append(line)

        # Check if this pipeline insert matches a manual insert
        exec_pg = ins['exec_insert_after_page']
        exec_ln = ins['exec_insert_after_line']
        manual_match = manual_by_page_line.get((exec_pg, exec_ln))
        if manual_match is None and exec_pg in manual_by_page:
            # Fuzzy match: same page, check if any manual insert is within 2 lines
            for m_label, m_line, m_notes in manual_by_page[exec_pg]:
                if m_line is not None and abs(m_line - (exec_ln or 0)) <= 2:
                    manual_match = f"{m_label}?"  # question mark = approximate match
                    break

        editor_rows.append({
            'Insert': label,
            'Status': '',  # User fills in: done, skip, pending, etc.
            '26-27 Page': exec_pg,
            '26-27 After Line': exec_ln,
            'Manual Insert': manual_match or '',
            '25-26 Source': ins['enacted_page_range'],
            'Drops': ins['num_undisbursed_drops'],
            'Include Headers': ins['chapter_headers_to_include'],
            'Skip Lines': ins['zero_balance_lines_to_skip'],
            'Reapprops in Insert': '\n'.join(reapprop_lines),
        })

    # Build manual inserts reference sheet
    manual_rows = []
    for label, pg, line, notes in manual_inserts:
        # Find pipeline inserts on same page
        pipeline_matches = [r['Insert'] for r in editor_rows if r['26-27 Page'] == pg]
        manual_rows.append({
            'Manual Label': label,
            '26-27 Page': pg,
            'After Line': line,
            'Notes': notes if notes else None,
            'Pipeline Inserts on Page': ', '.join(pipeline_matches) if pipeline_matches else 'NONE',
        })

    output_path = DROPS_DIR / "INSERT_EDITOR.xlsx"
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(editor_rows).to_excel(writer, sheet_name='INSERTS', index=False)
        pd.DataFrame(manual_rows).to_excel(writer, sheet_name='MANUAL INSERTS', index=False)
    print(f"  INSERT_EDITOR.xlsx: {len(editor_rows)} inserts, {len(manual_rows)} manual inserts tracked")


def _is_nonfederal_fund(fund_str: str) -> bool:
    """Check if a fund is non-federal (General Fund or Special Revenue Funds - Other)."""
    fl = fund_str.lower()
    return 'general fund' in fl or ('special revenue' in fl and 'other' not in fl) or \
           any(keyword in fl for keyword in [
               'general fund',
               'vesid social security',
               'local government records',
               'vocational rehabilitation',
           ]) or ('fund' in fl and 'federal' not in fl)


def generate_insert_editor_nonfederal(inserts_rows, details_rows):
    """Generate INSERT_EDITOR_NONFEDERAL.xlsx — only General Fund and SRF-Other inserts.

    Filters to inserts where ALL drops come from non-federal funds:
      - General Fund; Local Assistance Account
      - Miscellaneous Special Revenue Fund; VESID Social Security Account
      - NY Local Government Records Management Improvement Fund
      - Vocational Rehabilitation Fund; Vocational Rehabilitation Account

    Re-labels inserts with sequential numbering within the filtered set.
    """
    # Filter inserts_rows to only those whose drops are all non-federal
    # First, group details by insert_id and check fund
    insert_funds = defaultdict(set)
    for d in details_rows:
        insert_funds[d['insert_id']].add(d.get('fund', ''))

    nonfed_ids = set()
    for iid, funds in insert_funds.items():
        if all('federal' not in f.lower() for f in funds):
            nonfed_ids.add(iid)

    # Filter to non-federal inserts only
    nf_inserts = [r for r in inserts_rows if r['insert_id'] in nonfed_ids]
    nf_details = [d for d in details_rows if d['insert_id'] in nonfed_ids]

    if not nf_inserts:
        print(f"  INSERT_EDITOR_NONFEDERAL.xlsx: 0 inserts (no non-federal drops)")
        return

    # Re-assign insert labels for the filtered set (letter suffixes may change
    # since we have fewer inserts per page now)
    page_counts = defaultdict(int)
    for row in nf_inserts:
        page = row.get('exec_insert_after_page', '?')
        page_counts[page] += 1

    page_current = defaultdict(int)
    for row in nf_inserts:
        page = row.get('exec_insert_after_page', '?')
        if page_counts[page] > 1:
            idx = page_current[page]
            letter = chr(65 + idx)
            page_current[page] += 1
            row['_nf_label'] = f"Insert {page} {letter}"
        else:
            row['_nf_label'] = f"Insert {page}"

    # Manual inserts (same as main editor)
    manual_inserts = [
        ('268A', 268, 11, ''),
        ('268B', 268, 24, ''),
        ('272A', 272, 7, ''),
        ('273A', 273, 4, 'plus one extra space'),
        ('273B', 273, 5, 'plus one extra space, immediately after 273A'),
        ('273C', 273, 7, 'plus one extra space'),
        ('275C', 275, 16, ''),
        ('275A', 275, None, ''),
        ('281*', 281, None, 'untouched (last on page)'),
        ('290E', 290, None, ''),
    ]

    manual_by_page_line = {}
    manual_by_page = defaultdict(list)
    for label, pg, line, notes in manual_inserts:
        if line is not None:
            manual_by_page_line[(pg, line)] = label
        manual_by_page[pg].append((label, line, notes))

    editor_rows = []
    for ins in nf_inserts:
        iid = ins['insert_id']
        label = ins.get('_nf_label', ins['insert_label'])

        details = [d for d in nf_details if d['insert_id'] == iid]

        reapprop_lines = []
        for d in details:
            line = (f"ID {d['approp_id'] or '?'} ChYr {d['chapter_year']}  "
                    f"p{d['enacted_page']} L{d['enacted_line_start']}-{d['enacted_line_end']}  "
                    f"re.${d['reapprop_amount']:,.0f}  (SFS ${d['sfs_balance']:,.0f})")
            reapprop_lines.append(line)

        exec_pg = ins['exec_insert_after_page']
        exec_ln = ins['exec_insert_after_line']
        manual_match = manual_by_page_line.get((exec_pg, exec_ln))
        if manual_match is None and exec_pg in manual_by_page:
            for m_label, m_line, m_notes in manual_by_page[exec_pg]:
                if m_line is not None and abs(m_line - (exec_ln or 0)) <= 2:
                    manual_match = f"{m_label}?"
                    break

        editor_rows.append({
            'Insert': label,
            'Status': '',
            '26-27 Page': exec_pg,
            '26-27 After Line': exec_ln,
            'Manual Insert': manual_match or '',
            'Fund': ins.get('fund', ''),
            '25-26 Source': ins['enacted_page_range'],
            'Drops': ins['num_undisbursed_drops'],
            'Include Headers': ins['chapter_headers_to_include'],
            'Skip Lines': ins['zero_balance_lines_to_skip'],
            'Reapprops in Insert': '\n'.join(reapprop_lines),
        })

    # Manual inserts reference (same as main editor)
    manual_rows = []
    for label, pg, line, notes in manual_inserts:
        pipeline_matches = [r['Insert'] for r in editor_rows if r['26-27 Page'] == pg]
        manual_rows.append({
            'Manual Label': label,
            '26-27 Page': pg,
            'After Line': line,
            'Notes': notes if notes else None,
            'Pipeline Inserts on Page': ', '.join(pipeline_matches) if pipeline_matches else 'NONE',
        })

    nf_drops = sum(r['Drops'] for r in editor_rows)
    output_path = DROPS_DIR / "INSERT_EDITOR_NONFEDERAL.xlsx"
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(editor_rows).to_excel(writer, sheet_name='INSERTS', index=False)
        pd.DataFrame(manual_rows).to_excel(writer, sheet_name='MANUAL INSERTS', index=False)
    print(f"  INSERT_EDITOR_NONFEDERAL.xlsx: {len(editor_rows)} inserts ({nf_drops} drops), {len(manual_rows)} manual inserts tracked")


def _format_enacted_range(drops):
    """Format a list of drops into an enacted page/line range string."""
    parts = []
    for d in drops:
        parts.append(f"p{d['page']} L{d['line_start']}-{d['line_end']}")
    return ', '.join(parts)


def _round_sfs(balance):
    """Round SFS balance for display."""
    if balance == 0:
        return '$0'
    if balance < 1000:
        return f'${balance:,.0f}'
    return f'${round(balance / 1000) * 1000:,.0f}'


def _build_exec_fund_ranges(exec_anchors):
    """Build a reference table of exec fund/chapter_year page ranges.

    For each (program, fund) in the exec budget, shows the page/line range of
    the fund section and each chapter_year subsection within it.
    """
    rows = []

    # Walk exec anchors in order to build ranges
    # Fund anchors define the start; the next fund/program anchor defines the end
    sorted_anchors = sorted(exec_anchors, key=lambda a: (a['page'], a['line_start']))

    # First pass: build fund section ranges
    fund_sections = []
    for i, a in enumerate(sorted_anchors):
        if a['type'] == 'fund':
            # Find end: next fund or program anchor
            end_page = 999
            end_line = 0
            for j in range(i + 1, len(sorted_anchors)):
                if sorted_anchors[j]['type'] in ('fund', 'program'):
                    end_page = sorted_anchors[j]['page']
                    end_line = sorted_anchors[j]['line_start'] - 1
                    break
            fund_sections.append({
                'program': a.get('program', ''),
                'fund': a.get('fund', ''),
                'start_page': a['page'],
                'start_line': a['line_start'],
                'end_page': end_page,
                'end_line': end_line,
            })

    # Second pass: build chapter_year ranges within each fund section
    for fs in fund_sections:
        # Collect chapter_year anchors within this fund section
        chyr_anchors = []
        for a in sorted_anchors:
            if (a['type'] == 'chapter_year' and
                    a.get('program') == fs['program'] and
                    a.get('fund') == fs['fund'] and
                    (a['page'], a['line_start']) >= (fs['start_page'], fs['start_line']) and
                    (a['page'], a['line_start']) <= (fs['end_page'], fs.get('end_line', 999))):
                chyr_anchors.append(a)

        # Add fund-level row
        rows.append({
            'program': fs['program'],
            'fund': fs['fund'],
            'chapter_year': 'ALL',
            'start_page': fs['start_page'],
            'start_line': fs['start_line'],
            'end_page': fs['end_page'],
            'end_line': fs['end_line'],
        })

        # Add chapter_year rows
        for k, ca in enumerate(chyr_anchors):
            # End of this chapter_year: next chapter_year start or fund end
            if k + 1 < len(chyr_anchors):
                cy_end_page = chyr_anchors[k + 1]['page']
                cy_end_line = chyr_anchors[k + 1]['line_start'] - 1
            else:
                cy_end_page = fs['end_page']
                cy_end_line = fs['end_line']

            rows.append({
                'program': fs['program'],
                'fund': fs['fund'],
                'chapter_year': ca.get('chapter_year', ca.get('year', 0)),
                'start_page': ca['page'],
                'start_line': ca['line_start'],
                'end_page': cy_end_page,
                'end_line': cy_end_line,
            })

    return rows


# =============================================================================
# DROPS OUTPUT
# =============================================================================

def generate_drops_output(dropped_df):
    """Generate ALL_DROPS.xlsx and per-program/fund CSVs in Drops/ directory."""
    DROPS_DIR.mkdir(exist_ok=True)

    # Program short names
    program_map = {
        'ADULT CAREER AND CONTINUING EDUCATION SERVICES PROGRAM': 'ACCES',
        'CULTURAL EDUCATION PROGRAM': 'CULTURAL ED',
        'OFFICE OF HIGHER EDUCATION AND THE PROFESSIONS PROGRAM': 'HIGHER ED',
        'OFFICE OF PREKINDERGARTEN THROUGH GRADE TWELVE EDUCATION PROGRAM': 'PREK-12',
    }

    # Parse fund into components
    def parse_fund(fund_str):
        parts = str(fund_str).split('; ')
        fund_type = parts[0] if len(parts) >= 1 else ''
        fund_name = parts[1] if len(parts) >= 3 else None
        account = parts[-1] if len(parts) >= 2 else parts[0]
        return fund_type, fund_name, account

    # Add parsed columns
    df = dropped_df.copy()
    parsed = df['fund'].apply(parse_fund)
    df['fund_type'] = [p[0] for p in parsed]
    df['fund_name'] = [p[1] for p in parsed]
    df['account'] = [p[2] for p in parsed]

    # Rename columns
    df = df.rename(columns={
        'page_number': 'enacted_page',
        'line_number_start': 'enacted_line_start',
        'line_number_end': 'enacted_line_end',
    })

    # Build composite key
    df['composite_key'] = df.apply(
        lambda r: f"EDUCATION DEPARTMENT|{r['approp_id'] or ''}|{r['chapter_year']}|{r.get('approp_amount', '')}",
        axis=1
    )

    # Column order for CSV output
    csv_cols = ['program', 'fund_type', 'fund_name', 'account', 'chapter_year',
                'approp_id', 'approp_amount', 'reapprop_amount', 'composite_key',
                'sfs_undisbursed_balance', 'bill_language', 'chapter_citation',
                'enacted_page', 'enacted_line_start', 'enacted_line_end']

    # Write per-program files
    summary_rows = []
    for program, short_name in program_map.items():
        prog_df = df[df['program'] == program]
        if len(prog_df) == 0:
            continue

        prog_dir = DROPS_DIR / short_name
        prog_dir.mkdir(exist_ok=True)

        # Write _ALL_ file
        prog_df[csv_cols].to_csv(prog_dir / f"_ALL_{short_name}.csv", index=False)

        # Write per-account files
        for account, acc_df in prog_df.groupby('account'):
            safe_name = account.replace(' ', '_').replace('-', '_').replace('/', '_')
            acc_df[csv_cols].to_csv(prog_dir / f"{safe_name}.csv", index=False)

        # Summary row
        summary_rows.append({
            'program': program,
            'num_drops': len(prog_df),
            'total_reapprop': prog_df['reapprop_amount'].sum(),
            'total_sfs_undisbursed': prog_df['sfs_undisbursed_balance'].fillna(0).sum(),
            'chapter_years': ', '.join(str(y) for y in sorted(prog_df['chapter_year'].unique())),
        })

    # Write ALL_DROPS.xlsx with program sheets
    output_path = DROPS_DIR / "ALL_DROPS.xlsx"
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name='SUMMARY', index=False)
        for program, short_name in program_map.items():
            prog_df = df[df['program'] == program]
            if len(prog_df) > 0:
                prog_df[csv_cols].to_excel(writer, sheet_name=short_name, index=False)

    print(f"  ALL_DROPS.xlsx: {len(df)} drops across {len(summary_rows)} programs")


# =============================================================================
# VALIDATION
# =============================================================================

def validate_outputs(mapped_groups, enacted_elements, exec_elements, enacted_to_exec_map,
                     exec_ranges=None):
    """Run validation checks on the pipeline outputs."""
    errors = []
    warnings = []

    # Check 1: All continued enacted elements should have exec mapping
    continued_elems = [e for e in enacted_elements
                       if e['type'] == 'reapprop' and e.get('status') == 'continued_or_modified']
    mapped_count = 0
    for elem in continued_elems:
        key = f"{elem['page']}_{elem['line_start']}"
        if key in enacted_to_exec_map:
            mapped_count += 1

    if mapped_count < len(continued_elems):
        unmapped = len(continued_elems) - mapped_count
        warnings.append(f"  {unmapped}/{len(continued_elems)} continued elements lack exec mapping")
    else:
        print(f"  [OK] All {mapped_count} continued elements have exec mapping")

    # Check 2: No exec_below before exec_above
    backward_count = 0
    for g in mapped_groups:
        above = g.get('exec_above') or {}
        below = g.get('exec_below') or {}
        a_pos = (above.get('page', 0), above.get('line_start', 0) or 0)
        b_pos = (below.get('page', 0), below.get('line_start', 0) or 0)
        if b_pos < a_pos and below.get('type') != 'end_of_document':
            backward_count += 1
            errors.append(f"  Insert {g['insert_id']}: exec_below {b_pos} < exec_above {a_pos}")

    if backward_count == 0:
        print(f"  [OK] No backward exec_below references")
    else:
        print(f"  [FAIL] {backward_count} backward exec_below references")

    # Check 3: No sentinel page 999
    sentinel_count = 0
    for g in mapped_groups:
        for key in ['exec_above', 'exec_below']:
            d = g.get(key) or {}
            if d.get('page', 0) >= 999:
                sentinel_count += 1
                errors.append(f"  Insert {g['insert_id']}: {key} has sentinel page {d.get('page')}")

    if sentinel_count == 0:
        print(f"  [OK] No sentinel page values")
    else:
        print(f"  [FAIL] {sentinel_count} sentinel page values")

    # Check 4: No self-referencing anchors
    self_ref = 0
    for g in mapped_groups:
        ea = (g.get('enacted_above') or {}).get('sort_key')
        eb = (g.get('enacted_below') or {}).get('sort_key')
        if ea and eb and ea == eb:
            self_ref += 1
            aid = (g.get('enacted_above') or {}).get('approp_id', '?')
            warnings.append(f"  Insert {g['insert_id']}: self-ref anchor (ID={aid})")

    if self_ref == 0:
        print(f"  [OK] No self-referencing anchors")
    else:
        print(f"  [WARN] {self_ref} self-referencing anchors")

    # Check 5: Total drops accounted for
    total_in_groups = sum(g['num_undisbursed'] for g in mapped_groups)
    total_drops_with_balance = sum(
        1 for e in enacted_elements
        if e['type'] == 'reapprop' and e.get('has_undisbursed')
    )
    if total_in_groups == total_drops_with_balance:
        print(f"  [OK] All {total_drops_with_balance} undisbursed drops accounted for in groups")
    else:
        errors.append(f"  Groups have {total_in_groups} drops but {total_drops_with_balance} exist")

    # Check 6: Fund consistency — all drops in an insert must share the same fund
    mixed_fund_count = 0
    for g in mapped_groups:
        funds = set(d.get('fund', '') for d in g['undisbursed_drops'])
        if len(funds) > 1:
            mixed_fund_count += 1
            errors.append(f"  Insert {g['insert_id']}: mixed funds: {funds}")

    if mixed_fund_count == 0:
        print(f"  [OK] All inserts have consistent fund types")
    else:
        print(f"  [FAIL] {mixed_fund_count} inserts mix items from different funds")

    # Check 7: All missing headers have exec placements
    total_missing = 0
    placed_count = 0
    for g in mapped_groups:
        for mh in g.get('missing_chapter_headers', []):
            total_missing += 1
            p = mh.get('exec_placement', {})
            if p.get('insert_after_page') is not None:
                placed_count += 1

    if total_missing == 0:
        print(f"  [OK] No missing headers (nothing to place)")
    elif placed_count == total_missing:
        print(f"  [OK] All {total_missing} missing headers have exec placement")
    else:
        unplaced = total_missing - placed_count
        warnings.append(f"  {unplaced}/{total_missing} missing headers lack exec placement")

    # Check 8: No placement above a 2025 section within same fund (ordering rule)
    if exec_ranges:
        above_2025_count = 0
        for g in mapped_groups:
            for mh in g.get('missing_chapter_headers', []):
                p = mh.get('exec_placement', {})
                if p.get('insert_after_page') is None:
                    continue
                prog = mh.get('program', '')
                fund = mh.get('fund', '')
                fund_data = exec_ranges.get(prog, {}).get('funds', {}).get(fund)
                if not fund_data:
                    continue
                # Check if there's a 2025 section in this fund
                for cy_sec in fund_data.get('chapter_years', []):
                    if cy_sec['chapter_year'] == 2025:
                        cy2025_start = (cy_sec['range'][0], cy_sec['range'][1])
                        placement_pos = (p['insert_after_page'],
                                         p['insert_after_line'] or 0)
                        if placement_pos < cy2025_start:
                            above_2025_count += 1
                            warnings.append(
                                f"  Insert {g['insert_id']}: ChYr {mh['chapter_year']} placed"
                                f" at p{p['insert_after_page']}L{p['insert_after_line']}"
                                f" ABOVE 2025 section at p{cy2025_start[0]}L{cy2025_start[1]}"
                                f" in {fund}"
                            )
                        break

        if above_2025_count == 0:
            print(f"  [OK] No missing header placements above 2025 sections")
        else:
            print(f"  [WARN] {above_2025_count} placements above 2025 sections")

    # Check 9: All placements within correct fund range
    if exec_ranges:
        out_of_range = 0
        for g in mapped_groups:
            for mh in g.get('missing_chapter_headers', []):
                p = mh.get('exec_placement', {})
                if p.get('insert_after_page') is None:
                    continue
                if p.get('placement_method') in ('graduated_to_program', 'no_program_in_exec'):
                    continue  # graduated placements are by definition outside fund range
                prog = mh.get('program', '')
                fund = mh.get('fund', '')
                fund_data = exec_ranges.get(prog, {}).get('funds', {}).get(fund)
                if not fund_data:
                    continue
                fr = fund_data['range']
                pp, pl = p['insert_after_page'], p['insert_after_line'] or 0
                if (pp, pl) < (fr[0], fr[1]) or (pp, pl) > (fr[2], fr[3]):
                    out_of_range += 1
                    errors.append(
                        f"  Insert {g['insert_id']}: ChYr {mh['chapter_year']} placement"
                        f" p{pp}L{pl} outside fund range p{fr[0]}L{fr[1]}-p{fr[2]}L{fr[3]}"
                        f" ({fund})"
                    )

        if out_of_range == 0:
            print(f"  [OK] All missing header placements within correct fund ranges")
        else:
            print(f"  [FAIL] {out_of_range} placements outside fund ranges")

    # Summary
    if errors:
        print(f"\n  ERRORS: {len(errors)}")
        for e in errors:
            print(e)
    if warnings:
        print(f"\n  WARNINGS: {len(warnings)}")
        for w in warnings:
            print(w)

    return len(errors) == 0


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def main():
    print("=" * 80)
    print("Build Inserts Pipeline")
    print("=" * 80)

    # Step 1: Load data
    print("\n>>> Loading extraction CSVs...")
    enacted_df, exec_df, continued_df, dropped_df = load_extraction_csvs()
    print(f"  Enacted: {len(enacted_df)}, Executive: {len(exec_df)}")
    print(f"  Continued/Modified: {len(continued_df)}, Dropped: {len(dropped_df)}")

    print("\n>>> Loading structural anchors...")
    enacted_anchors, exec_anchors = load_anchors()
    print(f"  Enacted anchors: {len(enacted_anchors)}, Exec anchors: {len(exec_anchors)}")

    # Step 2: Build element lists (with amendment parsing from Step 1)
    print("\n>>> Building enacted elements...")
    enacted_elements = build_enacted_elements(enacted_df, continued_df, dropped_df, enacted_anchors)
    print(f"  {len(enacted_elements)} elements ({sum(1 for e in enacted_elements if e['type'] == 'reapprop')} reapprops)")
    enacted_chyr = [e for e in enacted_elements if e['type'] == 'anchor_chapter_year']
    print(f"  Chapter year anchors: {len(enacted_chyr)}"
          f" (amended: {sum(1 for e in enacted_chyr if e.get('is_amended'))})")

    print("\n>>> Building exec elements...")
    exec_elements = build_exec_elements(exec_df, exec_anchors)
    print(f"  {len(exec_elements)} elements ({sum(1 for e in exec_elements if e['type'] == 'reapprop')} reapprops)")
    exec_chyr = [e for e in exec_elements if e['type'] == 'anchor_chapter_year']
    print(f"  Chapter year anchors: {len(exec_chyr)}"
          f" (amended: {sum(1 for e in exec_chyr if e.get('is_amended'))})")

    # Step 2b: Build hierarchy indices and tuple keys
    print("\n>>> Building hierarchy indices...")
    enacted_hierarchy = build_hierarchy_indices(enacted_anchors)
    exec_hierarchy = build_hierarchy_indices(exec_anchors)
    print(f"  Enacted: {len(enacted_hierarchy['program_idx'])} programs,"
          f" {len(enacted_hierarchy['fund_idx'])} funds")
    print(f"  Exec: {len(exec_hierarchy['program_idx'])} programs,"
          f" {len(exec_hierarchy['fund_idx'])} funds")

    print("\n>>> Assigning tuple keys...")
    build_element_tuples(enacted_elements, enacted_hierarchy)
    build_element_tuples(exec_elements, exec_hierarchy)
    # Verify all elements got tuples
    tupled = sum(1 for e in enacted_elements if e.get('tuple_key'))
    print(f"  Enacted: {tupled}/{len(enacted_elements)} elements with tuple keys")
    tupled_exec = sum(1 for e in exec_elements if e.get('tuple_key'))
    print(f"  Exec: {tupled_exec}/{len(exec_elements)} elements with tuple keys")

    # Step 2c: Build hierarchical ranges
    print("\n>>> Building hierarchical ranges...")
    enacted_ranges = build_hierarchical_ranges(enacted_elements, enacted_hierarchy)
    exec_ranges = build_hierarchical_ranges(exec_elements, exec_hierarchy)
    enacted_chyr_count = sum(
        len(fd['chapter_years'])
        for prog in enacted_ranges.values() for fd in prog['funds'].values()
    )
    exec_chyr_count = sum(
        len(fd['chapter_years'])
        for prog in exec_ranges.values() for fd in prog['funds'].values()
    )
    print(f"  Enacted: {len(enacted_ranges)} programs,"
          f" {sum(len(p['funds']) for p in enacted_ranges.values())} funds,"
          f" {enacted_chyr_count} chapter year sections")
    print(f"  Exec: {len(exec_ranges)} programs,"
          f" {sum(len(p['funds']) for p in exec_ranges.values())} funds,"
          f" {exec_chyr_count} chapter year sections")

    # Step 3: Annotate exec presence on enacted elements
    print("\n>>> Annotating exec presence on enacted anchors...")
    annotated = annotate_exec_presence(enacted_elements, exec_anchors)
    has_exec = sum(1 for e in enacted_elements
                   if e['type'].startswith('anchor_') and e.get('has_exec_counterpart'))
    no_exec = sum(1 for e in enacted_elements
                  if e['type'].startswith('anchor_') and not e.get('has_exec_counterpart', True))
    print(f"  {annotated} anchors annotated: {has_exec} with exec counterpart,"
          f" {no_exec} enacted-only")

    # Step 4: Build enacted-to-exec mapping (Bug 2 fix)
    print("\n>>> Building enacted-to-exec mapping...")
    enacted_to_exec_map = build_enacted_to_exec_mapping(continued_df, exec_df)
    print(f"  {len(enacted_to_exec_map)} mappings")

    # Step 5: Build anchor mapping
    print("\n>>> Building anchor mapping...")
    enacted_anchor_to_exec_map, exec_anchor_lookup = build_anchor_mapping(enacted_anchors, exec_anchors)
    print(f"  {len(enacted_anchor_to_exec_map)} anchors mapped to exec")

    # Step 6: Group drops into inserts (Bug 4 fix + exec-only anchors)
    print("\n>>> Grouping drops into inserts...")
    groups = group_drops_into_inserts(enacted_elements, enacted_to_exec_map)
    print(f"  {len(groups)} insert groups")

    # Step 7: Detect missing chapter year headers (with placement)
    print("\n>>> Detecting missing chapter year headers...")
    detect_missing_chapter_headers(groups, enacted_elements, exec_anchors, exec_ranges)
    total_missing = sum(len(g['missing_chapter_headers']) for g in groups)
    placed = sum(
        1 for g in groups for mh in g['missing_chapter_headers']
        if mh.get('exec_placement', {}).get('insert_after_page') is not None
    )
    print(f"  {total_missing} missing headers across all groups ({placed} with exec placement)")

    # Step 7: Map to exec locations (Bug 3, Bug 5 fixes)
    print("\n>>> Mapping groups to exec locations...")
    mapped_groups = map_groups_to_exec(groups, enacted_to_exec_map, enacted_elements,
                                       exec_elements, enacted_anchor_to_exec_map,
                                       exec_anchor_lookup)
    print(f"  {len(mapped_groups)} mapped groups")

    # Step 8: Save intermediate artifacts
    print("\n>>> Saving intermediate artifacts...")

    with open(BASE_DIR / "enacted_elements.pkl", 'wb') as f:
        pickle.dump(enacted_elements, f)
    print(f"  enacted_elements.pkl: {len(enacted_elements)} elements")

    with open(BASE_DIR / "exec_elements.pkl", 'wb') as f:
        pickle.dump(exec_elements, f)
    print(f"  exec_elements.pkl: {len(exec_elements)} elements")

    with open(BASE_DIR / "enacted_to_exec_loc.json", 'w') as f:
        json.dump(enacted_to_exec_map, f, indent=2)
    print(f"  enacted_to_exec_loc.json: {len(enacted_to_exec_map)} mappings")

    with open(BASE_DIR / "insert_groups.pkl", 'wb') as f:
        pickle.dump(groups, f)
    print(f"  insert_groups.pkl: {len(groups)} groups")

    with open(BASE_DIR / "mapped_groups_final.pkl", 'wb') as f:
        pickle.dump(mapped_groups, f)
    print(f"  mapped_groups_final.pkl: {len(mapped_groups)} groups")

    # Step 9: Generate Excel outputs
    print("\n>>> Generating Excel outputs...")
    inserts_rows, details_rows = generate_insert_lookup(mapped_groups, enacted_elements, exec_anchors)
    generate_insert_editor(inserts_rows, details_rows)
    generate_insert_editor_nonfederal(inserts_rows, details_rows)
    generate_drops_output(dropped_df)

    # Step 10: Validation
    print("\n>>> Running validation...")
    valid = validate_outputs(mapped_groups, enacted_elements, exec_elements, enacted_to_exec_map,
                             exec_ranges=exec_ranges)

    print(f"\n{'=' * 80}")
    if valid:
        print("PIPELINE COMPLETE — all checks passed")
    else:
        print("PIPELINE COMPLETE — errors found, see above")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()

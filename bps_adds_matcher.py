#!/usr/bin/env python3
"""
BPS Legislative Adds Matcher

Matches legislative budget preparation system (BPS) adds against v7 analysis
output to determine the status of each add (discontinued, continued, modified,
or not found in enacted budget).

Uses the Anthropic API to semantically match BPS program names against
enacted budget bill language, since identifiers don't align between systems.

Usage:
    python bps_adds_matcher.py <adds_excel> <enacted_csv> <drops_excel> [--output results.xlsx]

Example:
    python bps_adds_matcher.py \
        "Reapprops 26-27/BPS to v7 check/AA ATL Adds.xlsx" \
        "Reapprops 26-27/output_atl_v7/enacted_budget_data.csv" \
        "Reapprops 26-27/BPS to v7 check/ATL Drops.xlsx" \
        --output "Reapprops 26-27/BPS to v7 check/AA_ATL_matched.xlsx"
"""

import pandas as pd
import argparse
import json
import os
import sys
import re
import time
from pathlib import Path
from anthropic import Anthropic


def get_client() -> Anthropic:
    """Create Anthropic client, handling OAuth token auth if no API key."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    oauth_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')

    if api_key:
        return Anthropic(api_key=api_key)
    elif oauth_token:
        return Anthropic(
            auth_token=oauth_token,
            base_url=os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com'),
        )
    else:
        print("Error: No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN found")
        sys.exit(1)


def load_adds(path: Path) -> pd.DataFrame:
    """Load BPS adds Excel (no header row)."""
    df = pd.read_excel(path, header=None).dropna(how='all')
    df.columns = [
        'key', 'fiscal_year', 'source', 'chapter_year', 'bps_id',
        'program_name', 'amount', 'house', 'col8', 'col9', 'col10', 'key_dup'
    ]
    # Clean up types
    df['chapter_year'] = df['chapter_year'].astype(int)
    df['amount'] = df['amount'].astype(int)
    df['bps_id'] = df['bps_id'].astype(int)
    return df


def load_enacted(enacted_csv: Path, drops_xlsx: Path) -> pd.DataFrame:
    """Load all enacted records with bill_language.

    Merges enacted CSV data with bill_language from drops Excel where available,
    and uses raw_line as fallback.
    """
    enacted = pd.read_csv(enacted_csv)

    # If bill_language column exists in enacted CSV, use it directly
    if 'bill_language' in enacted.columns:
        enacted['text'] = enacted['bill_language'].fillna(enacted.get('raw_line', ''))
    else:
        # Merge with drops to get bill_language for discontinued items
        drops = pd.read_excel(drops_xlsx)
        drops_lang = drops[['composite_key', 'bill_language']].rename(
            columns={'bill_language': 'drops_bill_language'}
        )
        enacted = enacted.merge(drops_lang, on='composite_key', how='left')
        enacted['text'] = enacted['drops_bill_language'].fillna(enacted.get('raw_line', ''))

    return enacted


def load_comparison_status(drops_xlsx: Path, enacted_csv: Path) -> dict:
    """Build a lookup: composite_key -> status (discontinued/continued/modified/etc).

    Drops are explicitly in the drops file. Everything else in enacted that
    isn't in drops is continued or modified.
    """
    drops = pd.read_excel(drops_xlsx)
    drop_keys = set(drops['composite_key'].tolist())

    enacted = pd.read_csv(enacted_csv)
    status_map = {}
    for _, row in enacted.iterrows():
        ck = row['composite_key']
        if ck in drop_keys:
            status_map[ck] = 'discontinued'
        else:
            status_map[ck] = 'continued_or_modified'

    return status_map


def truncate_bill_language(text: str, max_chars: int = 300) -> str:
    """Truncate bill language to keep LLM context manageable."""
    if not text or pd.isna(text):
        return ""
    text = str(text)
    # Remove line number prefixes
    text = re.sub(r'(?m)^\d{1,2}\s+', '', text)
    # Remove page headers
    text = re.sub(r'AB\n.*?\n.*?\n.*?20\d\d-\d\d\n', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


def build_candidate_list(
    add_row: pd.Series,
    enacted: pd.DataFrame,
    status_map: dict,
    max_candidates: int = 30
) -> list:
    """Find candidate enacted records for a BPS add.

    Filters by chapter year first, then by amount if too many candidates.
    Returns list of dicts with candidate info.
    """
    chyr = add_row['chapter_year']
    amt = add_row['amount']

    # Filter by chapter year
    candidates = enacted[enacted['chapter_year'] == chyr].copy()

    # If too many, also filter by amount
    if len(candidates) > max_candidates:
        exact_amt = candidates[candidates['appropriation_amount'] == amt]
        if len(exact_amt) > 0:
            candidates = exact_amt

    # If still too many, truncate
    if len(candidates) > max_candidates:
        candidates = candidates.head(max_candidates)

    result = []
    for _, row in candidates.iterrows():
        ck = row['composite_key']
        result.append({
            'index': len(result),
            'agency': row['agency'],
            'appropriation_id': row['appropriation_id'],
            'chapter_year': row['chapter_year'],
            'amount': row['appropriation_amount'],
            'reapprop_amount': row.get('reappropriation_amount', 0),
            'account': row.get('account', ''),
            'text': truncate_bill_language(row['text']),
            'composite_key': ck,
            'status': status_map.get(ck, 'unknown'),
        })

    return result


def match_single_add(
    client: Anthropic,
    add_row: pd.Series,
    candidates: list,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    """Use LLM to match a single BPS add against candidate enacted records."""

    program_name = add_row['program_name']
    chyr = add_row['chapter_year']
    amt = add_row['amount']
    bps_id = add_row['bps_id']

    if not candidates:
        return {
            'matched': False,
            'match_index': None,
            'confidence': 'none',
            'reasoning': 'No candidates found for this chapter year/amount',
        }

    # Build candidate descriptions
    cand_lines = []
    for c in candidates:
        cand_lines.append(
            f"[{c['index']}] Agency: {c['agency']} | "
            f"ID: {c['appropriation_id']} | "
            f"Amount: ${c['amount']:,} | "
            f"Status: {c['status']} | "
            f"Text: {c['text']}"
        )
    candidates_text = "\n".join(cand_lines)

    prompt = f"""Match this legislative budget add to the correct enacted budget record.

BPS ADD:
- Program Name: {program_name}
- BPS ID: {bps_id}
- Chapter Year: {chyr}
- Amount: ${amt:,}

CANDIDATE ENACTED RECORDS (same chapter year):
{candidates_text}

Instructions:
- Find the candidate whose bill language describes the same program as the BPS add name.
- The program name usually appears within the bill language text (e.g., "Storm King Arts Center" appears in "For services and expenses of the Storm King Arts Center (57003)").
- Names may be slightly different (abbreviations, "Inc." vs "Inc", word order).
- If no candidate matches, say so.

Respond with ONLY a JSON object (no markdown):
{{"matched": true/false, "match_index": <number or null>, "confidence": "high"/"medium"/"low"/"none", "reasoning": "<brief explanation>"}}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Parse JSON from response
        # Handle potential markdown wrapping
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        result = json.loads(text)
        return result
    except Exception as e:
        return {
            'matched': False,
            'match_index': None,
            'confidence': 'error',
            'reasoning': f'API error: {str(e)}',
        }


def run_matching(
    adds: pd.DataFrame,
    enacted: pd.DataFrame,
    status_map: dict,
    model: str = "claude-haiku-4-5-20251001",
    batch_delay: float = 0.1,
) -> pd.DataFrame:
    """Run LLM matching for all BPS adds."""

    client = get_client()
    results = []

    total = len(adds)
    print(f"\nMatching {total} BPS adds against {len(enacted)} enacted records...")
    print(f"Using model: {model}")
    print()

    for i, (_, add) in enumerate(adds.iterrows()):
        candidates = build_candidate_list(add, enacted, status_map)

        result = match_single_add(client, add, candidates, model=model)

        # Build output row
        row = {
            'bps_id': add['bps_id'],
            'program_name': add['program_name'],
            'chapter_year': add['chapter_year'],
            'amount': add['amount'],
            'fiscal_year': add['fiscal_year'],
            'house': add['house'],
            'matched': result.get('matched', False),
            'confidence': result.get('confidence', 'none'),
            'reasoning': result.get('reasoning', ''),
            'num_candidates': len(candidates),
        }

        # If matched, pull in the enacted record details
        match_idx = result.get('match_index')
        if result.get('matched') and match_idx is not None and match_idx < len(candidates):
            matched_cand = candidates[match_idx]
            row['enacted_agency'] = matched_cand['agency']
            row['enacted_approp_id'] = matched_cand['appropriation_id']
            row['enacted_chapter_year'] = matched_cand['chapter_year']
            row['enacted_amount'] = matched_cand['amount']
            row['enacted_reapprop_amount'] = matched_cand['reapprop_amount']
            row['enacted_account'] = matched_cand['account']
            row['enacted_status'] = matched_cand['status']
            row['enacted_composite_key'] = matched_cand['composite_key']
            row['enacted_bill_language'] = matched_cand['text']
        else:
            row['enacted_agency'] = None
            row['enacted_approp_id'] = None
            row['enacted_chapter_year'] = None
            row['enacted_amount'] = None
            row['enacted_reapprop_amount'] = None
            row['enacted_account'] = None
            row['enacted_status'] = None
            row['enacted_composite_key'] = None
            row['enacted_bill_language'] = None

        results.append(row)

        # Progress
        status_str = f"MATCH ({result.get('confidence', '?')})" if result.get('matched') else "NO MATCH"
        print(f"  [{i+1}/{total}] {add['program_name'][:50]:<50} -> {status_str}")

        # Rate limiting
        time.sleep(batch_delay)

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(
        description='Match BPS legislative adds against v7 analysis output'
    )
    parser.add_argument('adds_excel', type=Path, help='BPS adds Excel file')
    parser.add_argument('enacted_csv', type=Path, help='Enacted budget data CSV from v7')
    parser.add_argument('drops_excel', type=Path, help='ATL Drops Excel file')
    parser.add_argument('--output', '-o', type=Path,
                        default=Path('bps_matched_results.xlsx'),
                        help='Output Excel file')
    parser.add_argument('--model', default='claude-haiku-4-5-20251001',
                        help='Anthropic model to use')

    args = parser.parse_args()

    # Validate inputs
    for p in [args.adds_excel, args.enacted_csv, args.drops_excel]:
        if not p.exists():
            print(f"Error: File not found: {p}")
            sys.exit(1)

    print("=" * 70)
    print("BPS LEGISLATIVE ADDS MATCHER")
    print("=" * 70)

    # Load data
    print("\nLoading BPS adds...")
    adds = load_adds(args.adds_excel)
    print(f"  {len(adds)} adds loaded")

    print("Loading enacted budget data...")
    enacted = load_enacted(args.enacted_csv, args.drops_excel)
    print(f"  {len(enacted)} enacted records loaded")

    print("Building status lookup...")
    status_map = load_comparison_status(args.drops_excel, args.enacted_csv)
    disc_count = sum(1 for v in status_map.values() if v == 'discontinued')
    print(f"  {disc_count} discontinued, {len(status_map) - disc_count} continued/modified")

    # Run matching
    results = run_matching(adds, enacted, status_map, model=args.model)

    # Summary
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    matched = results[results['matched'] == True]
    unmatched = results[results['matched'] == False]
    print(f"Total adds: {len(results)}")
    print(f"Matched: {len(matched)}")
    print(f"Unmatched: {len(unmatched)}")

    if len(matched) > 0:
        print(f"\nMatched status breakdown:")
        print(matched['enacted_status'].value_counts().to_string())

        print(f"\nConfidence breakdown:")
        print(matched['confidence'].value_counts().to_string())

        disc_matched = matched[matched['enacted_status'] == 'discontinued']
        cont_matched = matched[matched['enacted_status'] == 'continued_or_modified']
        print(f"\n  -> {len(disc_matched)} adds are DISCONTINUED (dropped by executive)")
        print(f"  -> {len(cont_matched)} adds are CONTINUED/MODIFIED (kept by executive)")

    # Save
    results.to_excel(args.output, index=False)
    print(f"\nResults saved to: {args.output}")


if __name__ == '__main__':
    main()

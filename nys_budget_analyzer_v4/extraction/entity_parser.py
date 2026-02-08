"""
Lean entity parser for extracting structured data from bill_language.
"""

import re
from typing import Dict, Any, Optional

# Precompiled patterns based on actual data analysis
_PURPOSE = re.compile(r'(?:^|\n)\s*\d*\s*(For\s+(?:services and expenses|additional\s+\w+|payment|grants?|the\s+\w+)[^(]{10,150})', re.I)

# Recipient: Org name ending with suffix, followed by 5-digit ID
_RECIPIENT = re.compile(
    r'([A-Z][A-Za-z\s,\-&\.\']{5,70}'
    r'(?:Inc\.?|Corp\.?|Corporation|Foundation|Association|Center|Council|'
    r'Authority|University|College|Hospital|Institute))'
    r'\s*\(\d{5}\)', re.I
)

# Transfer: "may be suballocated/transferred to [target]"
_TRANSFER = re.compile(r'may be (?:suballocated|transferred)(?: or (?:suballocated|transferred))? to\s+(?:the\s+)?([a-z][a-z\s]{5,50}(?:department|agency|office|account))', re.I)

# Approval: "subject to/with approval of [authority]"
_APPROVAL = re.compile(r'(?:subject to|with) (?:the )?(?:prior )?approval of (?:the )?([a-z][a-z\s]{5,40}(?:director|commissioner|budget))', re.I)

# Set-aside: "up to $X of the amount"
_SETASIDE = re.compile(r'up to \$?([\d,]+)\s+(?:of the amount|herein|from this)', re.I)

# Statute: "section X of the Y law"
_STATUTE = re.compile(r'section (\d+(?:-\w+)?)\s+of (?:the )?(education|executive|social services|public health|mental hygiene|state finance) law', re.I)

# Category keywords - high-signal terms only
_CATEGORIES = {
    'education': ['school', 'education', 'university', 'college', 'student', 'tuition', 'suny', 'cuny', 'regents'],
    'health': ['health', 'medical', 'hospital', 'medicaid', 'clinic', 'mental hygiene', 'patient'],
    'social_services': ['family assistance', 'child care', 'aging', 'senior', 'foster care', 'welfare', 'elderly'],
    'housing': ['housing', 'homeless', 'shelter', 'residential'],
    'transportation': ['transportation', 'highway', 'transit', 'bridge', 'mta', 'railroad'],
    'public_safety': ['police', 'correction', 'criminal justice', 'emergency', 'public safety'],
    'environment': ['environmental', 'parks', 'conservation', 'pollution', 'wildlife'],
    'economic_dev': ['economic development', 'business', 'workforce', 'tourism', 'job training'],
    'agriculture': ['agriculture', 'farm', 'food', 'dairy'],
    'arts': ['arts', 'museum', 'cultural', 'library', 'historic preservation'],
}

# Org type keywords
_ORG_TYPES = [
    (['inc.', 'inc', 'corp.', 'corp', 'corporation'], 'corporation'),
    (['foundation'], 'foundation'),
    (['university', 'college', 'school'], 'educational'),
    (['authority', 'commission'], 'public_authority'),
    (['council', 'association', 'society'], 'nonprofit'),
    (['hospital', 'clinic', 'medical'], 'healthcare'),
    (['center', 'program', 'project', 'initiative', 'services'], 'program'),
]


def parse(text: str) -> Dict[str, Any]:
    """Extract entities from bill_language text."""
    if not text:
        return _empty()

    text_lower = text.lower()

    # Purpose - first "For..." clause
    m = _PURPOSE.search(text)
    purpose = ' '.join(m.group(1).split())[:200] if m else None

    # Category - keyword scoring
    category = None
    best_score = 0
    for cat, keywords in _CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score, category = score, cat

    # Recipient - org name before (ID)
    m = _RECIPIENT.search(text)
    recipient = m.group(1).strip() if m else None
    recipient_type = _get_org_type(recipient) if recipient else None

    # Transfer authority
    m = _TRANSFER.search(text)
    has_transfer = bool(m)
    transfer_target = m.group(1).strip() if m else None

    # Approval requirement
    m = _APPROVAL.search(text)
    requires_approval = bool(m)
    approval_authority = m.group(1).strip() if m else None

    # Set-aside amount
    m = _SETASIDE.search(text)
    set_aside = int(m.group(1).replace(',', '')) if m else None

    # Statutory references
    statutes = [{'section': m.group(1), 'law': m.group(2)} for m in _STATUTE.finditer(text)]

    return {
        'program_purpose': purpose,
        'program_category': category,
        'recipient_name': recipient,
        'recipient_type': recipient_type,
        'has_transfer_authority': has_transfer,
        'transfer_target': transfer_target,
        'requires_approval': requires_approval,
        'approval_authority': approval_authority,
        'set_aside_amount': set_aside,
        'statutory_references': statutes,
    }


def _empty() -> Dict[str, Any]:
    return {
        'program_purpose': None,
        'program_category': None,
        'recipient_name': None,
        'recipient_type': None,
        'has_transfer_authority': False,
        'transfer_target': None,
        'requires_approval': False,
        'approval_authority': None,
        'set_aside_amount': None,
        'statutory_references': [],
    }


def _get_org_type(name: str) -> str:
    if not name:
        return 'other'
    name_lower = name.lower()
    for keywords, org_type in _ORG_TYPES:
        if any(kw in name_lower for kw in keywords):
            return org_type
    return 'other'


# Backward compatibility
class EntityParser:
    def parse(self, text: str) -> Dict[str, Any]:
        return parse(text)


class ProgramCategoryClassifier:
    def classify(self, text: str) -> Optional[str]:
        return parse(text).get('program_category')

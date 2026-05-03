"""Medical code and DME device extraction utilities.

Centralizes the regex patterns and helpers used to pull CPT codes,
HCPCS Level II codes (including DME and orthotic/prosthetic codes),
ICD-10-CM diagnosis codes, and DME device mentions out of unstructured
denial-letter text.

Previously these patterns were duplicated inside ``process_denial.py``
and ``common_view_logic.py`` with overly strict delimiter requirements
that silently dropped codes flush against the start/end of a string,
codes adjacent to hyphens or quotes, and skipped HCPCS Level II codes
entirely. Centralizing the logic here lets the rest of the codebase
benefit from one consistent extractor (and one place to add new
heuristics like DME keyword detection).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# CPT codes are five characters: typically 5 digits (Category I), or
# 4 digits + a letter (Category II ends in F; Category III ends in T).
# We accept any ``\d{4}[A-Z0-9]`` for backwards compatibility with the
# previous regex while widening the boundary characters.
#
# We use lookarounds rather than consuming delimiters so codes that sit
# at the start/end of a string, next to a hyphen, or inside quotation
# marks still match - the previous regex required at least one of
# ``[(\s:,]`` before and ``[\s:.),]`` after, which silently dropped
# common cases like ``"Code:99213."`` or ``"99213"`` at end-of-string.
CPT_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(\d{4}[A-Z0-9])(?![A-Za-z0-9])",
    re.M | re.UNICODE,
)

# HCPCS Level II codes are a single letter followed by 4 digits.
# The valid CMS Level II prefixes are A-V excluding I, N, and O - I/O
# are skipped because they are visually ambiguous with 1/0, and N is
# not currently issued. D-codes are dental (CDT-aligned).
HCPCS_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-HJ-MP-V]\d{4})(?![A-Za-z0-9])",
    re.M | re.UNICODE,
)

# ICD-10-CM diagnosis codes: letter (A-Z, excluding U which is reserved)
# + digit + alnum, optionally followed by a decimal and up to four more
# characters. Functionally equivalent to the previous pattern but uses
# lookarounds to match codes flush against a string boundary or hyphen.
ICD10_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-TV-Z][0-9][0-9AB]\.?[0-9A-TV-Z]{0,4})(?![A-Za-z0-9])",
    re.M | re.UNICODE,
)


# HCPCS letter-prefix categories (CMS Level II).
HCPCS_CATEGORY: dict[str, str] = {
    "A": "supplies",
    "B": "enteral_parenteral",
    "C": "outpatient_pps",
    "D": "dental",
    "E": "dme",
    "G": "temporary",
    "H": "behavioral_health",
    "J": "drug",
    "K": "dme",
    "L": "orthotic_prosthetic",
    "M": "medical_services",
    "P": "pathology_laboratory",
    "Q": "temporary",
    "R": "diagnostic_radiology",
    "S": "private_payer",
    "T": "state_medicaid",
    "V": "vision_hearing_speech",
}

# HCPCS letter prefixes that identify the code as durable medical
# equipment or a closely-related orthotic/prosthetic device. These are
# the codes the appeal generator should treat as DME for routing,
# template selection, and RAG filtering.
DME_HCPCS_PREFIXES: frozenset[str] = frozenset({"E", "K", "L"})


# Subset of DME categories whose keys correspond directly to choices in
# ``fighthealthinsurance.forms.questions.AssistiveDeviceAppealForm``.
# Categories outside this set (respiratory, diabetic, hospital_bed,
# infusion_pump) describe broader DME classes that the assistive-device
# form does not enumerate; callers that pre-fill the form must map
# those to ``"other"``. ``extract_dme_devices`` does this mapping
# automatically and exposes the form-aligned subset as ``device_types``.
FORM_DEVICE_TYPES: frozenset[str] = frozenset(
    {
        "aac_high_tech",
        "aac_low_tech",
        "mobility",
        "prosthetic",
        "orthotic",
        "hearing",
    }
)


# Map DME-category keys to lowercased keyword phrases that, when found
# in denial text, strongly suggest a particular kind of DME or
# assistive device. Phrases are kept lowercase; matching is
# case-insensitive and operates on whitespace-normalized text so
# multi-word phrases also match across line breaks or repeated spaces.
DME_DEVICE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "aac_high_tech": (
        "speech-generating device",
        "speech generating device",
        "high-tech aac",
        "high tech aac",
        "augmentative communication device",
        "augmentative and alternative communication",
        "communication tablet",
        "voice output communication aid",
        "voice output device",
    ),
    "aac_low_tech": (
        "low-tech aac",
        "low tech aac",
        "picture exchange communication",
        "picture board",
        "communication book",
    ),
    "mobility": (
        "wheelchair",
        "power chair",
        "powerchair",
        "scooter",
        "rollator",
        "walker",
        "crutch",
        "mobility aid",
        "mobility device",
        "transfer chair",
        "geri chair",
    ),
    "prosthetic": (
        "prosthetic",
        "prosthesis",
        "artificial limb",
        "limb replacement",
    ),
    "orthotic": (
        "orthotic",
        "orthosis",
        "ankle-foot orthosis",
        "knee brace",
        "back brace",
        "spinal orthosis",
    ),
    "hearing": (
        "hearing aid",
        "cochlear implant",
        "bone-anchored hearing",
        "baha device",
    ),
    "respiratory": (
        "cpap",
        "bipap",
        "bi-pap",
        "auto-pap",
        "ventilator",
        "oxygen concentrator",
        "oxygen tank",
        "nebulizer",
        "suction pump",
        "tracheostomy supply",
    ),
    "diabetic": (
        "continuous glucose monitor",
        "insulin pump",
        "blood glucose monitor",
    ),
    "hospital_bed": (
        "hospital bed",
        "hospital-grade bed",
        "specialty bed",
        "alternating pressure mattress",
        "low air loss mattress",
    ),
    "infusion_pump": (
        "infusion pump",
        "external infusion pump",
    ),
}


def extract_cpt_codes(text: str) -> set[str]:
    """Return the set of CPT codes (Category I/II/III) found in *text*."""
    if not text:
        return set()
    return {m.group(1) for m in CPT_CODE_PATTERN.finditer(text)}


def extract_hcpcs_codes(text: str) -> set[str]:
    """Return the set of HCPCS Level II codes found in *text*.

    Includes DME (E/K), orthotic-prosthetic (L), drug (J), supply (A),
    and other Level II categories.
    """
    if not text:
        return set()
    return {m.group(1) for m in HCPCS_CODE_PATTERN.finditer(text)}


def extract_icd10_codes(text: str) -> set[str]:
    """Return the set of ICD-10-CM diagnosis codes found in *text*."""
    if not text:
        return set()
    return {m.group(1) for m in ICD10_CODE_PATTERN.finditer(text)}


def extract_procedure_codes(text: str) -> set[str]:
    """Return the union of CPT and HCPCS codes - the full set of
    procedure-style codes useful for downstream RAG / lookup."""
    return extract_cpt_codes(text) | extract_hcpcs_codes(text)


def hcpcs_category(code: str) -> Optional[str]:
    """Return the HCPCS letter-prefix category for *code*, or ``None``
    if *code* is empty or has an unknown prefix."""
    if not code:
        return None
    return HCPCS_CATEGORY.get(code[0].upper())


def is_dme_code(code: str) -> bool:
    """True if *code* is a HCPCS Level II code that identifies durable
    medical equipment or a closely-related orthotic/prosthetic device.
    """
    if not code or len(code) != 5:
        return False
    return code[0].upper() in DME_HCPCS_PREFIXES


def is_aac_code(code: str) -> bool:
    """True if *code* identifies a speech-generating / AAC device.

    AAC speech-generating devices live in HCPCS E1902 and E2500-E2599.
    """
    if not code or len(code) != 5:
        return False
    if code[0].upper() != "E":
        return False
    try:
        num = int(code[1:])
    except ValueError:
        return False
    return num == 1902 or 2500 <= num <= 2599


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace (spaces, tabs, newlines) into single
    spaces so that multi-word keyword phrases match across line wraps
    and repeated spaces commonly produced by OCR or PDF text extraction.
    """
    return _WHITESPACE_RE.sub(" ", text)


def extract_dme_devices(text: str) -> dict[str, object]:
    """Identify DME and assistive devices mentioned in *text*.

    Returns a dict with:
        - ``codes``: set of HCPCS codes that look like DME (E/K/L)
        - ``all_hcpcs``: full set of HCPCS codes found
        - ``device_types``: set of form-aligned device-type keys
          (a subset of the ``AssistiveDeviceAppealForm.device_type``
          choices). When a broader DME category matches but is not a
          valid form choice, ``"other"`` is added so callers can safely
          pre-fill the form.
        - ``dme_categories``: full set of matched DME category keys,
          including non-form-aligned categories like ``respiratory``,
          ``diabetic``, ``hospital_bed``, and ``infusion_pump``. Useful
          for appeal routing and RAG context filtering.
        - ``matched_keywords``: dict of dme_category -> list of matched
          keyword phrases (lowercase, deduplicated, in the order each
          phrase first appears in the source text).

    Keyword matching operates on whitespace-normalized lowercased text
    so multi-word phrases ("speech-generating device") also match when
    the text contains line breaks or repeated spaces from OCR or PDF
    extraction.
    """
    if not text:
        return {
            "codes": set(),
            "all_hcpcs": set(),
            "device_types": set(),
            "dme_categories": set(),
            "matched_keywords": {},
        }

    all_hcpcs = extract_hcpcs_codes(text)
    dme_codes = {c for c in all_hcpcs if is_dme_code(c)}

    normalized = _normalize_whitespace(text.lower())

    # Find each (category, keyword) pair that matches and remember the
    # position of its first occurrence so we can record matched keywords
    # in document order rather than DME_DEVICE_KEYWORDS-iteration order.
    keyword_hits: list[tuple[int, str, str]] = []
    for category, keywords in DME_DEVICE_KEYWORDS.items():
        for keyword in keywords:
            position = normalized.find(keyword)
            if position != -1:
                keyword_hits.append((position, category, keyword))
    keyword_hits.sort()

    matched_keywords: dict[str, list[str]] = {}
    dme_categories: set[str] = set()
    for _position, category, keyword in keyword_hits:
        bucket = matched_keywords.setdefault(category, [])
        if keyword not in bucket:
            bucket.append(keyword)
        dme_categories.add(category)

    # AAC and L-code HCPCS codes promote the category even without
    # keyword hits. Inspect codes in their first-occurrence order so
    # the L-code orthotic-default decision sees keyword-derived
    # categories first.
    for code in all_hcpcs:
        upper = code.upper()
        if is_aac_code(upper):
            dme_categories.add("aac_high_tech")
        elif upper.startswith("L") and not (
            "prosthetic" in dme_categories or "orthotic" in dme_categories
        ):
            # L-codes cover both orthotics and prosthetics; default to
            # "orthotic" when we have no keyword evidence either way.
            dme_categories.add("orthotic")

    # Project to the form-valid subset; anything outside FORM_DEVICE_TYPES
    # collapses to "other" so callers can pre-fill the form safely.
    device_types: set[str] = set()
    for category in dme_categories:
        if category in FORM_DEVICE_TYPES:
            device_types.add(category)
        else:
            device_types.add("other")

    return {
        "codes": dme_codes,
        "all_hcpcs": all_hcpcs,
        "device_types": device_types,
        "dme_categories": dme_categories,
        "matched_keywords": matched_keywords,
    }


def unique_in_order(items: Iterable[str]) -> list[str]:
    """De-duplicate *items* while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out

"""
Shared regex helpers for extracting medical codes from free-form text.

Several modules in the codebase extract CPT and ICD-10 codes from denial
text — the RAG context builder, the USPSTF preventive-evidence helper, and
the PA requirement lookup. Centralizing the regexes here keeps their
behavior consistent (so a fix in one place doesn't silently disagree with
another) and makes it obvious which extractor to reach for.

Two flavors are exposed:

  * "Loose" extractors — match the 4-digit + alphanumeric ``CPT/CPT-II``
    shape and any well-formed ``ICD-10`` code (with or without the
    canonical period). These exist for callers (RAG, USPSTF) that want
    a broad sweep and treat codes as opaque lookup tokens.
  * Strict ``CPT/HCPCS`` extraction lives in ``pa_requirements.py``
    because PA matching needs to filter out OCR'd ICD-10 false positives.

Callers should prefer the function form over importing the patterns
directly, so the extra normalization (de-dup, surrounding-punctuation
stripping) stays in one place.
"""

import re
from typing import List

# Surrounding-punctuation form used by the legacy denial classifier.
# Matches things like "(95810)", " 95810 ", "95810." — the bracketing
# punctuation is required so we don't pick up arbitrary 5-char tokens.
LOOSE_CPT_PATTERN = re.compile(r"[\(\s:,]+(\d{4}[A-Z0-9])[\s:\.\),]", re.M | re.UNICODE)

# Permissive ICD-10 matcher. Accepts the dotted form (``Z51.11``) and the
# OCR-stripped compact form (``Z5111``). The leading-letter set excludes
# ``U`` (special-purpose codes) to mirror the historical regex.
LOOSE_ICD10_PATTERN = re.compile(
    r"[\(\s:\.,]+([A-TV-Z][0-9][0-9AB]\.?[0-9A-TV-Z]{0,4})[\s:\.\),]",
    re.M | re.UNICODE,
)


def extract_loose_cpt_codes(text: str) -> List[str]:
    """Return the de-duplicated set of CPT/CPT-II codes found in ``text``."""
    if not text:
        return []
    return list({m.group(1) for m in LOOSE_CPT_PATTERN.finditer(text)})


def extract_loose_icd10_codes(text: str) -> List[str]:
    """Return the de-duplicated set of ICD-10 codes found in ``text``."""
    if not text:
        return []
    return list({m.group(1) for m in LOOSE_ICD10_PATTERN.finditer(text)})

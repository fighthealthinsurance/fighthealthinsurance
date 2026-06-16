"""
Deterministic confidence scoring for extracted patient fields.

The LLM-based entity extractor at ``rest_views.extract_patient_fields`` returns
plain string values for ``patient_name``, ``member_id``, ``dob``, ``plan_id``,
and ``insurance_company``. Callers (chat UI, prior-auth form prefill) benefit
from a per-field confidence note so they can highlight uncertain values and
prompt the user to verify rather than silently submit them.

This module computes confidence with **no extra LLM calls** — only the
extracted value plus the source OCR/PDF text are used. The scoring is
intentionally conservative: a "high" rating requires both plausibility and
evidence that the value appears verbatim in the source.

Confidence levels: ``"high"``, ``"medium"``, ``"low"``.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from fighthealthinsurance.generate_appeal import (
    identifier_found_in_text,
    is_plausible_identifier,
)

_LABEL_TOKENS = {
    "patient",
    "name",
    "member",
    "id",
    "plan",
    "dob",
    "date",
    "birth",
    "insurance",
    "company",
    "subscriber",
}


def _appears_in_source(value: str, source_text: str) -> bool:
    """Case-insensitive substring check; trims label punctuation first."""
    cleaned = value.strip().strip(":#").strip()
    if not cleaned:
        return False
    return cleaned.lower() in source_text.lower()


def _looks_like_label(value: str) -> bool:
    """Return True when the value is a bare field label rather than data."""
    tokens = re.findall(r"[A-Za-z]+", value.lower())
    if not tokens:
        return False
    return all(tok in _LABEL_TOKENS for tok in tokens)


def _score_name(value: str, source_text: str) -> str:
    if _looks_like_label(value):
        return "low"
    word_tokens = [t for t in re.split(r"\s+", value.strip()) if t]
    in_source = _appears_in_source(value, source_text)
    if in_source and len(word_tokens) >= 2:
        return "high"
    if in_source:
        return "medium"
    return "low"


def _score_identifier(value: str, source_text: str) -> str:
    if not is_plausible_identifier(value):
        return "low"
    # Use the separator-normalizing matcher so IDs that appear in the source
    # with different separators than the extracted value (e.g. "H5521001" vs
    # "H5521-001" or "H5521 001") still count as source evidence. This is
    # still substring-based on the normalized text; it improves recall, not
    # word-boundary precision.
    if identifier_found_in_text(value, source_text):
        return "high"
    return "medium"


def _score_dob(value, source_text: str) -> str:
    """``value`` may be a ``datetime.date`` (parsed in the view) or a raw str.

    "high" requires both parseability AND evidence that the date appears in
    ``source_text`` — either as the raw string the LLM returned, or as one
    of a few common renderings of the parsed date (ISO, US slash, US dash).
    A plausible but hallucinated date that doesn't appear in the document
    drops to "medium".
    """
    parsed: Optional[datetime.date] = None
    raw_str: Optional[str] = None
    if isinstance(value, datetime.date):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw_str = value
        try:
            from dateutil import parser as _parser

            parsed = _parser.parse(value).date()
        except (ValueError, OverflowError, TypeError):
            parsed = None

    if parsed is None:
        return "low"
    today = datetime.date.today()
    if not (datetime.date(1900, 1, 1) <= parsed <= today):
        return "medium"

    # High tier: require source-text evidence. Check the raw LLM string
    # first, then a small set of common renderings so OCR variants like
    # "1/15/1980" vs "01/15/1980" still match.
    candidates = []
    if raw_str:
        candidates.append(raw_str)
    candidates.extend(
        [
            parsed.isoformat(),
            parsed.strftime("%m/%d/%Y"),
            parsed.strftime("%m-%d-%Y"),
            f"{parsed.month}/{parsed.day}/{parsed.year}",
            # Day-first renderings (e.g. "15/01/1980") so documents that write
            # the date day-first still provide source evidence. Adding these
            # only widens what counts as evidence; the date must still appear
            # in the source, so a hallucinated date cannot reach "high".
            parsed.strftime("%d/%m/%Y"),
            parsed.strftime("%d-%m-%Y"),
            f"{parsed.day}/{parsed.month}/{parsed.year}",
        ]
    )
    if any(_appears_in_source(c, source_text) for c in candidates):
        return "high"
    return "medium"


def _score_insurance_company(value: str, source_text: str) -> str:
    """High when the value both resolves to a known InsuranceCompany row
    AND appears in the source document. Resolution without source presence
    is downgraded to "medium" because the extractor could have hallucinated
    a real-but-unrelated carrier name.
    """
    if not value or not value.strip():
        return "low"
    in_source = _appears_in_source(value, source_text)
    resolved = False
    try:
        from fighthealthinsurance.pa_requirements import (
            resolve_insurance_company_by_name,
        )

        resolved = resolve_insurance_company_by_name(value) is not None
    except Exception:
        # DB lookup must never break extraction; fall through to "medium".
        pass
    if resolved and in_source:
        return "high"
    return "medium"


# Phrases that historically signaled junk extraction in the appeal
# generator — the model dropped these silently with no log line. Keeping
# the list module-level so tests can reference it.
_JUNK_FREETEXT_PHRASES = ("independent medical review",)
_JUNK_FREETEXT_EXACT = {"false"}
_JUNK_FREETEXT_CONTAINS = ("unknown",)


def score_freetext_extraction(value: Optional[str], source_text: str) -> str:
    """Score a free-text extraction (procedure, diagnosis, similar) for
    inclusion in the appeal prompt.

    Returns ``"low"`` for empty values and the historical junk filters
    (``"unknown"`` substring, exact ``"false"``, ``"independent medical
    review"`` substring) so callers can ``logger.info`` the drop and skip
    it.  ``"high"`` requires the value to appear verbatim in
    ``source_text``; otherwise ``"medium"``.

    Source ``generate_appeal.attempt_model`` used to drop low-confidence
    extractions silently; the score keeps the same rejection set but lets
    the caller decide what to log.
    """
    if not value:
        return "low"
    cleaned = value.strip()
    if not cleaned:
        return "low"
    lowered = cleaned.lower()
    if lowered in _JUNK_FREETEXT_EXACT:
        return "low"
    if any(phrase in lowered for phrase in _JUNK_FREETEXT_PHRASES):
        return "low"
    if any(token in lowered for token in _JUNK_FREETEXT_CONTAINS):
        return "low"
    if _appears_in_source(cleaned, source_text):
        return "high"
    return "medium"


def score_extracted_field(field_name: str, value, source_text: str) -> str:
    """Return a ``"high"``/``"medium"``/``"low"`` confidence label.

    Empty/None values always score ``"low"``. Unknown field names fall back to
    a simple "value appears in source" heuristic so the helper degrades
    gracefully if the extractor adds new fields later.
    """
    if value is None:
        return "low"
    if isinstance(value, str) and not value.strip():
        return "low"

    if field_name == "patient_name":
        return _score_name(str(value), source_text)
    if field_name in ("member_id", "plan_id"):
        return _score_identifier(str(value), source_text)
    if field_name == "dob":
        return _score_dob(value, source_text)
    if field_name == "insurance_company":
        return _score_insurance_company(str(value), source_text)

    # Unknown field: degrade gracefully.
    return "high" if _appears_in_source(str(value), source_text) else "medium"

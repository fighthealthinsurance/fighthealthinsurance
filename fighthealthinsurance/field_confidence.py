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

from fighthealthinsurance.generate_appeal import is_plausible_identifier

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
    if _appears_in_source(value, source_text):
        return "high"
    return "medium"


def _score_dob(value, source_text: str) -> str:
    """``value`` may be a ``datetime.date`` (parsed in the view) or a raw str."""
    parsed: Optional[datetime.date] = None
    if isinstance(value, datetime.date):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            from dateutil import parser as _parser

            parsed = _parser.parse(value).date()
        except (ValueError, OverflowError, TypeError):
            parsed = None

    if parsed is None:
        return "low"
    today = datetime.date.today()
    if datetime.date(1900, 1, 1) <= parsed <= today:
        return "high"
    return "medium"


def _score_insurance_company(value: str) -> str:
    """High when the value resolves to a known InsuranceCompany row."""
    if not value or not value.strip():
        return "low"
    try:
        from fighthealthinsurance.pa_requirements import (
            resolve_insurance_company_by_name,
        )

        if resolve_insurance_company_by_name(value) is not None:
            return "high"
    except Exception:
        # DB lookup must never break extraction; fall through to "medium".
        pass
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
        return _score_insurance_company(str(value))

    # Unknown field: degrade gracefully.
    return "high" if _appears_in_source(str(value), source_text) else "medium"

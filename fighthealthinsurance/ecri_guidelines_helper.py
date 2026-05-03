"""
Helper for surfacing evidence-based clinical practice guidelines from the
ECRI Guidelines Trust (https://guidelines.ecri.org/) — a publicly available
repository of guideline content — when crafting health insurance appeals.

The helper matches stored ``ECRIGuideline`` records to a denial's procedure
and diagnosis using whole-word keyword matching, then returns either short
citation strings (for the appeal letter's references section) or a formatted
context block (for inclusion in ML prompts).
"""

import re
from typing import Iterable, List, Optional

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.models import ECRIGuideline


def _normalize(value: Optional[str]) -> str:
    return value.strip().lower() if value else ""


# Tokens shorter than this are dropped from the match query. 2 keeps common
# clinical abbreviations (RA, MI, CT, PE, MS, CF, ...) but excludes 1-character
# noise like trailing punctuation or stray letters.
_MIN_TOKEN_LEN = 2

# Generic English/medical filler that produces noisy matches when used as a
# substring against the JSON-serialized keyword arrays. Kept short to avoid
# discarding legitimate clinical terms.
_STOP_WORDS = frozenset(
    {
        "of",
        "to",
        "in",
        "on",
        "is",
        "at",
        "by",
        "or",
        "an",
        "as",
        "be",
        "no",
        "do",
    }
)

# Hard cap on guidelines pulled into Python for whole-word post-filtering.
# The DB pre-filter is a coarse substring match on JSON blobs (no usable
# index), so we rely on the small expected table size and clamp the candidate
# set so a runaway query can't dominate appeal generation latency.
_CANDIDATE_FETCH_LIMIT = 200


def _tokens(value: str) -> List[str]:
    """Split ``value`` into deduped lowercase tokens for keyword matching."""
    seen: List[str] = []
    for raw in value.replace("/", " ").replace(",", " ").split():
        token = raw.strip().lower()
        if len(token) < _MIN_TOKEN_LEN or token in _STOP_WORDS:
            continue
        if token not in seen:
            seen.append(token)
    return seen


def _token_matches_keyword(token: str, keyword: str) -> bool:
    """True iff ``token`` appears as a whole word inside ``keyword``."""
    if not keyword:
        return False
    pattern = rf"\b{re.escape(token)}\b"
    return re.search(pattern, keyword.lower()) is not None


class ECRIGuidelinesHelper:
    """Match ``ECRIGuideline`` records against denial inputs."""

    @staticmethod
    def _build_keyword_filter(field_name: str, tokens: Iterable[str]) -> Optional[Q]:
        """Build an OR-ed icontains filter for a JSON list field.

        ``ECRIGuideline.procedure_keywords`` / ``diagnosis_keywords`` are stored
        as JSON lists. ``__icontains`` against the JSON-serialized text gives a
        cheap, dialect-agnostic candidate fetch; correctness is enforced by a
        whole-word check in Python before results are returned.
        """
        q: Optional[Q] = None
        for token in tokens:
            clause = Q(**{f"{field_name}__icontains": token})
            q = clause if q is None else q | clause
        return q

    @staticmethod
    def _guideline_matches_tokens(
        tokens: Iterable[str], guideline: ECRIGuideline
    ) -> bool:
        """True iff any token whole-word-matches a procedure/diagnosis keyword."""
        keywords: List[str] = []
        keywords.extend(guideline.procedure_keywords or [])
        keywords.extend(guideline.diagnosis_keywords or [])
        for token in tokens:
            for keyword in keywords:
                if _token_matches_keyword(token, keyword):
                    return True
        return False

    @classmethod
    async def find_relevant_guidelines(
        cls,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        max_results: int = 3,
    ) -> List[ECRIGuideline]:
        """Return up to ``max_results`` active guidelines matching the inputs.

        A guideline matches when any procedure/diagnosis token appears as a
        whole word in one of the guideline's ``procedure_keywords`` or
        ``diagnosis_keywords`` entries. Topic labels (``topics``) are too
        broad to drive matching and are not consulted here.
        """
        procedure_norm = _normalize(procedure)
        diagnosis_norm = _normalize(diagnosis)
        if not procedure_norm and not diagnosis_norm:
            return []

        tokens: List[str] = []
        for source in (procedure_norm, diagnosis_norm):
            for token in _tokens(source):
                if token not in tokens:
                    tokens.append(token)
        if not tokens:
            return []

        filters: Optional[Q] = None
        for field in ("procedure_keywords", "diagnosis_keywords"):
            clause = cls._build_keyword_filter(field, tokens)
            if clause is not None:
                filters = clause if filters is None else filters | clause
        if filters is None:
            return []

        try:
            results: List[ECRIGuideline] = []
            async for guideline in (
                ECRIGuideline.objects.filter(is_active=True)
                .filter(filters)
                .order_by("-publication_date", "title")[:_CANDIDATE_FETCH_LIMIT]
            ):
                if cls._guideline_matches_tokens(tokens, guideline):
                    results.append(guideline)
                    if len(results) >= max_results:
                        break
            return results
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error querying ECRI guidelines for {procedure_norm}/"
                f"{diagnosis_norm}: {e}"
            )
            return []

    @classmethod
    async def get_citations(
        cls,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        max_citations: int = 3,
    ) -> List[str]:
        """Return citation strings ready to append to an appeal's references."""
        guidelines = await cls.find_relevant_guidelines(
            procedure=procedure,
            diagnosis=diagnosis,
            max_results=max_citations,
        )
        return [g.citation_string() for g in guidelines]

    @classmethod
    async def get_context(
        cls,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        max_guidelines: int = 3,
        max_chars_per_guideline: int = 1200,
    ) -> str:
        """Return a markdown context block for inclusion in ML prompts.

        Returns an empty string when no matching guidelines are found.
        """
        guidelines = await cls.find_relevant_guidelines(
            procedure=procedure,
            diagnosis=diagnosis,
            max_results=max_guidelines,
        )
        if not guidelines:
            return ""

        parts: List[str] = [
            "## Evidence-Based Clinical Practice Guidelines",
            "The following guideline summaries are drawn from the ECRI "
            "Guidelines Trust, a public repository of evidence-based clinical "
            "practice guideline content. Cite them only as the references "
            "they are; do not invent additional details.",
        ]
        for guideline in guidelines:
            header_bits = [guideline.title]
            if guideline.developer_organization:
                header_bits.append(guideline.developer_organization)
            if guideline.publication_date:
                header_bits.append(str(guideline.publication_date.year))
            parts.append("\n### " + " — ".join(header_bits))
            if guideline.intended_population:
                parts.append(f"*Population:* {guideline.intended_population}")
            if guideline.evidence_quality:
                parts.append(f"*Evidence quality:* {guideline.evidence_quality}")
            summary = guideline.recommendations_summary or ""
            if summary:
                if len(summary) > max_chars_per_guideline:
                    summary = summary[:max_chars_per_guideline] + "..."
                parts.append(summary)
            if guideline.url:
                parts.append(f"Source: {guideline.url}")
        return "\n".join(parts)

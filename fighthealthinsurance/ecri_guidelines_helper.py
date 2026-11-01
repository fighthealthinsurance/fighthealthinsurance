"""
Helper for surfacing evidence-based clinical practice guidelines from the
ECRI Guidelines Trust (https://guidelines.ecri.org/) — a publicly available
repository of guideline content — when crafting health insurance appeals.

The helper matches stored ``ECRIGuideline`` records to a denial's procedure
and diagnosis with the same closeness rule as the Medicare coverage (NCD)
lookup, then returns short citation strings for the appeal letter's
references section.
"""

from typing import Iterable, List, Optional

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.cms_coverage_api import (
    _meaningful_tokens,
    _title_matches,
)
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


def _prefilter_tokens(value: str) -> List[str]:
    """Words for the coarse database fetch: each word as written and in
    singular form, so a stored "biopsies" is still fetched for "biopsy".
    Which candidates actually match is decided by ``_keyword_match_key``."""
    out: List[str] = []
    for token in _tokens(value):
        for word in [token] + _meaningful_tokens(token):
            if word not in out:
                out.append(word)
    return out


def _keyword_match_key(query_tokens: List[str], keyword: str):
    """How closely one of a guideline's keyword phrases fits the query, or
    None.

    The NCD lookup's rule (``cms_coverage_api._title_matches``), with the
    keyword phrase in place of the title: one shared word is not enough
    ("stable angina" against an "unstable angina" phrase, or any phrase
    sharing only a generic word such as "therapy"), but a phrase the query
    names in full is ("pci" in "pci of the lad").
    """
    if not keyword or not query_tokens:
        return None
    return _title_matches(query_tokens, keyword)


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
    def _guideline_match_key(queries: Iterable[List[str]], guideline: ECRIGuideline):
        """The best closeness key of any of the guideline's procedure or
        diagnosis keyword phrases against any of the queries (the
        procedure's tokens and the diagnosis's, each on its own), or None
        when no phrase fits closely enough."""
        keywords: List[str] = []
        keywords.extend(guideline.procedure_keywords or [])
        keywords.extend(guideline.diagnosis_keywords or [])
        best = None
        for query_tokens in queries:
            for keyword in keywords:
                key = _keyword_match_key(query_tokens, keyword)
                if key is not None and (best is None or key < best):
                    best = key
        return best

    @classmethod
    async def find_relevant_guidelines(
        cls,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        max_results: int = 3,
    ) -> List[ECRIGuideline]:
        """Return up to ``max_results`` active guidelines matching the inputs.

        A guideline matches when the procedure or the diagnosis names one of
        its ``procedure_keywords`` or ``diagnosis_keywords`` phrases closely
        (``_keyword_match_key``); one shared word, or a shared generic word
        such as "therapy", is not enough. Matches are ranked by how closely
        they fit, then by publication date. Topic labels (``topics``) are too
        broad to drive matching and are not consulted here.
        """
        procedure_norm = _normalize(procedure)
        diagnosis_norm = _normalize(diagnosis)
        if not procedure_norm and not diagnosis_norm:
            return []

        queries = [
            q
            for q in (
                _meaningful_tokens(procedure_norm),
                _meaningful_tokens(diagnosis_norm),
            )
            if q
        ]
        if not queries:
            return []
        tokens: List[str] = []
        for source in (procedure_norm, diagnosis_norm):
            for token in _prefilter_tokens(source):
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
            scored: List[tuple] = []
            async for guideline in (
                ECRIGuideline.objects.filter(is_active=True)
                .filter(filters)
                .order_by("-publication_date", "title")[:_CANDIDATE_FETCH_LIMIT]
            ):
                key = cls._guideline_match_key(queries, guideline)
                if key is not None:
                    # The fetch order (newest first) breaks ties.
                    scored.append((key, len(scored), guideline))
            scored.sort(key=lambda x: (x[0], x[1]))
            return [g for _, _, g in scored[:max_results]]
        except Exception as e:
            # Only the exception's type: the procedure and diagnosis are the
            # person's, and a database error's message or traceback can
            # repeat the query's values.
            logger.warning(f"Error querying ECRI guidelines ({type(e).__name__})")
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

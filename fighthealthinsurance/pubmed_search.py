"""Structured PubMed search helpers.

Pure functions for building PubMed query strings with structured filters
(publication type, date range, MeSH), classifying evidence strength from
publication types, and formatting articles into appeal-friendly snippets.

Kept separate from `pubmed_tools.py` (which holds the I/O-heavy ``PubMedTools``
class) so these helpers can be unit tested without database or network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


class EvidenceStrength(str, Enum):
    """Evidence strength tiers used to categorize PubMed articles.

    The string values are stable identifiers safe to expose in JSON/API
    responses and template strings.
    """

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    UNKNOWN = "unknown"


# Publication types that constitute the strongest tier of medical evidence.
# These are typically what insurers and reviewers find most persuasive in an
# appeal: meta-analyses, systematic reviews, RCTs, and formal guidelines.
STRONG_PUB_TYPES: Set[str] = {
    "meta-analysis",
    "systematic review",
    "randomized controlled trial",
    "practice guideline",
    "guideline",
    "clinical trial, phase iii",
    "clinical trial, phase iv",
    "consensus development conference",
    "consensus development conference, nih",
}

# Moderate-strength evidence: useful but less authoritative than the strong
# tier. Still worth including in appeals, especially for newer therapies that
# lack large RCTs.
MODERATE_PUB_TYPES: Set[str] = {
    "clinical trial",
    "clinical trial, phase i",
    "clinical trial, phase ii",
    "controlled clinical trial",
    "multicenter study",
    "comparative study",
    "observational study",
    "validation study",
    "review",
    "pragmatic clinical trial",
    "equivalence trial",
}

# Weaker-but-relevant evidence. Case reports and editorials still get
# surfaced, but bucketed separately so the appeal letter can lead with
# stronger sources.
WEAK_PUB_TYPES: Set[str] = {
    "case reports",
    "editorial",
    "comment",
    "letter",
    "news",
    "historical article",
    "personal narrative",
}


# PubMed publication-type filter tokens. These map to PubMed's ``[pt]`` field
# search and are documented at
# https://www.ncbi.nlm.nih.gov/books/NBK3827/#pubmedhelp.Publication_Types_PT
PUB_TYPE_FILTERS: Dict[str, str] = {
    "meta_analysis": "Meta-Analysis[pt]",
    "systematic_review": "Systematic Review[pt]",
    "rct": "Randomized Controlled Trial[pt]",
    "guideline": "Guideline[pt] OR Practice Guideline[pt]",
    "clinical_trial": "Clinical Trial[pt]",
    "review": "Review[pt]",
}


# Preset bundles for common appeal-relevant filter combinations. Each preset
# expands to a set of publication-type filter tokens joined with ``OR``.
PUB_TYPE_PRESETS: Dict[str, Sequence[str]] = {
    "guidelines_and_systematic_reviews": (
        "guideline",
        "systematic_review",
        "meta_analysis",
    ),
    "high_quality_evidence": (
        "guideline",
        "systematic_review",
        "meta_analysis",
        "rct",
    ),
    "any_evidence": (
        "guideline",
        "systematic_review",
        "meta_analysis",
        "rct",
        "clinical_trial",
        "review",
    ),
}


# Characters that have meaning inside a PubMed query string. We escape them
# (or strip them) when interpolating user-provided text into a query.
_PUBMED_QUERY_RESERVED_RE = re.compile(r'[\[\]\(\)"]')


def _sanitize_term(term: str) -> str:
    """Strip control chars and PubMed-reserved punctuation from a search term.

    Preserves spaces and basic ASCII letters/digits/hyphens so that multi-word
    medical phrases ("rheumatoid arthritis", "anti-tnf") survive unchanged.
    """
    if not term:
        return ""
    cleaned = _PUBMED_QUERY_RESERVED_RE.sub(" ", term)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _quote_phrase(term: str) -> str:
    """Wrap a multi-word term in double quotes for exact-phrase matching."""
    cleaned = _sanitize_term(term)
    if not cleaned:
        return ""
    if " " in cleaned:
        return f'"{cleaned}"'
    return cleaned


@dataclass
class StructuredQuery:
    """A composed PubMed query plus the components that produced it.

    Carrying the components alongside the final string lets callers report
    "we searched for X with filters Y" in UI/logs without re-parsing.
    """

    query: str
    condition: str = ""
    treatment: str = ""
    pub_type_filters: List[str] = field(default_factory=list)
    mesh_terms: List[str] = field(default_factory=list)
    since_year: Optional[str] = None
    until_year: Optional[str] = None

    def __str__(self) -> str:
        return self.query


def build_structured_query(
    condition: Optional[str] = None,
    treatment: Optional[str] = None,
    pub_type_preset: Optional[str] = None,
    pub_type_filters: Optional[Sequence[str]] = None,
    mesh_terms: Optional[Sequence[str]] = None,
    since_year: Optional[str] = None,
    until_year: Optional[str] = None,
    extra_terms: Optional[Sequence[str]] = None,
) -> StructuredQuery:
    """Build a structured PubMed query from condition + treatment + filters.

    Args:
        condition: The disease/diagnosis (e.g., "rheumatoid arthritis").
            Multi-word phrases are quoted automatically.
        treatment: The procedure/medication being appealed (e.g., "physical
            therapy", "infliximab").
        pub_type_preset: Name of a preset from ``PUB_TYPE_PRESETS`` (e.g.,
            ``"guidelines_and_systematic_reviews"``). Mutually inclusive with
            ``pub_type_filters`` — both are combined with ``OR``.
        pub_type_filters: Explicit list of filter keys from
            ``PUB_TYPE_FILTERS`` (e.g., ``["rct", "meta_analysis"]``).
        mesh_terms: MeSH terms to require (joined with ``AND``). Each is
            tagged ``[mh]`` so PubMed treats it as a controlled-vocabulary
            lookup rather than free-text.
        since_year: Earliest publication year to include (inclusive). Renders
            as a PubMed date-range filter ``"YYYY"[dp] : "YYYY"[dp]``.
        until_year: Latest publication year (defaults to current year when
            ``since_year`` is set).
        extra_terms: Free-text terms to append (e.g., microsite-specific
            keywords). Joined with ``AND``.

    Returns:
        A ``StructuredQuery`` whose ``.query`` is the assembled PubMed string
        ready for ``pmids_for_query``. If no inputs produce any tokens, the
        ``.query`` will be an empty string.
    """
    parts: List[str] = []

    condition_clean = _quote_phrase(condition or "")
    treatment_clean = _quote_phrase(treatment or "")

    if condition_clean and treatment_clean:
        parts.append(f"({condition_clean} AND {treatment_clean})")
    elif condition_clean:
        parts.append(condition_clean)
    elif treatment_clean:
        parts.append(treatment_clean)

    # MeSH terms are joined with AND so each one constrains results.
    mesh_clean: List[str] = []
    if mesh_terms:
        for term in mesh_terms:
            sanitized = _sanitize_term(term)
            if sanitized:
                mesh_clean.append(sanitized)
                parts.append(f'"{sanitized}"[mh]')

    # Free-text "extra" terms (microsite keywords, etc.).
    if extra_terms:
        for term in extra_terms:
            quoted = _quote_phrase(term)
            if quoted:
                parts.append(quoted)

    # Combine pub-type preset + explicit filters into a single OR group.
    resolved_filters: List[str] = []
    if pub_type_preset:
        for key in PUB_TYPE_PRESETS.get(pub_type_preset, ()):
            if key in PUB_TYPE_FILTERS:
                resolved_filters.append(key)
    if pub_type_filters:
        for key in pub_type_filters:
            if key in PUB_TYPE_FILTERS and key not in resolved_filters:
                resolved_filters.append(key)

    if resolved_filters:
        filter_tokens = [PUB_TYPE_FILTERS[k] for k in resolved_filters]
        parts.append("(" + " OR ".join(filter_tokens) + ")")

    # Date-range filter. PubMed wants both ends, so synthesize an upper bound
    # from the current year if only `since_year` is supplied.
    since_clean: Optional[str] = None
    until_clean: Optional[str] = None
    if since_year:
        since_clean = _sanitize_term(str(since_year))
    if until_year:
        until_clean = _sanitize_term(str(until_year))
    if since_clean:
        upper = until_clean or str(datetime.now().year)
        parts.append(f'("{since_clean}"[dp] : "{upper}"[dp])')
    elif until_clean:
        parts.append(f'("1900"[dp] : "{until_clean}"[dp])')

    query_str = " AND ".join(parts)

    return StructuredQuery(
        query=query_str,
        condition=condition_clean,
        treatment=treatment_clean,
        pub_type_filters=resolved_filters,
        mesh_terms=mesh_clean,
        since_year=since_clean,
        until_year=until_clean,
    )


def normalize_pub_type(pub_type: Any) -> str:
    """Lowercase and trim a publication-type label for set-based comparison."""
    if pub_type is None:
        return ""
    return str(pub_type).strip().lower()


def extract_publication_types(article: Any) -> List[str]:
    """Pull a list of publication-type strings off a metapub-like article.

    Tries the common attribute names in order: ``publication_types``,
    ``pubtype``, ``pub_types``. Accepts a list, a dict (uses its values), a
    single string, or ``None``. Returns ``[]`` if nothing usable is found.
    """
    for attr in ("publication_types", "pubtype", "pub_types"):
        value = getattr(article, attr, None)
        if value is None:
            continue
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value if v]
    return []


def classify_evidence_strength(pub_types: Iterable[str]) -> EvidenceStrength:
    """Classify a publication-type list into an ``EvidenceStrength`` tier.

    The strongest tier present wins: an article tagged both "Review" (moderate)
    and "Meta-Analysis" (strong) classifies as STRONG. Empty/unknown types
    yield ``EvidenceStrength.UNKNOWN`` so callers can decide whether to
    surface them.
    """
    if not pub_types:
        return EvidenceStrength.UNKNOWN

    normalized = {normalize_pub_type(p) for p in pub_types if p}
    if not normalized:
        return EvidenceStrength.UNKNOWN

    if normalized & STRONG_PUB_TYPES:
        return EvidenceStrength.STRONG
    if normalized & MODERATE_PUB_TYPES:
        return EvidenceStrength.MODERATE
    if normalized & WEAK_PUB_TYPES:
        return EvidenceStrength.WEAK
    return EvidenceStrength.UNKNOWN


def categorize_articles_by_strength(
    articles: Iterable[Any],
    pub_types_lookup: Optional[Dict[str, List[str]]] = None,
) -> Dict[EvidenceStrength, List[Any]]:
    """Bucket articles by evidence strength.

    Args:
        articles: Iterable of article-like objects with a ``pmid`` attribute.
        pub_types_lookup: Optional ``{pmid: [pub_type, ...]}`` mapping. When
            provided, takes precedence over publication types read directly
            off the article object. Useful when types were fetched separately
            (e.g., via ``efetch``) rather than stored on the article.

    Returns:
        A dict keyed by ``EvidenceStrength`` whose values are the article
        objects in their original order. All four tiers are always present
        (possibly with empty lists) so callers can render consistent
        "strong / moderate / weak / unknown" sections.
    """
    buckets: Dict[EvidenceStrength, List[Any]] = {
        EvidenceStrength.STRONG: [],
        EvidenceStrength.MODERATE: [],
        EvidenceStrength.WEAK: [],
        EvidenceStrength.UNKNOWN: [],
    }
    for article in articles:
        pmid = getattr(article, "pmid", None)
        types: List[str] = []
        if pub_types_lookup and pmid and pmid in pub_types_lookup:
            types = pub_types_lookup[pmid]
        else:
            types = extract_publication_types(article)
        strength = classify_evidence_strength(types)
        buckets[strength].append(article)
    return buckets


# How many sentences of the abstract to include in an appeal-friendly snippet.
# Short enough to be quotable in a letter; long enough to convey the finding.
SNIPPET_SENTENCE_COUNT = 3
SNIPPET_MAX_CHARS = 600


def _split_sentences(text: str) -> List[str]:
    """Split a paragraph into sentence-like chunks."""
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def format_appeal_snippet(
    article: Any,
    sentence_count: int = SNIPPET_SENTENCE_COUNT,
    max_chars: int = SNIPPET_MAX_CHARS,
) -> str:
    """Format an article as a short, appeal-letter-ready snippet.

    The output is a single line of the form::

        Title (Journal, Year). PMID: 12345 [URL]
        Excerpt: <first N sentences of summary or abstract>

    Prefers ``basic_summary`` (already condensed) over the raw abstract.
    Truncates to ``max_chars`` so a snippet stays quotable in a letter.
    """
    title = (getattr(article, "title", "") or "").strip()
    journal = (getattr(article, "journal", "") or "").strip()
    year = getattr(article, "year", "") or ""
    if year is None:
        year = ""
    year = str(year).strip()
    pmid = (getattr(article, "pmid", "") or "").strip()
    url = (getattr(article, "article_url", "") or "").strip()
    summary = (
        getattr(article, "basic_summary", None)
        or getattr(article, "abstract", None)
        or ""
    ).strip()

    citation_parts: List[str] = []
    if title:
        citation_parts.append(title)
    venue: List[str] = []
    if journal:
        venue.append(journal)
    if year:
        venue.append(year)
    if venue:
        citation_parts.append("(" + ", ".join(venue) + ")")
    if pmid:
        citation_parts.append(f"PMID: {pmid}")
    if url:
        citation_parts.append(f"[{url}]")
    citation = " ".join(citation_parts).strip()

    excerpt = ""
    if summary:
        sentences = _split_sentences(summary)
        if sentences:
            excerpt = " ".join(sentences[:sentence_count]).strip()
        if not excerpt:
            excerpt = summary
        if len(excerpt) > max_chars:
            excerpt = excerpt[: max_chars - 1].rstrip() + "…"

    if citation and excerpt:
        return f"{citation}\nExcerpt: {excerpt}"
    return citation or excerpt


def format_categorized_snippets(
    buckets: Dict[EvidenceStrength, List[Any]],
    include_empty: bool = False,
) -> str:
    """Render evidence buckets as a multi-section markdown-ish text block.

    Output looks like::

        ## Strong evidence
        - <snippet 1>
        - <snippet 2>

        ## Moderate evidence
        - ...

    Sections with no articles are omitted unless ``include_empty=True``.
    """
    section_titles: List[Tuple[EvidenceStrength, str]] = [
        (EvidenceStrength.STRONG, "Strong evidence"),
        (EvidenceStrength.MODERATE, "Moderate evidence"),
        (EvidenceStrength.WEAK, "Weak but relevant evidence"),
        (EvidenceStrength.UNKNOWN, "Other evidence"),
    ]
    sections: List[str] = []
    for strength, title in section_titles:
        articles = buckets.get(strength, [])
        if not articles and not include_empty:
            continue
        lines = [f"## {title}"]
        if not articles:
            lines.append("(no articles)")
        else:
            for article in articles:
                snippet = format_appeal_snippet(article)
                if snippet:
                    lines.append("- " + snippet.replace("\n", "\n  "))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)

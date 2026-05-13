"""Utilities for gathering, truncating, and merging appeal/denial context.

Centralizes patterns that were previously scattered across the codebase:

- ``truncate_at_boundary`` — boundary-aware truncation that prefers
  paragraph/sentence breaks over abrupt mid-word cuts. Replaces ad-hoc
  ``text[:N] + "..."`` slicing in extralink, RAG, PubMed, and plan-doc
  helpers.

- ``dedupe_blocks`` / ``merge_context_blocks`` — normalize-and-dedupe
  optional context strings before joining them with a separator, with an
  optional cumulative ``max_chars`` budget so a single oversized block
  cannot crowd out the rest of the prompt.

- ``flatten_citation_context`` / ``attach_supplemental_to_citations`` —
  the recurring pattern in ``common_view_logic`` where microsite/IMR
  context is appended to the ML citation context if present, else to the
  PubMed context, else used standalone.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Tuple, Union

# Sentence-ending punctuation followed by whitespace or end-of-string.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")

DEFAULT_ELLIPSIS = "..."

CitationContext = Optional[Union[str, List[Any]]]


def truncate_at_boundary(
    text: Optional[str],
    max_chars: int,
    ellipsis: str = DEFAULT_ELLIPSIS,
    min_keep_ratio: float = 0.5,
) -> str:
    """Truncate ``text`` to at most ``max_chars`` characters at a boundary.

    Preference order: paragraph break (``\\n\\n``), sentence terminator,
    word boundary, hard cut. ``ellipsis`` is appended when truncation
    occurs.

    ``min_keep_ratio`` controls how aggressively we'll back off looking
    for a clean boundary: at 0.5 (default) we keep at least half the
    budget rather than e.g. cutting after the first sentence in a long
    document. Returns the input unchanged when it already fits, and an
    empty string when ``text`` is empty/None or ``max_chars`` <= 0.

    Output length is guaranteed to be ``<= max_chars``. When
    ``max_chars`` is smaller than ``len(ellipsis)`` there is no room to
    signal truncation, so the function hard-cuts at ``max_chars`` and
    omits the ellipsis entirely.
    """
    if not text or max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    if max_chars <= len(ellipsis):
        return text[:max_chars]

    budget = max_chars - len(ellipsis)
    window = text[:budget]
    min_cut = int(budget * min_keep_ratio)

    para_break = window.rfind("\n\n")
    if para_break >= min_cut:
        return window[:para_break].rstrip() + ellipsis

    last_sentence = -1
    for match in _SENTENCE_END_RE.finditer(window):
        end = match.end()
        if end >= min_cut:
            last_sentence = end
    if last_sentence > 0:
        return window[:last_sentence].rstrip() + ellipsis

    space = window.rfind(" ")
    if space >= min_cut:
        return window[:space].rstrip() + ellipsis

    return window + ellipsis


def _normalize_for_dedup(s: str) -> str:
    """Collapse whitespace and lowercase for dedup comparisons."""
    return " ".join(s.lower().split())


def dedupe_blocks(
    blocks: Iterable[Optional[str]],
    *,
    min_chars: int = 1,
) -> List[str]:
    """Drop empty, whitespace-only, and duplicate blocks.

    Comparison is case- and whitespace-insensitive. The first occurrence
    of each unique block is kept; later duplicates are dropped.
    ``min_chars`` filters trivially short blocks (default keeps every
    non-empty block).
    """
    seen: set[str] = set()
    out: List[str] = []
    for block in blocks:
        if not block:
            continue
        stripped = block.strip()
        if len(stripped) < min_chars:
            continue
        key = _normalize_for_dedup(stripped)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(stripped)
    return out


def merge_context_blocks(
    blocks: Iterable[Optional[str]],
    *,
    separator: str = "\n\n",
    dedupe: bool = True,
    max_chars: Optional[int] = None,
) -> str:
    """Join non-empty context blocks into a single string.

    When ``dedupe`` is set, duplicate blocks (case- and
    whitespace-insensitive) are removed before joining. When
    ``max_chars`` is set, blocks are added in order until the next would
    exceed the budget; remaining blocks are dropped. Returns the empty
    string when no usable blocks remain.
    """
    if dedupe:
        cleaned = dedupe_blocks(blocks)
    else:
        cleaned = [b.strip() for b in blocks if b and b.strip()]
    if not cleaned:
        return ""
    if max_chars is None:
        return separator.join(cleaned)

    parts: List[str] = []
    used = 0
    sep_len = len(separator)
    for block in cleaned:
        cost = len(block) + (sep_len if parts else 0)
        if used + cost > max_chars:
            break
        parts.append(block)
        used += cost
    return separator.join(parts)


def _supplemental_already_present(existing: str, supplemental: str) -> bool:
    """Whether ``supplemental`` already appears as a block in ``existing``.

    Both strings are split on paragraph breaks (``\\n\\n``) and the
    supplemental's normalized blocks must appear as a contiguous
    subsequence within the existing blocks. This is stricter than a
    naive substring check on the normalized text: a short supplemental
    that coincidentally appears inside an unrelated longer block is not
    treated as a duplicate.
    """
    if not existing or not supplemental:
        return False

    existing_blocks = [
        _normalize_for_dedup(b) for b in existing.split("\n\n") if b.strip()
    ]
    supp_blocks = [
        _normalize_for_dedup(b) for b in supplemental.split("\n\n") if b.strip()
    ]
    if not existing_blocks or not supp_blocks:
        return False
    n, m = len(existing_blocks), len(supp_blocks)
    if m > n:
        return False
    for i in range(n - m + 1):
        if existing_blocks[i : i + m] == supp_blocks:
            return True
    return False


def flatten_citation_context(value: CitationContext) -> str:
    """Render a list-or-string citation context as a single string.

    ML citation helpers sometimes return ``list[str]`` and other times a
    pre-joined ``str``; downstream prompt construction needs a single
    string regardless. Whitespace-only list entries are filtered out so
    they don't render as blank lines. Returns the empty string for
    None/empty inputs.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        cleaned = [str(c).strip() for c in value if c is not None]
        return "\n".join(c for c in cleaned if c)
    return str(value).strip()


def attach_supplemental_to_citations(
    ml_citation_context: CitationContext,
    pubmed_context: Optional[str],
    supplemental: Optional[str],
) -> Tuple[CitationContext, Optional[str]]:
    """Append ``supplemental`` to whichever citation context is set.

    Mirrors the pattern in ``common_view_logic.py`` for merging
    microsite/IMR context into the appeal pipeline:
      1. If ``ml_citation_context`` is non-empty, append to it (flattening
         a list to a string first).
      2. Otherwise, if ``pubmed_context`` is non-empty, append to it.
      3. Otherwise, use ``supplemental`` as a standalone
         ``ml_citation_context``.

    Returns the updated ``(ml_citation_context, pubmed_context)`` pair.
    Supplemental content is deduplicated against the existing block it's
    being appended to, so re-running with the same supplemental does not
    cause unbounded growth.
    """
    if not supplemental or not supplemental.strip():
        return ml_citation_context, pubmed_context

    supp = supplemental.strip()

    flat_ml = flatten_citation_context(ml_citation_context)
    if flat_ml:
        if _supplemental_already_present(flat_ml, supp):
            return flat_ml, pubmed_context
        return f"{flat_ml}\n\n{supp}", pubmed_context

    if pubmed_context and pubmed_context.strip():
        existing = pubmed_context.strip()
        if _supplemental_already_present(existing, supp):
            return ml_citation_context, existing
        return ml_citation_context, f"{existing}\n\n{supp}"

    return supp, pubmed_context

"""
Grep-style search over long document text, with ranked passages.

Shared by the chat's search_documents tool (chat/document_search.py), which
greps a chat's stored pastes and uploads one query at a time, and by the
appeal flow's plan-document helper (ml/ml_plan_doc_helper.py), which fills a
model's window from plan documents too long to send whole, using the search
strings a local model chose.

Matching is literal and case-insensitive, at the start of a word (a stem
finds its forms: "authoriz" finds "authorization"; "age" stays out of
"coverage"), and any run of whitespace stands for a space, since pasted and
extracted text wraps its lines mid-phrase. Queries are model-written, so
nothing in one is ever compiled as regex syntax.

Pure: no Django, no models, so anything may import it.
"""

import bisect
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# Context kept on each side of a hit.
SNIPPET_RADIUS_CHARS = 300
# Words a query may contribute, and the shortest that counts. A query is
# written on purpose, so short codes and abbreviations ("PT", "IV") count.
MAX_QUERY_TERMS = 12
MIN_TERM_LENGTH = 2
# Hit positions kept per needle. Counts stay exact past it; only the
# passages are drawn from the first ones.
MAX_HITS_PER_NEEDLE = 500
# A hit on a whole query or a quoted phrase outweighs a single word's.
PHRASE_WEIGHT = 3.0
TERM_WEIGHT = 1.0
# Score a passage gains for each OTHER query with a hit inside it, when
# several queries are selected for together.
OTHER_QUERY_BONUS = 1.0
# How far a snippet edge may move to land on a line break, or a space.
_SNAP_TO_LINE_CHARS = 80
_SNAP_TO_SPACE_CHARS = 30

# Words excluded from query term extraction. Includes common English stopwords
# plus conversational filler that isn't meaningful for document search.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "about",
        "what",
        "which",
        "who",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "me",
        "him",
        "us",
        "them",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "and",
        "but",
        "or",
        "if",
        "up",
        "also",
        "tell",
        "say",
        "says",
        "said",
        "please",
        "thank",
        "thanks",
        "hi",
        "hello",
        "document",
        "file",
        "uploaded",
        "page",
    }
)

WORD_RE = re.compile(r"[a-zA-Z0-9]+(?:[-'][a-zA-Z0-9]+)*")
QUOTED_RE = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class Needle:
    """One thing a search looks for."""

    label: str
    weight: float
    pattern: "re.Pattern[str]"


@dataclass(frozen=True)
class Passage:
    """``texts[source][start:end]``, found around the hit at ``hit``."""

    source: int
    start: int
    end: int
    hit: int
    score: float


def needle_pattern(needle: str) -> "re.Pattern[str]":
    """A literal, case-insensitive pattern for ``needle``, matching at the
    start of a word, with any run of whitespace standing for a space."""
    body = r"\s+".join(re.escape(word) for word in needle.split())
    return re.compile(r"(?<![A-Za-z0-9])" + body, re.IGNORECASE)


def whole_phrase_pattern(phrase: str) -> "re.Pattern[str]":
    """``needle_pattern`` closed at both ends: the phrase as whole words.

    For a phrase that stands for a place or a name rather than a stem,
    where "target" must not find "targeted therapy" and "san jose" must not
    find "San Joseph". ``phrase`` must not be empty: an empty needle matches
    at the start of any text.
    """
    return re.compile(
        needle_pattern(phrase).pattern + r"(?![A-Za-z0-9])", re.IGNORECASE
    )


def query_needles(query: str) -> List[Needle]:
    """What a query looks for: its quoted phrases and, for a query of more
    than one word, the whole query as a phrase (both weighted higher), then
    each of its words that is not a stopword. A query of nothing but
    stopwords is looked for as written."""
    needles: List[Needle] = []
    seen: set = set()

    def add(text: str, weight: float) -> None:
        label = " ".join(text.lower().split())
        if label and label not in seen:
            seen.add(label)
            needles.append(Needle(label, weight, needle_pattern(label)))

    for phrase in QUOTED_RE.findall(query):
        add(phrase, PHRASE_WEIGHT)
    unquoted = query.replace('"', " ")
    if len(unquoted.split()) > 1:
        add(unquoted, PHRASE_WEIGHT)
    terms = [
        word
        for word in WORD_RE.findall(unquoted.lower())
        if word not in STOPWORDS and len(word) >= MIN_TERM_LENGTH
    ]
    for term in terms[:MAX_QUERY_TERMS]:
        add(term, TERM_WEIGHT)
    if not needles:
        add(unquoted, TERM_WEIGHT)
    return needles


def needle_hits(text: str, needle: Needle) -> Tuple[int, List[Tuple[int, int]]]:
    """How often ``needle`` occurs in ``text``, and the spans of its first
    MAX_HITS_PER_NEEDLE occurrences."""
    count = 0
    spans: List[Tuple[int, int]] = []
    for match in needle.pattern.finditer(text):
        count += 1
        if len(spans) < MAX_HITS_PER_NEEDLE:
            spans.append(match.span())
    return count, spans


def _snap_start(text: str, pos: int) -> int:
    """Move a snippet's start back to a line start, or a word start, nearby."""
    if pos <= 0:
        return 0
    line = text.rfind("\n", max(0, pos - _SNAP_TO_LINE_CHARS), pos)
    if line != -1:
        return line + 1
    space = text.rfind(" ", max(0, pos - _SNAP_TO_SPACE_CHARS), pos)
    return space + 1 if space != -1 else pos


def _snap_end(text: str, pos: int) -> int:
    """Move a snippet's end on to a line end, or a word end, nearby."""
    if pos >= len(text):
        return len(text)
    line = text.find("\n", pos, pos + _SNAP_TO_LINE_CHARS)
    if line != -1:
        return line
    space = text.find(" ", pos, pos + _SNAP_TO_SPACE_CHARS)
    return space if space != -1 else pos


def _candidate_passages(
    text: str,
    needles: Sequence[Needle],
    hits: Sequence[List[Tuple[int, int]]],
) -> List[Tuple[float, int, int, int]]:
    """A passage around every hit, as ``(score, start, end, hit_start)``.

    A passage scores each needle with a hit inside it, more for a phrase
    than a word, with diminishing returns for repeats, so the passages where
    the most of the query occurs together rank first.
    """
    hit_starts = [[start for start, _ in spans] for spans in hits]
    candidates: List[Tuple[float, int, int, int]] = []
    for spans in hits:
        for hit_start, hit_end in spans:
            lo = hit_start - SNIPPET_RADIUS_CHARS
            hi = hit_end + SNIPPET_RADIUS_CHARS
            score = 0.0
            for needle, starts in zip(needles, hit_starts):
                inside = bisect.bisect_right(starts, hi) - bisect.bisect_left(
                    starts, lo
                )
                if inside:
                    score += needle.weight * (1.0 + min(inside - 1, 4) * 0.25)
            candidates.append(
                (score, _snap_start(text, lo), _snap_end(text, hi), hit_start)
            )
    return candidates


def select_passages(
    texts: Sequence[str],
    queries: Sequence[str],
    budget_chars: int,
    max_passages: int,
    per_passage_overhead: int = 0,
) -> List[Passage]:
    """Non-overlapping passages of ``texts`` that best cover ``queries``.

    Passages are taken in rounds, each query's best remaining passage in
    turn, so a query with hits on every page ("coverage") cannot crowd out
    one with a single hit ("J0135"). Within a query, a passage scores by
    that query's needles, plus OTHER_QUERY_BONUS for each other query with a
    hit inside it, so passages where several concerns meet come first.
    For a single query that is simply best first.

    Passages are returned in the order taken (best first, round by round).
    Each costs its length plus ``per_passage_overhead`` (the caller's label
    and separators) against ``budget_chars``; one that no longer fits is
    passed over for a smaller one.
    """
    needle_sets = [query_needles(query) for query in queries]
    # Every query's hit starts, per text, for the other-query bonus.
    query_starts: List[List[List[int]]] = []
    per_query: List[List[Tuple[float, int, int, int, int]]] = []
    hits_by_query: List[List[List[List[Tuple[int, int]]]]] = []
    for needles in needle_sets:
        hits_per_text = [[needle_hits(text, n)[1] for n in needles] for text in texts]
        hits_by_query.append(hits_per_text)
        query_starts.append(
            [
                sorted(start for spans in hits for start, _ in spans)
                for hits in hits_per_text
            ]
        )
    for qi, needles in enumerate(needle_sets):
        candidates: List[Tuple[float, int, int, int, int]] = []
        for ti, text in enumerate(texts):
            for score, start, end, hit in _candidate_passages(
                text, needles, hits_by_query[qi][ti]
            ):
                others = sum(
                    1
                    for qj, starts in enumerate(query_starts)
                    if qj != qi
                    and bisect.bisect_right(starts[ti], end)
                    > bisect.bisect_left(starts[ti], start)
                )
                candidates.append(
                    (score + OTHER_QUERY_BONUS * others, ti, start, end, hit)
                )
        # Highest score first; among equals, earliest in the text.
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        per_query.append(candidates)

    picked: List[Passage] = []
    budget = budget_chars
    cursors = [0] * len(per_query)
    took_one = True
    while took_one and len(picked) < max_passages:
        took_one = False
        for qi, candidates in enumerate(per_query):
            if len(picked) >= max_passages:
                break
            while cursors[qi] < len(candidates):
                score, ti, start, end, hit = candidates[cursors[qi]]
                cursors[qi] += 1
                cost = end - start + per_passage_overhead
                if cost > budget or any(
                    p.source == ti and start < p.end and p.start < end for p in picked
                ):
                    continue
                picked.append(Passage(ti, start, end, hit, score))
                budget -= cost
                took_one = True
                break
    return picked


def merge_adjacent(passages: Sequence[Passage], max_gap: int) -> List[Passage]:
    """``passages`` in reading order, any two from the same text no more
    than ``max_gap`` characters apart joined into one.

    Passages never overlap, but two can meet end to end, and a rule that
    straddles them then reads as two fragments with a label between. Joined
    passages keep the first one's hit and the higher score.
    """
    merged: List[Passage] = []
    for passage in sorted(passages, key=lambda p: (p.source, p.start)):
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.source == passage.source
            and passage.start - last.end <= max_gap
        ):
            merged[-1] = Passage(
                last.source,
                last.start,
                max(last.end, passage.end),
                last.hit,
                max(last.score, passage.score),
            )
        else:
            merged.append(passage)
    return merged


def passage_text(text: str, passage: Passage) -> str:
    """The passage's text, with an ellipsis where it cuts into the text."""
    return (
        ("…" if passage.start > 0 else "")
        + text[passage.start : passage.end].strip()
        + ("…" if passage.end < len(text) else "")
    )


def line_number(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def match_counts(text: str, needles: Sequence[Needle]) -> Dict[str, int]:
    """How often each needle occurs in ``text``, by label."""
    return {needle.label: needle_hits(text, needle)[0] for needle in needles}

"""Text-similarity helpers for detecting (near-)repeated chat replies.

The chat loop failure mode this supports: the model re-sends its previous
reply (often verbatim, sometimes lightly reworded) instead of using the
user's newest answer, so the conversation stops advancing. Detection has to
be shared by two layers that cannot import each other's home modules --
``fighthealthinsurance.ml.ml_models`` (per-backend retry loop) and
``fighthealthinsurance.chat.llm_client`` (candidate scoring), where
``llm_client`` already imports from ``ml_models`` -- so the helpers live in
this tiny stdlib-only module both can import without a cycle.
"""

import re
from difflib import SequenceMatcher
from typing import Optional, Set

# A normalized-exact repeat of anything at least this long is a real repeat
# (below it, short confirmations like "Yes." can legitimately recur).
EXACT_REPEAT_MIN_CHARS = 20

# Similarity-based ("mostly repeated") detection only applies to texts at
# least this long -- short strings reach high ratios by accident.
NEAR_REPEAT_MIN_CHARS = 80

# SequenceMatcher ratio at/above which two long texts count as the same reply.
NEAR_REPEAT_SIMILARITY = 0.9

# For long texts, a near-total word-set overlap also counts as a repeat even
# when reordering keeps the sequence ratio lower.
LONG_TEXT_JACCARD_MIN_CHARS = 200
LONG_TEXT_JACCARD_THRESHOLD = 0.95

# Cap comparison cost on pathological outputs; the head of a reply is enough
# to recognize a repeat.
_MAX_COMPARE_CHARS = 4000

_WORD_RE = re.compile(r"[a-z0-9]+")

# Markers of replies the system prompt REQUIRES to be sent verbatim (e.g. the
# Medicaid work-requirements block must always be that exact text). Repeating
# them across turns is by design, so repeat-rejection layers exempt them --
# soft scoring penalties still prefer a fresh non-canned answer when one
# exists. Shared here (not in chat.llm_client) so the model layer's
# self-heal retry loop can use it without an import cycle.
CANNED_REPLY_MARKERS = ("Medicaid Work Requirements FAQ",)

# When the user explicitly asks for a repeat, repeating is the right answer --
# repeat-rejection layers skip themselves so the turn can succeed. NOTE:
# match this against the user's RAW message only; system-injected wrapper
# text (anti-repeat notes, bridge notes) legitimately contains the word
# "repeat" and would false-positive.
USER_REQUESTED_REPEAT_RE = re.compile(
    r"\b(repeat|say (?:that|it) again|one more time|once more|again please|"
    r"re-?send|(?:show|send) (?:that|it) again)\b",
    re.IGNORECASE,
)


def is_canned_reply(text: Optional[str]) -> bool:
    """Whether the reply is one of the mandated verbatim canned responses."""
    if not text:
        return False
    return any(marker in text for marker in CANNED_REPLY_MARKERS)


def user_requested_repeat(message: Optional[str]) -> bool:
    """True when the user's message explicitly asks us to repeat ourselves."""
    return bool(message and USER_REQUESTED_REPEAT_RE.search(message))


def normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip."""
    return re.sub(r"\s+", " ", text.lower().strip())


def bag_of_words(text: str) -> Set[str]:
    """Extract a bag of words (lowercased) from text for unordered comparison."""
    return set(_WORD_RE.findall(text.lower()))


def jaccard_similarity(a: str, b: str) -> float:
    """Word-set Jaccard similarity of two texts (0.0-1.0)."""
    words_a = bag_of_words(a)
    words_b = bag_of_words(b)
    if not words_a or not words_b:
        return 0.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union else 0.0


def sequence_similarity(a: str, b: str) -> float:
    """Order-sensitive similarity of two normalized texts (0.0-1.0).

    ``difflib.SequenceMatcher`` ratio on the normalized, length-capped
    strings, with the cheap ``quick_ratio`` upper bound short-circuiting
    clearly-different pairs (ratio can never exceed quick_ratio, and callers
    only care about values near the repeat thresholds).
    """
    na = normalize_text(a)[:_MAX_COMPARE_CHARS]
    nb = normalize_text(b)[:_MAX_COMPARE_CHARS]
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    matcher = SequenceMatcher(None, na, nb)
    quick = matcher.quick_ratio()
    if quick < 0.6:
        return quick
    return matcher.ratio()


def response_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Overall similarity of two replies (0.0-1.0): max of the order-sensitive
    sequence ratio and the word-set Jaccard, so both verbatim repeats and
    same-content reorderings score high."""
    if not a or not b:
        return 0.0
    return max(sequence_similarity(a, b), jaccard_similarity(a, b))


def is_mostly_repeated(
    candidate: Optional[str],
    previous: Optional[str],
    *,
    similarity_threshold: float = NEAR_REPEAT_SIMILARITY,
    min_chars: int = NEAR_REPEAT_MIN_CHARS,
) -> bool:
    """Whether ``candidate`` is (nearly) the same reply as ``previous``.

    True when the normalized texts match exactly (and are long enough to not
    be a routine short confirmation), when long texts are near-identical by
    sequence ratio, or when long texts share almost their whole word set.
    Short texts never trip the similarity paths, so brief answers ("Yes.",
    "Which state?") are left to softer scoring penalties instead.
    """
    if not candidate or not previous:
        return False
    norm_candidate = normalize_text(candidate)
    norm_previous = normalize_text(previous)
    if not norm_candidate or not norm_previous:
        return False
    if norm_candidate == norm_previous:
        return len(norm_candidate) >= EXACT_REPEAT_MIN_CHARS
    if len(norm_candidate) < min_chars or len(norm_previous) < min_chars:
        return False
    if sequence_similarity(norm_candidate, norm_previous) >= similarity_threshold:
        return True
    if (
        len(norm_candidate) >= LONG_TEXT_JACCARD_MIN_CHARS
        and len(norm_previous) >= LONG_TEXT_JACCARD_MIN_CHARS
        and jaccard_similarity(norm_candidate, norm_previous)
        >= LONG_TEXT_JACCARD_THRESHOLD
    ):
        return True
    return False

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
# to recognize a repeat. Kept low deliberately: ratio() is O(n*m) and runs on
# the event loop inside the fan-out scorer, so the cap is the hard ceiling on
# per-comparison cost (~30ms at 1200 chars vs ~240ms at 4000).
_MAX_COMPARE_CHARS = 1200

# Word-set overlap below which two texts CANNOT be a near-verbatim repeat, so
# the expensive sequence comparison is skipped. A 0.9 character-sequence
# ratio implies almost the same words in almost the same order; unrelated
# English replies share only stopwords (Jaccard well under 0.3). Deliberately
# loose -- this is a cost gate, not a detector.
_SEQUENCE_JACCARD_FLOOR = 0.35

_WORD_RE = re.compile(r"[a-z0-9]+")

# Replies the system prompt REQUIRES to be sent verbatim (e.g. the Medicaid
# work-requirements block must always be that exact text). Repeating them
# across turns is by design, so repeat-rejection layers exempt them -- soft
# scoring penalties still prefer a fresh non-canned answer when one exists.
# Shared here (not in chat.llm_client) so the model layer's self-heal retry
# loop can use it without an import cycle.
#
# Each entry is the set of phrases a reply must contain to BE that canned
# block. A single marker is not enough: the system prompt tells the model to
# link the Medicaid FAQ whenever work requirements come up, so matching on
# the link alone exempted every ordinary Medicaid reply -- including the
# looping ones this module exists to catch -- from the whole ladder.
CANNED_REPLY_SIGNATURES = (
    (
        "Medicaid Work Requirements FAQ",
        "80 hours per month",
        "December 31, 2026",
    ),
)

# When the user explicitly asks for a repeat, repeating is the right answer --
# repeat-rejection layers skip themselves so the turn can succeed.
#
# NOTE 1: match this against the user's RAW message only; system-injected
# wrapper text (anti-repeat notes, bridge notes) legitimately contains the
# word "repeat" and would false-positive.
#
# NOTE 2: a bare \brepeat\b is NOT enough. "repeat MRI", "repeat colonoscopy",
# "repeat prescription" and "repeat denial" are everyday vocabulary in this
# product, and this flag is the master switch that disables every rung of the
# ladder -- so it must match an explicit REQUEST to say something again, not
# the topic of the conversation. Erring toward not-matching is the safe
# direction: it keeps loop protection on.
_REPEAT_VERB = r"(?:repeat|say|send|resend|re-send|show|write|state|tell)"
USER_REQUESTED_REPEAT_RE = re.compile(
    r"(?:"
    # "repeat that", "repeat your last reply", "repeat the above"
    r"\brepeat\s+(?:that|it|this|those|your|the|last|previous|above)\b"
    # "say that again", "send it again", "show me that one more time"
    rf"|\b{_REPEAT_VERB}\b[^.?!\n]{{0,40}}?\b(?:again|one more time|once more)\b"
    # "can you repeat", "could you please repeat"
    r"|\b(?:can|could|would|will|please|plz)\s+(?:you\s+)?(?:please\s+)?repeat\b"
    # "resend it", "re-send that" -- but NOT "resend the fax to my doctor",
    # which asks us to send a document, not to say something again.
    r"|\bre-?send\s+(?:that|it|this)\b"
    # A message that is JUST the request. Anchored to the whole message
    # because mid-sentence these are ordinary phrases ("I filed an appeal
    # one more time last month").
    r"|^\s*repeat(?:\s+please)?\s*[.?!]*\s*$"
    r"|^\s*(?:one more time|once more)(?:\s+please)?\s*[.?!]*\s*$"
    r")",
    re.IGNORECASE,
)


def is_canned_reply(text: Optional[str]) -> bool:
    """Whether the reply IS one of the mandated verbatim canned responses.

    Requires every phrase of a signature, so a normal reply that merely
    links or mentions the canned block is not exempted from repeat checks.
    """
    if not text:
        return False
    return any(
        all(phrase in text for phrase in signature)
        for signature in CANNED_REPLY_SIGNATURES
    )


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


def _sequence_similarity_normalized(na: str, nb: str, min_useful: float = 0.0) -> float:
    """``sequence_similarity`` for already-normalized, already-capped text.

    With ``min_useful > 0`` the caller declares it only cares whether the
    result reaches that threshold, which unlocks the cheap gates below; the
    return is then either the exact ratio or a value guaranteed to be under
    ``min_useful``. With the default 0.0 the exact ratio is always computed
    -- callers comparing against a LOWER threshold (the alternate-answer
    check uses 0.7) would otherwise read an upper bound as a real score and
    reject a perfectly distinct answer.

    ``autojunk=False`` is REQUIRED for correctness here, not a tuning knob.
    difflib's default autojunk heuristic treats any element occurring in more
    than 1% of a sequence of 200+ elements as "popular junk" and refuses to
    match on it -- on character-level natural language that is every space and
    every common letter, so ratio() collapses. Measured on two 430-char
    assistant replies differing by one reworded sentence: 0.33 with autojunk
    vs 0.96 without, against a 0.9 threshold. Every long near-verbatim repeat
    -- exactly what this module exists to catch -- scored as unrelated.

    Turning autojunk off makes ratio() genuinely O(n*m), so two cheap O(n)
    gates run first. The LENGTH gate is an exact upper bound on ratio(), so
    skipping on it can never change a verdict. The word-set gate is a
    HEURISTIC, not a bound -- it only applies when the caller's threshold
    sits above the floor, so the surrogate value it returns is still
    guaranteed to fall below that threshold.
    """
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if min_useful > 0.0:
        # Length bound: ratio() can never exceed 2*min/(len_a+len_b).
        length_bound = 2 * min(len(na), len(nb)) / (len(na) + len(nb))
        if length_bound < min_useful:
            return length_bound
        # Word-set gate (heuristic): a near-verbatim repeat shares nearly all
        # its words. Only usable when the caller's threshold is above the
        # floor -- otherwise the surrogate returned here could itself reach
        # the threshold and report a repeat that was never measured.
        if (
            min_useful > _SEQUENCE_JACCARD_FLOOR
            and jaccard_similarity(na, nb) < _SEQUENCE_JACCARD_FLOOR
        ):
            return min(length_bound, _SEQUENCE_JACCARD_FLOOR)
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def sequence_similarity(a: str, b: str) -> float:
    """Order-sensitive similarity of two texts (0.0-1.0).

    Normalizes and length-caps both sides, then compares (see
    :func:`_sequence_similarity_normalized` for why ``autojunk=False`` is
    load-bearing and how the cost is bounded).
    """
    return _sequence_similarity_normalized(
        normalize_text(a)[:_MAX_COMPARE_CHARS],
        normalize_text(b)[:_MAX_COMPARE_CHARS],
    )


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
    # Already normalized above -- compare without re-normalizing (the public
    # sequence_similarity would redo the lowercase + whitespace collapse).
    capped_candidate = norm_candidate[:_MAX_COMPARE_CHARS]
    capped_previous = norm_previous[:_MAX_COMPARE_CHARS]
    # This is the hot path (several comparisons per fan-out candidate, on the
    # event loop), and it only asks "does it reach the threshold?" -- so the
    # cheap upper-bound gates apply here.
    if (
        _sequence_similarity_normalized(
            capped_candidate, capped_previous, min_useful=similarity_threshold
        )
        >= similarity_threshold
    ):
        return True
    if (
        len(norm_candidate) >= LONG_TEXT_JACCARD_MIN_CHARS
        and len(norm_previous) >= LONG_TEXT_JACCARD_MIN_CHARS
        and jaccard_similarity(norm_candidate, norm_previous)
        >= LONG_TEXT_JACCARD_THRESHOLD
    ):
        return True
    return False

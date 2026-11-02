"""Improve Arena review insights: the raw interaction details a vote carries.

The Arena page sends a small summary of how a review went (how the judge
moved through the drafts, how they cast the vote) with each vote, and the
vote keeps it in ``ChooserVote.review_signals`` exactly as cleaned here:
raw counts and flags, no scores or verdicts, so later analysis can weigh
them however it likes.

Cleaning never refuses a vote. A known key with a value of the wrong shape
is left out and its name is listed under ``dropped``; unknown keys are
ignored. Counts are clamped to ``MAX_COUNT`` and position lists to the
number of drafts a task can hold.
"""

from typing import Any, Callable, Dict, List, Optional

# Large enough for any real review; a page left open with a busy mouse
# reaches it, and is recorded as reaching it.
MAX_COUNT = 1_000_000
# Draft positions on the page (0 for Draft A); the page labels up to F.
MAX_POSITIONS = 16

POINTER_TYPES = ("mouse", "touch", "pen")
VOTE_INPUTS = ("keyboard", "pointer")

# Whole-number counters the page keeps for each case.
COUNT_KEYS = (
    # Visible time (see ChooserVote.active_ms) until the first click, tap,
    # key or scroll on the case.
    "first_interaction_ms",
    "pointer_moves",
    "pointer_downs",
    "key_presses",
    "scroll_events",
    # Picks made and unmade before the vote.
    "selection_changes",
    # Times the page went to the background while the case was open.
    "hidden_count",
    # Rounds already voted in this page load, before this one.
    "round_in_page",
    "viewport_width",
)
# Draft positions, in display order.
POSITION_KEYS = (
    # Drafts too long for their box, for which "Read it all" or a scroll
    # was needed to see the end.
    "overflowing_positions",
    # Drafts scrolled to the end inside their box.
    "scrolled_to_end_positions",
)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN, negative
        return None
    return int(min(value, MAX_COUNT))


def _positions(value: Any) -> Optional[List[int]]:
    if not isinstance(value, list) or len(value) > MAX_POSITIONS:
        return None
    out: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if not 0 <= item < MAX_POSITIONS:
            return None
        if item not in out:
            out.append(item)
    return sorted(out)


def _choice(options: tuple) -> Callable[[Any], Optional[str]]:
    def clean(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value in options else None

    return clean


def _pointer_types(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list) or len(value) > len(POINTER_TYPES):
        return None
    if any(not isinstance(v, str) or v not in POINTER_TYPES for v in value):
        return None
    return sorted(set(value))


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


_CLEANERS: Dict[str, Callable[[Any], Any]] = {
    **{key: _count for key in COUNT_KEYS},
    **{key: _positions for key in POSITION_KEYS},
    "pointer_types": _pointer_types,
    "vote_input": _choice(VOTE_INPUTS),
    "webdriver": _flag,
}


def clean_review_signals(raw: Any) -> Dict[str, Any]:
    """The known, well-formed entries of ``raw``; malformed ones are named
    under ``dropped``. Anything that is not a dict cleans to ``{}``."""
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, cleaner in _CLEANERS.items():
        if key not in raw or raw[key] is None:
            continue
        value = cleaner(raw[key])
        if value is None:
            dropped.append(key)
        else:
            cleaned[key] = value
    if dropped:
        cleaned["dropped"] = dropped
    return cleaned

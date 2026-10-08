"""Versioned appeal-letter prompts and the staff setting that picks one.

v1 is the appeal prompt as it has always been. v2 is v1 with the output
contract from the September 2026 model evaluation appended: two sentences
that told models to return only the letter, with no markdown, headings,
commentary or notes. In that evaluation they cut output tokens and response
time by about 40% for the Gemma models and raised their scores, almost all of
it on tone and form. The text here is the evaluation's, word for word; change
it only as a new version, or the comparison stops measuring what was tested.

v3 is a different build of the prompt, not v1 with something added: the same
inputs laid out as labelled sections (task, point of view, details, plan,
evidence, the denial letter last), without the example openings, and with the
same output contract at the end, so v3 against v2 compares the two layouts.
AppealGenerator.make_sectioned_open_prompt builds it; v1 and v2 are built by
make_open_prompt.

Which version a letter gets is a staff setting (LetterPromptMode rows,
changed on the Model Usage dashboard): original (v1 for every letter), new
(v2 for every letter), split (each full-letter call draws v1 or v2 at
random, half and half), sectioned (v3 for every letter) or thirds (each
full-letter call draws v1, v2 or v3 at random, a third each). Every draft
records the version that wrote it, so the staff page can compare how often
people pick each version's letters.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from loguru import logger

PROMPT_V1 = "v1"
PROMPT_V2 = "v2"
PROMPT_V3 = "v3"

PROMPT_VERSION_CHOICES = (
    (PROMPT_V1, "v1: original prompt"),
    (PROMPT_V2, "v2: original prompt plus the output contract"),
    (PROMPT_V3, "v3: sectioned prompt plus the output contract"),
)

# The versions whose prompt ends with OUTPUT_CONTRACT.
_CONTRACT_VERSIONS = frozenset({PROMPT_V2, PROMPT_V3})

# The September 2026 evaluation's output contract (eval/RUNS.md, "The
# tightened output contract"), less its opening "You write health insurance
# appeal letters.", which the app's own prompts already say at length.
OUTPUT_CONTRACT = (
    "Output ONLY the letter itself: no markdown formatting or headings, no "
    "commentary before or after, no notes to the user. Plain prose, and end "
    "immediately after the signature block."
)

MODE_ORIGINAL = "original"
MODE_NEW = "new"
MODE_SPLIT = "split"
MODE_SECTIONED = "sectioned"
MODE_THIRDS = "thirds"

# Stored on LetterPromptMode rows, so a value keeps its meaning for good: add
# modes, never repurpose one.
MODE_CHOICES = (
    (MODE_ORIGINAL, "Original prompt (v1) for every letter"),
    (MODE_NEW, "New prompt (v2) for every letter"),
    (MODE_SPLIT, "Half and half: each draft is written with v1 or v2 at random"),
    (MODE_SECTIONED, "Sectioned prompt (v3) for every letter"),
    (MODE_THIRDS, "Thirds: each draft is written with v1, v2 or v3 at random"),
)

_MODES = frozenset(m for m, _label in MODE_CHOICES)

# The versions each mode writes letters with.
MODE_VERSIONS = {
    MODE_ORIGINAL: (PROMPT_V1,),
    MODE_NEW: (PROMPT_V2,),
    MODE_SPLIT: (PROMPT_V1, PROMPT_V2),
    MODE_SECTIONED: (PROMPT_V3,),
    MODE_THIRDS: (PROMPT_V1, PROMPT_V2, PROMPT_V3),
}

# The modes that draw a version at random for each call: the runs the staff
# page compares versions head to head over.
RANDOM_MODES = frozenset(
    m for m, versions in MODE_VERSIONS.items() if len(versions) > 1
)

# How long a process keeps the mode it read. A staff change reaches every
# pod within this long; each appeal run reads the mode once.
MODE_CACHE_SECONDS = 30.0


def apply_prompt_version(prompt: str, version: Optional[str]) -> str:
    """The prompt a call sends under ``version``, given the open prompt that
    version is built with (plus any hint block).

    v2 and v3 append the contract after everything else, so it is the last
    thing the model reads, and so context shedding (which swaps out the start
    of a prompt and keeps what follows it) keeps it too. Any other version
    leaves the prompt as it is.
    """
    if version in _CONTRACT_VERSIONS:
        return f"{prompt}\n\n{OUTPUT_CONTRACT}"
    return prompt


def uses_sectioned_prompt(version: Optional[str]) -> bool:
    """Whether ``version`` is built with the sectioned layout
    (make_sectioned_open_prompt) rather than make_open_prompt."""
    return version == PROMPT_V3


# The random draw for split and thirds. Its own generator, not the
# module-level random one that make_open_prompt shuffles its examples with,
# and a module attribute so tests can force any side.
_split_draw: Callable[[], float] = random.SystemRandom().random


def choose_prompt_version(mode: str) -> str:
    """The version one full-letter call is written with under ``mode``."""
    if mode == MODE_NEW:
        return PROMPT_V2
    if mode == MODE_SECTIONED:
        return PROMPT_V3
    if mode == MODE_SPLIT:
        return PROMPT_V2 if _split_draw() < 0.5 else PROMPT_V1
    if mode == MODE_THIRDS:
        draw = _split_draw()
        if draw < 1 / 3:
            return PROMPT_V1
        if draw < 2 / 3:
            return PROMPT_V2
        return PROMPT_V3
    return PROMPT_V1


class _ModeCache:
    """The last mode read, refreshed by one caller at a time.

    The lock only guards the cached value; it is never held during the
    database read. While one caller refreshes, every other caller gets the
    last value read (original before the first read), so a slow read delays
    one letter at most, never all of them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[str] = None
        self._read_at = 0.0
        self._refreshing = False

    def get(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now - self._read_at < MODE_CACHE_SECONDS:
                return self._value
            if self._refreshing:
                return self._value or MODE_ORIGINAL
            self._refreshing = True
        value = MODE_ORIGINAL
        try:
            value = _read_mode()
        finally:
            with self._lock:
                self._value = value
                self._read_at = time.monotonic()
                self._refreshing = False
        return value

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._read_at = 0.0
            self._refreshing = False


# How long the setting's query may run, waiting on a lock included, before
# the read gives up and the letter uses the original prompt.
MODE_READ_TIMEOUT_MS = 500


def _read_mode() -> str:
    """The newest LetterPromptMode row's mode, or original.

    Anything unexpected (no rows, a database error, a read slower than
    MODE_READ_TIMEOUT_MS, an unknown value) is original: a letter must never
    fail or wait because the setting could not be read.
    """
    try:
        from django.db import connection, transaction

        from fighthealthinsurance.ml.chat_policy import _bound_statements
        from fighthealthinsurance.models import LetterPromptMode

        newest = LetterPromptMode.objects.order_by("-created_at", "-id").values_list(
            "mode", flat=True
        )
        if connection.in_atomic_block:
            # The timeout is SET LOCAL, so inside a caller's transaction it
            # would outlive this read and cut short the caller's own
            # statements. Read without it there.
            mode = newest.first()
        else:
            with transaction.atomic():
                _bound_statements(connection, MODE_READ_TIMEOUT_MS)
                mode = newest.first()
    except Exception as e:
        logger.warning(f"Letter prompt mode unreadable, using original: {e}")
        return MODE_ORIGINAL
    if mode is None:
        return MODE_ORIGINAL
    if mode not in _MODES:
        logger.warning(f"Unknown letter prompt mode {mode!r}, using original")
        return MODE_ORIGINAL
    return str(mode)


_mode_cache = _ModeCache()


def current_letter_prompt_mode() -> str:
    """The staff-chosen mode, read at most every MODE_CACHE_SECONDS.

    Synchronous: it may query the database, so call it from a worker thread
    (make_appeals runs on one), not from the event loop.
    """
    return _mode_cache.get()


def reset_letter_prompt_mode_cache() -> None:
    """Forget the cached mode, so the next read goes to the database."""
    _mode_cache.reset()

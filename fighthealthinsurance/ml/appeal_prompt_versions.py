"""Versioned appeal-letter prompts and the staff setting that picks one.

v1 is the appeal prompt as it has always been. v2 is v1 with the output
contract from the September 2026 model evaluation appended: two sentences
that told models to return only the letter, with no markdown, headings,
commentary or notes. In that evaluation they cut output tokens and response
time by about 40% for the Gemma models and raised their scores, almost all of
it on tone and form. The text here is the evaluation's, word for word; change
it only as a new version, or the comparison stops measuring what was tested.

Which version a letter gets is a staff setting (LetterPromptMode rows,
changed on /timbit/help/letter_prompts): original (v1 for every letter), new
(v2 for every letter) or split (each full-letter call draws v1 or v2 at
random, half and half). Every draft records the version that wrote it, so
the staff page can compare how often people pick each version's letters.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from loguru import logger

PROMPT_V1 = "v1"
PROMPT_V2 = "v2"

PROMPT_VERSION_CHOICES = (
    (PROMPT_V1, "v1: original prompt"),
    (PROMPT_V2, "v2: original prompt plus the output contract"),
)

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

MODE_CHOICES = (
    (MODE_ORIGINAL, "Original prompt for every letter"),
    (MODE_NEW, "New prompt for every letter"),
    (MODE_SPLIT, "Half and half: each draft is written with one at random"),
)

_MODES = frozenset(m for m, _label in MODE_CHOICES)

# How long a process keeps the mode it read. A staff change reaches every
# pod within this long; each appeal run reads the mode once.
MODE_CACHE_SECONDS = 30.0


def apply_prompt_version(prompt: str, version: Optional[str]) -> str:
    """The prompt a call sends under ``version``.

    v2 appends the contract after everything else, so it is the last thing
    the model reads, and so context shedding (which swaps out the start of a
    prompt and keeps what follows it) keeps it too. Any other version leaves
    the prompt as it is.
    """
    if version == PROMPT_V2:
        return f"{prompt}\n\n{OUTPUT_CONTRACT}"
    return prompt


# The 50/50 draw. Its own generator, not the module-level random one that
# make_open_prompt shuffles its examples with, and a module attribute so
# tests can force either side.
_split_draw: Callable[[], float] = random.SystemRandom().random


def choose_prompt_version(mode: str) -> str:
    """The version one full-letter call is written with under ``mode``."""
    if mode == MODE_NEW:
        return PROMPT_V2
    if mode == MODE_SPLIT:
        return PROMPT_V2 if _split_draw() < 0.5 else PROMPT_V1
    return PROMPT_V1


class _ModeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[str] = None
        self._read_at = 0.0

    def get(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now - self._read_at < MODE_CACHE_SECONDS:
                return self._value
            self._value = _read_mode()
            self._read_at = now
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._read_at = 0.0


def _read_mode() -> str:
    """The newest LetterPromptMode row's mode, or original.

    Anything unexpected (no rows, a database error, an unknown value) is
    original: a letter must never fail to be written because the setting
    could not be read.
    """
    try:
        from fighthealthinsurance.models import LetterPromptMode

        mode = (
            LetterPromptMode.objects.order_by("-created_at", "-id")
            .values_list("mode", flat=True)
            .first()
        )
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

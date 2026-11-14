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

v4 and v5 are v2 and v3 under the evaluation's own system prompt: the user
turns are v2's and v3's, each ending with the output contract, and the
system prompt is the evaluation's (TIGHT_SYSTEM_PROMPT) in place of the
app's persona prompt, followed only by what the app depends on: the voice,
the placeholders filled in after the letter is written, and the recipient
(ml_models.TIGHT_PATIENT_LETTER_PROMPT and TIGHT_PROFESSIONAL_LETTER_PROMPT).
v1 to v3 keep the persona prompt. The system prompt is chosen per call from
the version (RemoteOpenLike.get_system_prompts).

Which version a letter gets is a staff setting (LetterPromptMode rows,
changed on the Model Usage dashboard): original (v1 for every letter), new
(v2 for every letter), split (each full-letter call draws v1 or v2 at
random, half and half), sectioned (v3 for every letter), thirds (each
full-letter call draws v1, v2 or v3 at random, a third each) or v2, v4 or
v5 (each full-letter call draws v2, v4 or v5 at random, a third each, to
compare v2 with the same turn under the tight system prompt, and the two
layouts under it; v1 and v3 are never drawn). With no row, or when the
row cannot be read, letters use original; so does a pod running code from
before a mode existed, which reads that mode as unknown. Every draft
records the version that wrote it, so the staff page can compare how often
people pick each version's letters.

v6 is the shared appeal context (ml/appeal_context): the same inputs as v3,
split into the cache-friendly layout every stage shares -- one fixed system
prompt (SHARED_APPEAL_INSTRUCTIONS), then the denial letter, the patient
history, the gathered research and the intake answers as fixed sections,
then the letter task (point of view, details, plan and payer context, the
output contract) last. AppealGenerator.make_shared_context_prompt builds it.
Two modes run it: shared_context (v6 for every letter, and every other stage
-- entity extraction, follow-up questions, citations -- sends the same
prefix, so a provider can reuse the denial across all of them) and
split_v2_v6 (each letter draws v2 or v6, half and half; the other stages
keep their own prompts, so the letter comparison is the only change).
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
PROMPT_V4 = "v4"
PROMPT_V5 = "v5"
PROMPT_V6 = "v6"

PROMPT_VERSION_CHOICES = (
    (PROMPT_V1, "v1: original prompt"),
    (PROMPT_V2, "v2: original prompt plus the output contract"),
    (PROMPT_V3, "v3: sectioned prompt plus the output contract"),
    (PROMPT_V4, "v4: v2 under the evaluation's tight system prompt"),
    (PROMPT_V5, "v5: v3 under the evaluation's tight system prompt"),
    (PROMPT_V6, "v6: the shared appeal context (cache-friendly sections)"),
)

# The versions whose prompt ends with OUTPUT_CONTRACT.
_CONTRACT_VERSIONS = frozenset({PROMPT_V2, PROMPT_V3, PROMPT_V4, PROMPT_V5, PROMPT_V6})

# The September 2026 evaluation's output contract (eval/RUNS.md, "The
# tightened output contract"), less its opening "You write health insurance
# appeal letters.", which the app's own prompts already say at length.
OUTPUT_CONTRACT = (
    "Output ONLY the letter itself: no markdown formatting or headings, no "
    "commentary before or after, no notes to the user. Plain prose, and end "
    "immediately after the signature block."
)

# The evaluation's whole system prompt, word for word: the one every tight
# arm was written with. v4's and v5's system prompts start with it.
TIGHT_SYSTEM_PROMPT = f"You write health insurance appeal letters. {OUTPUT_CONTRACT}"

MODE_ORIGINAL = "original"
MODE_NEW = "new"
MODE_SPLIT = "split"
MODE_SECTIONED = "sectioned"
MODE_THIRDS = "thirds"
MODE_THIRDS_V2_V4_V5 = "thirds_v2_v4_v5"
MODE_SHARED_CONTEXT = "shared_context"
MODE_SPLIT_V2_V6 = "split_v2_v6"

# Stored on LetterPromptMode rows, so a value keeps its meaning for good: add
# modes, never repurpose one.
MODE_CHOICES = (
    (MODE_ORIGINAL, "Original prompt (v1) for every letter"),
    (MODE_NEW, "New prompt (v2) for every letter"),
    (MODE_SPLIT, "Half and half: each draft is written with v1 or v2 at random"),
    (MODE_SECTIONED, "Sectioned prompt (v3) for every letter"),
    (MODE_THIRDS, "Thirds: each draft is written with v1, v2 or v3 at random"),
    (
        MODE_THIRDS_V2_V4_V5,
        "v2, v4 or v5: each draft is written with v2, v4 or v5 at random",
    ),
    (
        MODE_SHARED_CONTEXT,
        "Shared appeal context (v6) for every letter and every other stage",
    ),
    (
        MODE_SPLIT_V2_V6,
        "Half and half: each draft is written with v2 or v6 at random",
    ),
)

_MODES = frozenset(m for m, _label in MODE_CHOICES)

# The versions each mode writes letters with.
MODE_VERSIONS = {
    MODE_ORIGINAL: (PROMPT_V1,),
    MODE_NEW: (PROMPT_V2,),
    MODE_SPLIT: (PROMPT_V1, PROMPT_V2),
    MODE_SECTIONED: (PROMPT_V3,),
    MODE_THIRDS: (PROMPT_V1, PROMPT_V2, PROMPT_V3),
    MODE_THIRDS_V2_V4_V5: (PROMPT_V2, PROMPT_V4, PROMPT_V5),
    MODE_SHARED_CONTEXT: (PROMPT_V6,),
    MODE_SPLIT_V2_V6: (PROMPT_V2, PROMPT_V6),
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

    v2 to v5 append the contract after everything else, so it is the last
    thing the model reads, and so context shedding (which swaps out the start
    of a prompt and keeps what follows it) keeps it too. v6's prompt is an
    AppealPrompt: the contract goes at the end of its task, which is still
    the last thing the model reads and leaves the shared prefix alone. Any
    other version leaves the prompt as it is.
    """
    from fighthealthinsurance.ml.appeal_context import AppealPrompt

    if version in _CONTRACT_VERSIONS and isinstance(prompt, AppealPrompt):
        return prompt.with_task_suffix(OUTPUT_CONTRACT)
    if version in _CONTRACT_VERSIONS:
        return f"{prompt}\n\n{OUTPUT_CONTRACT}"
    return prompt


def uses_tight_system_prompt(version: Optional[str]) -> bool:
    """Whether ``version`` is written under the evaluation's tight system
    prompt rather than the app's persona prompt."""
    return version in (PROMPT_V4, PROMPT_V5)


def uses_shared_context_prompt(version: Optional[str]) -> bool:
    """Whether ``version`` is built in the shared appeal context layout
    (make_shared_context_prompt) under SHARED_APPEAL_INSTRUCTIONS."""
    return version == PROMPT_V6


def shared_context_stages(mode: str) -> bool:
    """Whether every stage, not just the letter, sends the shared appeal
    context under ``mode``."""
    return mode == MODE_SHARED_CONTEXT


def uses_sectioned_prompt(version: Optional[str]) -> bool:
    """Whether ``version`` is built with the sectioned layout
    (make_sectioned_open_prompt) rather than make_open_prompt."""
    return version in (PROMPT_V3, PROMPT_V5)


# The random draw for the random-draw modes. Its own generator, not the
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
    if mode == MODE_SHARED_CONTEXT:
        return PROMPT_V6
    if mode == MODE_SPLIT_V2_V6:
        return PROMPT_V6 if _split_draw() < 0.5 else PROMPT_V2
    if mode == MODE_THIRDS_V2_V4_V5:
        draw = _split_draw()
        if draw < 1 / 3:
            return PROMPT_V2
        if draw < 2 / 3:
            return PROMPT_V4
        return PROMPT_V5
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

    def peek(self) -> Optional[str]:
        """The cached mode while it is fresh, else None; never reads."""
        with self._lock:
            if (
                self._value is not None
                and time.monotonic() - self._read_at < MODE_CACHE_SECONDS
            ):
                return self._value
        return None

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


async def acurrent_letter_prompt_mode() -> str:
    """current_letter_prompt_mode for async callers: a fresh cached value is
    returned on the event loop; a refresh runs on a worker thread (it reads
    the database), so the loop never blocks on it. Like the sync read,
    anything unexpected is original: the bridge itself manages DB
    connections outside _read_mode's guard, and a model call must never
    fail because the setting could not be read."""
    fresh = _mode_cache.peek()
    if fresh is not None:
        return fresh
    try:
        from channels.db import database_sync_to_async

        return str(await database_sync_to_async(current_letter_prompt_mode)())
    except Exception as e:
        logger.warning(f"Letter prompt mode unreadable, using original: {e}")
        return MODE_ORIGINAL


async def ashared_context_stages_enabled() -> bool:
    """Whether the non-letter stages send the shared appeal context now."""
    return shared_context_stages(await acurrent_letter_prompt_mode())


def reset_letter_prompt_mode_cache() -> None:
    """Forget the cached mode, so the next read goes to the database."""
    _mode_cache.reset()

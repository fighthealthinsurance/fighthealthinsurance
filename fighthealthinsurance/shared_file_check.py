"""The nightly check that every shared plan document still has its file.

Each shared file that should have a stored copy (shared from a file, not
removed) is looked for in the object store, the backend that outlasts the
pods (storage_health.bucket_backends): the store is asked for the copy's
size and its last few bytes (a ranged read, never the whole file), each
look bounded by SECONDS_PER_FILE, with no key: nothing is decrypted, no
text is looked at, and no row is changed. Each file is counted as one of:

- whole: a whole encrypted token, the size its recorded upload size gives;
- no size recorded: a whole token, from a file shared before sizes were
  recorded (stored_size);
- cut short: not a whole token's shape
  (ml_document_extraction.encrypted_copy_looks_whole);
- wrong size: whole in shape, but not the size its upload gives;
- missing: no object store holds it;
- unknown: no object store is in the write path, or one could not answer;
- not checked: past the check's bound of MAX_FILES files or MAX_SECONDS.

The counts are kept for the day in SharedPlanDocumentsDay, for staff, and
said in one log line. The hourly plan documents sweep runs the check once a
UTC day (run_file_check_if_due); check_shared_files runs it by hand.
"""

import datetime
import functools
import time
from typing import Optional

from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import PolicyDocument, SharedPlanDocumentsDay

#: The most files one check reads, and how long it may take: past either,
#: the rest are counted as not checked. The time ends inside the plan
#: documents worker's shutdown drain.
MAX_FILES = 1000
MAX_SECONDS = 180
#: The longest one file's look in the object store may take; a stalled one
#: is counted unknown and the check goes on.
SECONDS_PER_FILE = 20.0

Day = SharedPlanDocumentsDay


def _files_to_check():
    return (
        PolicyDocument.objects.filter(
            source__in=PolicyDocument.SHARED_FILE_SOURCES,
            document_enc__isnull=False,
        )
        .exclude(document_enc="")
        .exclude(processing_status=PolicyDocument.PROCESSING_REMOVED)
        .order_by("shared_at", "id")
        .values_list("pk", "document_enc", "stored_size")
    )


def _verdict(
    backends: list, name: str, stored_size: Optional[int], seconds: float
) -> str:
    from fighthealthinsurance.ml.ml_document_extraction import (
        encrypted_shape_is_whole,
        encrypted_size,
    )
    from fighthealthinsurance.storage_health import _probe, stored_length_and_tail

    unsure = False
    for backend in backends:
        try:
            found, _ = _probe(
                backend,
                seconds,
                functools.partial(stored_length_and_tail, backend, name),
            )
        except Exception as e:
            logger.warning(f"File check could not read a copy: {type(e).__name__}")
            unsure = True
            continue
        if found is None:
            continue
        length, tail = found
        if not encrypted_shape_is_whole(length, tail):
            return Day.FILE_CHECK_CUT_SHORT
        if stored_size is None:
            return Day.FILE_CHECK_NO_SIZE
        if length != encrypted_size(stored_size):
            return Day.FILE_CHECK_WRONG_SIZE
        return Day.FILE_CHECK_WHOLE
    return Day.FILE_CHECK_UNKNOWN if unsure else Day.FILE_CHECK_MISSING


def check_shared_files() -> dict[str, int]:
    """What the object store holds for each shared file, as counts by
    verdict (module docstring). Reads only; changes no row."""
    from fighthealthinsurance.storage_health import bucket_backends

    counts = {name: 0 for name in Day.FILE_CHECK_COUNTS}
    backends = bucket_backends()
    started = time.monotonic()
    checked: list = []
    for pk, name, stored_size in _files_to_check().iterator(chunk_size=200):
        left = MAX_SECONDS - (time.monotonic() - started)
        if len(checked) >= MAX_FILES or left <= 0:
            # The rest are counted once, not read row by row; a file removed
            # meanwhile is not among them.
            counts[Day.FILE_CHECK_NOT_CHECKED] = (
                _files_to_check().exclude(pk__in=checked).count()
            )
            break
        checked.append(pk)
        if not backends:
            counts[Day.FILE_CHECK_UNKNOWN] += 1
            continue
        seconds = min(SECONDS_PER_FILE, left)
        counts[_verdict(backends, name, stored_size, seconds)] += 1
    if not backends:
        logger.error("File check: no object store is in the write path")
    logger.info(
        "Shared plan document file check: "
        + ", ".join(f"{name[len('file_check_'):]} {n}" for name, n in counts.items())
    )
    return counts


def record(day: datetime.date, counts: dict[str, int]) -> None:
    """Keep ``counts`` as the day's file check counts."""
    for name, count in counts.items():
        Day.objects.update_or_create(day=day, name=name, defaults={"count": count})


def run_file_check_if_due(today: Optional[datetime.date] = None) -> bool:
    """Run today's check unless it ran already today, and keep its counts.
    True when it ran. The day is claimed first, so two sweeps at once run
    one check; a check that raises gives the claim back."""
    from fighthealthinsurance.shared_plan_documents import give_back, reserve

    today = today or timezone.now().date()
    claimed = reserve(Day.FILE_CHECK, 1, 1)
    if claimed is None:
        return False
    try:
        record(today, check_shared_files())
    except Exception as e:
        give_back(Day.FILE_CHECK, 1, claimed)
        logger.error(f"The shared plan document file check failed: {type(e).__name__}")
        return False
    return True

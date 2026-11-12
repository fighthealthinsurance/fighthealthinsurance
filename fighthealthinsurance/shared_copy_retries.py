"""Copies ticked for research whose save failed, made again later.

On Explain Policy and the appeal's plan documents step a person can tick a
box to share a copy of a file for research (shared_plan_documents
.share_opt_in_copies). When that copy's save fails (the bucket did not hold
it, storage or the database failed), it is not dropped: a SharedCopyRetry
row points at the page's own row for the file (the origin), and the hourly
plan documents sweep makes the copy again from there. The row holds no
file, name or text.

A retry goes through the same door as the first try: the closer look
(validate_shared_document), today's budgets, the appeal's repeat check by
fingerprint, then the usual save and reading. The copy keeps the sharing
terms' version the box was ticked under. The decrypted file lives only
inside the retry; logs carry reason codes and counts only.

Every retry ends within RETRY_LIFETIME:

- saved: the row goes;
- a full budget: tried again the next UTC day, no attempt spent;
- another failure: tried again RETRY_AFTER later, and after
  MAX_COPY_ATTEMPTS it is dropped (DROPPED_NOT_SAVED);
- the closer look refuses it: dropped (DROPPED_REFUSED);
- the origin has no file any more: dropped (DROPPED_ORIGIN_GONE);
- past RETRY_LIFETIME, checked again just before the save: dropped
  (DROPPED_NOT_SAVED).

A run starts no copy after RUN_SECONDS, so it ends inside the plan
documents worker's shutdown drain; the rest wait for the next run.

A removal by email deletes the rows (remove_shared_documents), and a
deleted origin takes its rows with it (cascade).
"""

import datetime
import os
import time
from typing import Any, Callable, Optional, Sequence

from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.db import transaction
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import (
    PlanDocuments,
    PolicyDocument,
    SharedCopyRetry,
    SharedPlanDocumentsDay,
)

#: How long a copy may wait at most, from its first failed save.
RETRY_LIFETIME = datetime.timedelta(days=7)
#: The wait after a failed retry, and how many failed tries end it.
RETRY_AFTER = datetime.timedelta(hours=6)
MAX_COPY_ATTEMPTS = 4
#: The most copies one sweep makes again, and how long into a run the last
#: may start.
MAX_RETRIES_PER_RUN = 10
RUN_SECONDS = 120

SAVED = "saved"
ALREADY_SHARED = "already_shared"
WAITING = "waiting"
DROPPED = "dropped"
GONE = "gone"


def origins_for(
    files: Sequence[UploadedFile], origin_of: Callable[[UploadedFile], Any]
) -> list[Any]:
    """Each file's own row on the page it came from, or None, found before
    the save is tried: a failed save can close the upload. Never raises."""
    found: list[Any] = []
    for uploaded in files:
        try:
            found.append(origin_of(uploaded))
        except Exception as e:
            logger.warning(f"Could not find a shared copy's origin: {type(e).__name__}")
            found.append(None)
    return found


def hold_for_retry(
    origins: Sequence[Any], *, source: str, hashed_email: Optional[str]
) -> int:
    """Hold a copy for each origin row to be made again later. Returns how
    many were held. Never raises: the page the copies came from has already
    done what it was for."""
    from fighthealthinsurance import consent

    held = 0
    for origin in origins:
        if origin is None:
            continue
        try:
            SharedCopyRetry.objects.create(
                source=source,
                origin_policy_document=(
                    origin if isinstance(origin, PolicyDocument) else None
                ),
                origin_plan_document=(
                    origin if isinstance(origin, PlanDocuments) else None
                ),
                hashed_email=hashed_email,
                consent_version=consent.SHARING_VERSION,
                next_try_at=timezone.now() + RETRY_AFTER,
            )
            held += 1
        except Exception as e:
            logger.warning(
                f"Could not hold a shared copy for later: {type(e).__name__}"
            )
    return held


def copies_waiting() -> int:
    """How many copies are waiting to be made again, for staff."""
    return SharedCopyRetry.objects.count()


def _drop(row: SharedCopyRetry, reason: str) -> str:
    from fighthealthinsurance.shared_plan_documents import note_dropped

    note_dropped(reason, 1)
    row.delete()
    logger.info(f"A shared copy waiting to be made again was dropped: {reason}")
    return DROPPED


def _failed(row: SharedCopyRetry, reason: str) -> str:
    row.attempts += 1
    row.last_reason = reason[:40]
    if row.attempts >= MAX_COPY_ATTEMPTS:
        return _drop(row, SharedPlanDocumentsDay.DROPPED_NOT_SAVED)
    row.next_try_at = timezone.now() + RETRY_AFTER
    row.save(update_fields=["attempts", "last_reason", "next_try_at"])
    return WAITING


def _origin_file(row: SharedCopyRetry) -> tuple[Any, Any]:
    """(the origin row, its stored file field), or (None, None) when the
    origin has no file to copy any more, or staff marked it potential spam."""
    origin: Any = row.origin_policy_document or row.origin_plan_document
    if origin is None:
        return None, None
    if isinstance(origin, PolicyDocument):
        if origin.processing_status in (
            PolicyDocument.PROCESSING_REMOVED,
            PolicyDocument.PROCESSING_SPAM,
        ):
            return None, None
        field = origin.document_enc
    else:
        field = origin.plan_document_enc
    return (origin, field) if field else (None, None)


def _details(origin: Any) -> tuple[dict[str, Any], str]:
    """The plan facts and document type the first try was given."""
    from fighthealthinsurance.shared_plan_documents import details_from_denial

    if isinstance(origin, PolicyDocument):
        return {"plan_category": origin.plan_category}, origin.document_type
    return details_from_denial(origin.denial), "other"


def _marked_spam(digest: str) -> bool:
    # A copy of this file staff marked as potential spam is never made again.
    return PolicyDocument.objects.filter(
        source__in=PolicyDocument.SHARED_FILE_SOURCES,
        fingerprint=digest,
        processing_status=PolicyDocument.PROCESSING_SPAM,
    ).exists()


def _retry_one(row: SharedCopyRetry) -> str:
    from django.core.exceptions import ValidationError

    from fighthealthinsurance.chat_forms import validate_shared_document
    from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file
    from fighthealthinsurance.shared_plan_documents import (
        OverDailyBudget,
        _shared_from_appeal,
        fingerprint,
        save_shared_documents,
    )
    from fighthealthinsurance.upload_limits import MAX_STORED_POLICY_DOCUMENT_BYTES

    origin, field = _origin_file(row)
    if origin is None:
        return _drop(row, SharedPlanDocumentsDay.DROPPED_ORIGIN_GONE)
    content = read_and_decrypt_file(field, max_bytes=MAX_STORED_POLICY_DOCUMENT_BYTES)
    if not content:
        return _failed(row, "file_not_read")
    uploaded = SimpleUploadedFile(os.path.basename(field.name), content)
    del content
    try:
        validate_shared_document(uploaded)
    except ValidationError:
        return _drop(row, SharedPlanDocumentsDay.DROPPED_REFUSED)
    digest = fingerprint(uploaded)
    if _marked_spam(digest):
        return _drop(row, SharedPlanDocumentsDay.DROPPED_REFUSED)
    if (
        row.source == PolicyDocument.SOURCE_SHARED_APPEAL
        and digest in _shared_from_appeal(row.hashed_email)
    ):
        row.delete()
        return ALREADY_SHARED
    details, document_type = _details(origin)
    saved: list[PolicyDocument] = []
    try:
        # The last look and the save hold the row: a removal or the origin's
        # deletion waits for them, then removes the copy too; and the copy
        # and the row's end commit together, so a copy is never made twice.
        with transaction.atomic():
            held = SharedCopyRetry.objects.select_for_update().filter(pk=row.pk).first()
            if held is None:
                return GONE
            if _origin_file(held)[0] is None:
                return _drop(held, SharedPlanDocumentsDay.DROPPED_ORIGIN_GONE)
            if held.created_at <= timezone.now() - RETRY_LIFETIME:
                return _drop(held, SharedPlanDocumentsDay.DROPPED_NOT_SAVED)
            saved = save_shared_documents(
                [uploaded],
                source=held.source,
                details=details,
                hashed_email=held.hashed_email,
                document_type=document_type,
                consent_version=held.consent_version,
            )
            held.delete()
    except OverDailyBudget:
        # A full day waits for the next one; it spends no attempt.
        tomorrow = (timezone.now() + datetime.timedelta(days=1)).date()
        row.next_try_at = datetime.datetime.combine(
            tomorrow, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        row.save(update_fields=["next_try_at"])
        return WAITING
    except Exception as e:
        from fighthealthinsurance.shared_plan_documents import NoBucketCopy, note_count

        # The rows rolled back; a copy's file written before would not.
        for doc in saved:
            try:
                doc.document_enc.delete(save=False)
            except Exception:
                logger.warning("Could not delete a retried copy's file")
        if isinstance(e, NoBucketCopy):
            # The save counted it inside the transaction that rolled back.
            note_count(SharedPlanDocumentsDay.NO_BUCKET_OPT_IN, 1)
        return _failed(row, type(e).__name__)
    return SAVED


def _drop_expired(now: datetime.datetime) -> int:
    """Drop every copy past RETRY_LIFETIME, whatever the run's limit."""
    from fighthealthinsurance.shared_plan_documents import note_dropped

    expired = SharedCopyRetry.objects.filter(created_at__lte=now - RETRY_LIFETIME)
    count, _ = expired.delete()
    if count:
        note_dropped(SharedPlanDocumentsDay.DROPPED_NOT_SAVED, count)
        logger.info(f"Shared copies past their lifetime dropped: {count}")
    return count


def retry_due_copies(limit: int = MAX_RETRIES_PER_RUN) -> dict[str, int]:
    """Drop every copy past RETRY_LIFETIME, then make again up to ``limit``
    copies that are due, starting none after RUN_SECONDS. Returns counts by
    outcome. Never raises for one copy."""
    started = time.monotonic()
    now = timezone.now()
    counts = {SAVED: 0, ALREADY_SHARED: 0, WAITING: 0, DROPPED: 0, GONE: 0}
    counts[DROPPED] += _drop_expired(now)
    due = SharedCopyRetry.objects.filter(next_try_at__lte=now).order_by(
        "next_try_at", "id"
    )[:limit]
    for row in list(due):
        if time.monotonic() - started >= RUN_SECONDS:
            logger.info("Shared copy retries hit the run's time bound; the rest wait")
            break
        try:
            outcome = _retry_one(row)
        except Exception as e:
            logger.warning(f"A shared copy retry failed: {type(e).__name__}")
            try:
                outcome = _failed(row, type(e).__name__)
            except Exception:
                # The row went meanwhile (a removal); nothing to count.
                continue
        counts[outcome] += 1
    if any(counts.values()):
        logger.info(
            "Shared copies made again: "
            + ", ".join(f"{name} {n}" for name, n in counts.items())
        )
    return counts

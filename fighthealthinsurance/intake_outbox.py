"""Intake-journey outbox: append-only ``IntakeJourneyEvent`` rows.

The durable intake journey must never be lost in the gap between a Django
commit and the Temporal RPC that tells the journey about it (external
review). Each event is INSERTED in the same transaction as the mutation it
describes (the intent), and ``acked_at`` is set only after Temporal accepts
it. Intent-without-ack is a pending delivery that the relay
(``deliver_intake_events``, a CronJob) retries with per-row backoff, so a
process death or a Temporal blip after the commit is repaired within
minutes and nothing is ever dropped.

Contracts:

- Recording the intent is part of the mutation: if the INSERT fails, the
  mutation fails. That is correct -- the alternative is a silent gap.
- Once the intent has committed, NOTHING in delivery, acking, or
  bookkeeping may escape into the user-facing request: :func:`adeliver`
  is one exception boundary that logs and returns.
- Delivery is one idempotent Signal-With-Start keyed by the denial uuid
  (see ``temporal_client.signal_with_start_intake`` for the ack rules).
- Data protection: opaque identifiers cross into Temporal; the row holds
  timestamps, a counter, and an exception type name -- never case content.
"""

import datetime
from typing import Any, Optional

from asgiref.sync import async_to_sync
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from loguru import logger

INTAKE_STARTED = "intake_started"
FORM_COMPLETED = "form_completed"
NUDGE_SENT = "nudge_sent"
DELIVERABLE_EVENTS = (INTAKE_STARTED, FORM_COMPLETED)

# Retry backoff for a failed delivery: 30s doubling per attempt, capped at
# one hour; rows are never dropped.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600


def _enabled() -> bool:
    from fighthealthinsurance.temporal_client import _intake_enabled

    return _intake_enabled()


def _check_event(event: str) -> None:
    if event not in DELIVERABLE_EVENTS:
        raise ValueError(f"unknown deliverable intake event {event!r}")


def record_intent(denial: Any, event: str) -> Optional[Any]:
    """Insert the intent row; call INSIDE the transaction that persists the
    mutation it describes, so both commit or neither does. Idempotent (the
    unique constraint makes a repeat a no-op). Returns the row, or None
    while the intake journey is dark."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    _check_event(event)
    if not _enabled():
        return None
    row, _ = IntakeJourneyEvent.objects.get_or_create(denial=denial, event_type=event)
    return row


async def arecord_intent(denial: Any, event: str) -> Optional[Any]:
    """Async twin of :func:`record_intent` (one atomic INSERT-or-get)."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    _check_event(event)
    if not _enabled():
        return None
    row, _ = await IntakeJourneyEvent.objects.aget_or_create(
        denial=denial, event_type=event
    )
    return row


def backoff_seconds(attempts: int) -> int:
    doubled = BACKOFF_BASE_SECONDS * int(2 ** max(attempts - 1, 0))
    return min(doubled, BACKOFF_CAP_SECONDS)


async def _aschedule_retry(row: Any, exc: BaseException) -> None:
    """Bookkeeping for a failed delivery. Any failure here is logged and
    swallowed: bookkeeping must never mask the delivery outcome or escape."""
    from django.db.models import F

    from fighthealthinsurance.models import IntakeJourneyEvent

    try:
        attempts = int(row.attempts) + 1
        now = timezone.now()
        await IntakeJourneyEvent.objects.filter(pk=row.pk).aupdate(
            attempts=F("attempts") + 1,
            next_attempt_at=now + datetime.timedelta(seconds=backoff_seconds(attempts)),
            last_error_at=now,
            last_error=type(exc).__name__[:128],
        )
    except Exception:
        logger.opt(exception=True).warning(
            f"intake outbox: could not record retry for event {row.pk}"
        )


async def adeliver(row: Any) -> bool:
    """Deliver one event to Temporal and ack it. NEVER raises.

    Returns True when the event is (now or already) acknowledged. On a
    delivery failure the row is scheduled for retry and False is returned.
    Every step -- the reload, the RPC, the ack, the bookkeeping -- sits
    inside one boundary so nothing escapes into a user request.
    """
    from fighthealthinsurance.models import IntakeJourneyEvent
    from fighthealthinsurance.temporal_client import signal_with_start_intake

    try:
        if not _enabled():
            return False
        # Fresh read with the denial: the caller's instance may be stale.
        row = await IntakeJourneyEvent.objects.select_related("denial").aget(pk=row.pk)
        if row.acked_at is not None:
            return True
        denial = row.denial
        try:
            await signal_with_start_intake(
                denial.hashed_email,
                str(denial.uuid),
                bool((denial.raw_email or "").strip()),
                row.event_type,
            )
        except Exception as exc:
            logger.opt(exception=True).warning(
                f"intake outbox: {row.event_type} not delivered for denial "
                f"{denial.uuid}; scheduled for retry"
            )
            await _aschedule_retry(row, exc)
            return False
        await IntakeJourneyEvent.objects.filter(
            pk=row.pk, acked_at__isnull=True
        ).aupdate(acked_at=timezone.now())
        return True
    except Exception:
        # The ack or the reload failed after (or before) Temporal accepted:
        # the row stays pending and the relay retries; the request is fine.
        logger.opt(exception=True).warning(
            f"intake outbox: delivery bookkeeping failed for event {getattr(row, 'pk', '?')}"
        )
        return False


def deliver(row: Any) -> bool:
    return async_to_sync(adeliver)(row)


async def aclaim_nudge(denial: Any) -> bool:
    """Single-shot claim for the abandonment nudge: the unique
    (denial, nudge_sent) row IS the claim. False = already claimed."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    try:
        await IntakeJourneyEvent.objects.acreate(
            denial=denial, event_type=NUDGE_SENT, acked_at=timezone.now()
        )
    except IntegrityError:
        return False
    return True


async def ahas_event(denial: Any, event: str) -> bool:
    from fighthealthinsurance.models import IntakeJourneyEvent

    return await IntakeJourneyEvent.objects.filter(
        denial=denial, event_type=event
    ).aexists()


def _pending_pks(limit: int) -> list:
    from django.db.models import Q

    from fighthealthinsurance.models import IntakeJourneyEvent

    now = timezone.now()
    return list(
        IntakeJourneyEvent.objects.filter(
            event_type__in=DELIVERABLE_EVENTS, acked_at__isnull=True
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("next_attempt_at", "id")
        .values_list("pk", flat=True)[:limit]
    )


def _claim(pk: int) -> Optional[Any]:
    """Row-level claim inside the caller's transaction: SELECT ... FOR UPDATE
    SKIP LOCKED where the backend supports it, so two overlapping relays
    never deliver the same row; the acked/due re-check covers backends that
    cannot lock (sqlite)."""
    from django.db.models import Q

    from fighthealthinsurance.models import IntakeJourneyEvent

    qs = IntakeJourneyEvent.objects.filter(pk=pk, acked_at__isnull=True).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())
    )
    if connection.features.has_select_for_update_skip_locked:
        qs = qs.select_for_update(skip_locked=True)
    elif connection.features.has_select_for_update:
        qs = qs.select_for_update()
    return qs.first()


def sweep(limit: int = 200) -> dict:
    """The relay: re-deliver every due pending event, each in its own
    claim transaction and its own exception boundary, so one poison row
    never stops the rows behind it. Backoff via ``next_attempt_at`` keeps a
    permanently failing row from starving newer ones. Inert while dark."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    counts = {"delivered": 0, "failed": 0, "skipped": 0, "backlog": 0}
    if not _enabled():
        counts["skipped_disabled"] = 1
        return counts
    for pk in _pending_pks(limit):
        try:
            with transaction.atomic():
                row = _claim(pk)
                if row is None:
                    counts["skipped"] += 1
                    continue
                if deliver(row):
                    counts["delivered"] += 1
                else:
                    counts["failed"] += 1
        except Exception:
            counts["failed"] += 1
            logger.opt(exception=True).warning(
                f"intake outbox: relay skipped event {pk} after an error"
            )
    counts["backlog"] = IntakeJourneyEvent.objects.filter(
        event_type__in=DELIVERABLE_EVENTS, acked_at__isnull=True
    ).count()
    logger.info(f"intake outbox relay: {counts}")
    return counts

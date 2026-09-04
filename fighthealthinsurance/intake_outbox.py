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
- The relay claims in two phases: a short locked transaction stamps a
  claim token and expiry and COMMITS; the Temporal calls then run with no
  database lock held, ONE client per batch, a per-call timeout, and a
  conditional ack (token + still-unacked). A lost or expired claim acks
  nothing; the next run re-delivers idempotently.
- Data protection: opaque identifiers cross into Temporal; the row holds
  timestamps, counters, and an exception type name -- never case content.
"""

import asyncio
import datetime
import time
import uuid
from typing import Any, List, Optional, Tuple

from asgiref.sync import async_to_sync
from django.db import IntegrityError, connection, transaction
from django.db.models import Min, Q
from django.utils import timezone

from loguru import logger

INTAKE_STARTED = "intake_started"
FORM_COMPLETED = "form_completed"
NUDGE_CLAIMED = "nudge_claimed"
DELIVERABLE_EVENTS = (INTAKE_STARTED, FORM_COMPLETED)

OUTCOME_CLAIMED = "claimed"
OUTCOME_SKIPPED_COMPLETED = "skipped_completed"
OUTCOME_SENT = "sent"
OUTCOME_SMTP_FAILED = "smtp_failed"

# Retry backoff for a failed delivery: 30s doubling per attempt, capped at
# one hour; rows are never dropped.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
# How long a relay claim holds a row before another run may take it.
CLAIM_TTL_SECONDS = 120
# No single Temporal call may consume the relay's lifetime.
RPC_TIMEOUT_SECONDS = 15.0
# Stop STARTING new rows after this long; whatever is left waits for the
# next run (the CronJob's activeDeadlineSeconds sits above it).
DEFAULT_TIME_BUDGET_SECONDS = 240.0

_CONNECTION_CLASS_NAMES = {"RPCError", "ConnectError", "ServiceUnavailable"}


def _enabled() -> bool:
    from fighthealthinsurance.temporal_client import _intake_enabled

    return _intake_enabled()


def _check_event(event: str) -> None:
    if event not in DELIVERABLE_EVENTS:
        raise ValueError(f"unknown deliverable intake event {event!r}")


def is_connection_class(exc: BaseException) -> bool:
    """Transport-level failures: the relay treats a batch where EVERY attempt
    failed this way as systemic (Temporal unreachable), not as rows waiting
    on backoff."""
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)):
        return True
    name = type(exc).__name__
    return name in _CONNECTION_CLASS_NAMES or "connect" in name.lower()


# ----------------------------------------------------------------- intent --


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
    """Async twin of :func:`record_intent` (one atomic INSERT-or-get; for
    callers with no surrounding mutation transaction)."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    _check_event(event)
    if not _enabled():
        return None
    row, _ = await IntakeJourneyEvent.objects.aget_or_create(
        denial=denial, event_type=event
    )
    return row


# --------------------------------------------------------------- delivery --


def backoff_seconds(attempts: int) -> int:
    doubled = BACKOFF_BASE_SECONDS * int(2 ** max(attempts - 1, 0))
    return min(doubled, BACKOFF_CAP_SECONDS)


async def _aschedule_retry(
    row: Any, exc: BaseException, token: Optional[uuid.UUID] = None
) -> None:
    """Bookkeeping for a failed delivery. Conditional on the relay's claim
    token when one is held, so a lost claim never rewrites another run's
    row. Any failure here is logged and swallowed: bookkeeping must never
    mask the delivery outcome or escape."""
    from django.db.models import F

    from fighthealthinsurance.models import IntakeJourneyEvent

    try:
        attempts = int(row.attempts) + 1
        now = timezone.now()
        qs = IntakeJourneyEvent.objects.filter(pk=row.pk, acked_at__isnull=True)
        if token is not None:
            qs = qs.filter(claimed_token=token)
        await qs.aupdate(
            attempts=F("attempts") + 1,
            next_attempt_at=now + datetime.timedelta(seconds=backoff_seconds(attempts)),
            last_error_at=now,
            last_error=type(exc).__name__[:128],
            claimed_token=None,
            claimed_until=None,
        )
    except Exception:
        logger.opt(exception=True).warning(
            f"intake outbox: could not record retry for event {row.pk}"
        )


async def _acall(denial: Any, event: str, client: Any = None) -> None:
    from fighthealthinsurance.temporal_client import signal_with_start_intake

    await asyncio.wait_for(
        signal_with_start_intake(
            denial.hashed_email,
            str(denial.uuid),
            bool((denial.raw_email or "").strip()),
            event,
            client=client,
        ),
        timeout=RPC_TIMEOUT_SECONDS,
    )


async def adeliver(row: Any) -> bool:
    """Request-path delivery of one event: deliver and ack. NEVER raises.

    Returns True when the event is (now or already) acknowledged. On a
    delivery failure the row is scheduled for retry and False is returned.
    Every step -- the reload, the RPC, the ack, the bookkeeping -- sits
    inside one boundary so nothing escapes into a user request. Holds no
    claim: its ack is conditional only on the row still being unacked.
    """
    from fighthealthinsurance.models import IntakeJourneyEvent

    try:
        if not _enabled():
            return False
        row = await IntakeJourneyEvent.objects.select_related("denial").aget(pk=row.pk)
        if row.acked_at is not None:
            return True
        try:
            await _acall(row.denial, row.event_type)
        except Exception as exc:
            logger.opt(exception=True).warning(
                f"intake outbox: {row.event_type} not delivered for denial "
                f"{row.denial.uuid}; scheduled for retry"
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


# ------------------------------------------------------------------ nudge --


async def aclaim_nudge(denial: Any) -> Optional[Any]:
    """Single-shot claim for the abandonment nudge: the unique
    (denial, nudge_claimed) row IS the claim. Returns the claim row, or
    None when already claimed. Never released."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    try:
        return await IntakeJourneyEvent.objects.acreate(
            denial=denial,
            event_type=NUDGE_CLAIMED,
            acked_at=timezone.now(),
            attempted_at=timezone.now(),
            outcome=OUTCOME_CLAIMED,
        )
    except IntegrityError:
        return None


async def arecord_nudge_outcome(claim: Any, outcome: str, sent: bool = False) -> None:
    from fighthealthinsurance.models import IntakeJourneyEvent

    fields: dict = {"outcome": outcome}
    if sent:
        fields["sent_at"] = timezone.now()
    await IntakeJourneyEvent.objects.filter(pk=claim.pk).aupdate(**fields)


async def ahas_event(denial: Any, event: str) -> bool:
    from fighthealthinsurance.models import IntakeJourneyEvent

    return await IntakeJourneyEvent.objects.filter(
        denial=denial, event_type=event
    ).aexists()


# ------------------------------------------------------------------ relay --


def _due(now: datetime.datetime) -> Q:
    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)


def _unclaimed(now: datetime.datetime) -> Q:
    return Q(claimed_until__isnull=True) | Q(claimed_until__lte=now)


def claim_batch(limit: int = 200) -> List[Tuple[int, uuid.UUID]]:
    """Phase 1 of the relay: one SHORT locked transaction stamps a claim
    token + expiry on due, unclaimed (or claim-expired) pending rows and
    commits. SELECT ... FOR UPDATE SKIP LOCKED where the backend supports
    it, so two overlapping relays never claim the same rows. No network
    call happens while this lock is held."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    token = uuid.uuid4()
    now = timezone.now()
    with transaction.atomic():
        qs = (
            IntakeJourneyEvent.objects.filter(
                event_type__in=DELIVERABLE_EVENTS, acked_at__isnull=True
            )
            .filter(_due(now))
            .filter(_unclaimed(now))
            .order_by("next_attempt_at", "id")
        )
        if connection.features.has_select_for_update_skip_locked:
            qs = qs.select_for_update(skip_locked=True)
        elif connection.features.has_select_for_update:
            qs = qs.select_for_update()
        pks = list(qs.values_list("pk", flat=True)[:limit])
        if pks:
            IntakeJourneyEvent.objects.filter(pk__in=pks).update(
                claimed_token=token,
                claimed_until=now + datetime.timedelta(seconds=CLAIM_TTL_SECONDS),
            )
    return [(pk, token) for pk in pks]


async def adeliver_claimed(
    claims: List[Tuple[int, uuid.UUID]],
    time_budget: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> dict:
    """Phase 2 of the relay: deliver claimed rows with NO database lock
    held, ONE Temporal client for the batch, a per-call timeout, and a
    conditional ack (token + still-unacked). Each row is its own exception
    boundary. ``systemic`` is set when the client could not connect or
    every attempted delivery failed at the transport level."""
    from fighthealthinsurance.models import IntakeJourneyEvent
    from fighthealthinsurance.temporal_client import get_temporal_client

    counts: dict = {
        "attempted": 0,
        "delivered": 0,
        "failed": 0,
        "lost_claim": 0,
        "deferred": 0,
        "systemic": False,
        "client_error": "",
    }
    if not claims:
        return counts
    try:
        client = await asyncio.wait_for(get_temporal_client(), RPC_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.opt(exception=True).error(
            "intake outbox relay: could not connect a Temporal client; "
            f"{len(claims)} claimed row(s) left for the next run"
        )
        counts["systemic"] = True
        counts["client_error"] = type(exc).__name__
        counts["deferred"] = len(claims)
        return counts
    started = time.monotonic()
    connection_failures = 0
    for pk, token in claims:
        if time.monotonic() - started > time_budget:
            counts["deferred"] += 1
            continue
        counts["attempted"] += 1
        try:
            row = (
                await IntakeJourneyEvent.objects.select_related("denial")
                .filter(pk=pk, claimed_token=token)
                .afirst()
            )
            if row is None or row.acked_at is not None:
                counts["lost_claim"] += 1  # acked or re-claimed elsewhere
                continue
            try:
                await _acall(row.denial, row.event_type, client=client)
            except Exception as exc:
                if is_connection_class(exc):
                    connection_failures += 1
                logger.opt(exception=True).warning(
                    f"intake outbox relay: {row.event_type} not delivered for "
                    f"denial {row.denial.uuid}; scheduled for retry"
                )
                await _aschedule_retry(row, exc, token=token)
                counts["failed"] += 1
                continue
            updated = await IntakeJourneyEvent.objects.filter(
                pk=pk, claimed_token=token, acked_at__isnull=True
            ).aupdate(acked_at=timezone.now(), claimed_token=None, claimed_until=None)
            if updated:
                counts["delivered"] += 1
            else:
                # Claim lost or expired between the RPC and the ack: nothing
                # is acked here; the next run re-delivers idempotently.
                counts["lost_claim"] += 1
        except Exception:
            counts["failed"] += 1
            logger.opt(exception=True).warning(
                f"intake outbox relay: skipped event {pk} after an error"
            )
    if (
        counts["attempted"]
        and counts["failed"] == counts["attempted"]
        and connection_failures == counts["attempted"]
    ):
        counts["systemic"] = True
    return counts


def pending_stats() -> Tuple[int, float]:
    """(pending count, oldest pending age in seconds). Zero-cost while the
    table has no pending rows (an EXISTS probe on the partial index)."""
    from fighthealthinsurance.models import IntakeJourneyEvent

    pending = IntakeJourneyEvent.objects.filter(
        event_type__in=DELIVERABLE_EVENTS, acked_at__isnull=True
    )
    if not pending.exists():
        return 0, 0.0
    agg = pending.aggregate(n=Min("created_at"))
    oldest = agg["n"]
    age = (timezone.now() - oldest).total_seconds() if oldest else 0.0
    return pending.count(), max(age, 0.0)


def sweep(limit: int = 200, time_budget: float = DEFAULT_TIME_BUDGET_SECONDS) -> dict:
    """One relay run: claim (short lock, commit), deliver (no lock), report.
    Inert while the intake journey is dark."""
    counts: dict = {
        "attempted": 0,
        "delivered": 0,
        "failed": 0,
        "lost_claim": 0,
        "deferred": 0,
        "systemic": False,
        "client_error": "",
        "backlog": 0,
        "oldest_pending_seconds": 0.0,
    }
    if not _enabled():
        counts["skipped_disabled"] = 1
        return counts
    claims = claim_batch(limit)
    counts.update(async_to_sync(adeliver_claimed)(claims, time_budget=time_budget))
    counts["backlog"], counts["oldest_pending_seconds"] = pending_stats()
    logger.info(f"intake outbox relay: {counts}")
    return counts

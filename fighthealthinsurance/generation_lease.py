"""Per-denial generation lease: one writer at a time, with a fencing epoch.

The interactive websocket flow and the Temporal appeal journey can both see
"fewer than three drafts" and both start generating -- the fingerprint
constraint stops identical letters, but two generators writing different
letters means double model spend and a denial holding four to six drafts
(external reviews). The lease is the single-writer boundary:

- one ``AppealGenerationLease`` row per denial, reused for its lifetime;
- ``acquire`` succeeds only when the lease is free or expired, unless the
  caller ``steal``s -- a live human watching a socket always beats a
  background job, so the interactive path steals and the journey defers;
- every successful acquisition increments ``epoch``, the fencing token: a
  holder whose epoch is no longer current has been superseded and must stop
  quietly rather than keep writing;
- ``expires_at`` frees the denial if a holder dies without releasing;
  ``deadline`` is the attempt deadline every inner layer inherits.

Read-modify-write runs under ``select_for_update`` inside one transaction
(a no-op on sqlite, which serializes writers anyway). The async wrappers
bridge through channels' ``database_sync_to_async`` per the repo rule for
ORM-touching app code.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from channels.db import database_sync_to_async
from django.db import IntegrityError, transaction
from django.utils import timezone

DEFAULT_TTL_SECONDS = 300  # generation budget (240s) + drain margin

# Interval at which a live holder renews its lease, from the moment of
# acquisition (a model call can run several minutes before its first draft,
# longer than the TTL, so renewal cannot wait for the first insert).
EXTEND_INTERVAL_SECONDS = 10.0


def _now():
    """The lease clock. Indirected so tests can drive expiry deterministically
    instead of racing wall-clock sleeps against a shrunk TTL."""
    return timezone.now()


@dataclass(frozen=True)
class Lease:
    acquired: bool
    epoch: int
    deadline: Optional[datetime]


def new_holder(kind: str) -> str:
    """Opaque holder label (``journey:<uuid4>`` / ``interactive:<uuid4>``);
    diagnostic only, never authorization."""
    return f"{kind}:{uuid.uuid4()}"


def acquire(
    denial,
    holder: str,
    ttl_seconds: Optional[int] = None,
    steal: bool = False,
) -> Lease:
    """Take the lease if it is free/expired (or unconditionally when
    ``steal``). Returns the resulting epoch and deadline either way, so a
    refused caller can log who holds it. ``ttl_seconds`` defaults to
    DEFAULT_TTL_SECONDS at call time (so tests can shrink it)."""
    from fighthealthinsurance.models import AppealGenerationLease

    if ttl_seconds is None:
        ttl_seconds = DEFAULT_TTL_SECONDS
    now = _now()
    until = now + timedelta(seconds=ttl_seconds)
    with transaction.atomic():
        row = (
            AppealGenerationLease.objects.select_for_update()
            .filter(for_denial=denial)
            .first()
        )
        if row is None:
            try:
                with transaction.atomic():
                    row = AppealGenerationLease.objects.create(
                        for_denial=denial,
                        holder=holder,
                        expires_at=until,
                        deadline=until,
                        epoch=1,
                    )
                return Lease(True, 1, until)
            except IntegrityError:
                # Lost the first-use race; fall through to the locked row.
                row = (
                    AppealGenerationLease.objects.select_for_update()
                    .filter(for_denial=denial)
                    .get()
                )
        if row.expires_at > now and not steal:
            return Lease(False, row.epoch, row.deadline)
        row.holder = holder
        row.expires_at = until
        row.deadline = until
        row.epoch += 1
        row.save(update_fields=["holder", "expires_at", "deadline", "epoch"])
        return Lease(True, row.epoch, until)


def extend(denial, epoch: int, ttl_seconds: Optional[int] = None) -> bool:
    """Push the expiry out for the holder of ``epoch``. False means the
    lease was stolen, or expired (never revived): the caller no longer
    owns it."""
    from fighthealthinsurance.models import AppealGenerationLease

    if ttl_seconds is None:
        ttl_seconds = DEFAULT_TTL_SECONDS
    now = _now()
    return bool(
        AppealGenerationLease.objects.filter(
            for_denial=denial,
            epoch=epoch,
            # Never revive an expired lease: once it lapsed another holder
            # may acquire at the same epoch's successor any moment, and a
            # late extend would silently re-fence them out (review).
            expires_at__gt=now,
        ).update(expires_at=now + timedelta(seconds=ttl_seconds))
    )


class LeaseSuperseded(Exception):
    """The caller's epoch no longer holds a live lease on the denial."""


def assert_holds(denial, epoch: int) -> None:
    """Row-locked check that ``epoch`` currently holds a live lease. Called
    inside the writer's transaction so a draft insert and the ownership
    check commit together: a superseded generator cannot persist a draft
    after a steal, however far along it was (review)."""
    from fighthealthinsurance.models import AppealGenerationLease

    row = (
        AppealGenerationLease.objects.select_for_update()
        .filter(for_denial=denial)
        .first()
    )
    if row is None or row.epoch != epoch or row.expires_at <= _now():
        held = "none" if row is None else f"epoch {row.epoch}"
        raise LeaseSuperseded(f"epoch {epoch} does not hold the lease ({held})")


def release(denial, epoch: int) -> bool:
    """Expire the lease now, only if ``epoch`` still holds it (a stolen
    lease is left alone -- it belongs to someone else)."""
    from fighthealthinsurance.models import AppealGenerationLease

    return bool(
        AppealGenerationLease.objects.filter(for_denial=denial, epoch=epoch).update(
            expires_at=_now()
        )
    )


def current_epoch(denial) -> int:
    from fighthealthinsurance.models import AppealGenerationLease

    return (
        AppealGenerationLease.objects.filter(for_denial=denial)
        .values_list("epoch", flat=True)
        .first()
        or 0
    )


aacquire = database_sync_to_async(acquire)
aextend = database_sync_to_async(extend)
arelease = database_sync_to_async(release)
acurrent_epoch = database_sync_to_async(current_epoch)

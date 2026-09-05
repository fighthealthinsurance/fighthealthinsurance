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

import asyncio
import time
import uuid
from collections.abc import Callable
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


class RenewalClock:
    """The last CONFIRMED renewal of one held lease, shared by every path
    that extends it.

    A lease can be renewed from more than one place: a background task on a
    timer, and (interactively) once more after each draft is saved. Whether
    the holder still owns the lease is a property of the LEASE, not of any
    one renewal loop, so a renewal confirmed anywhere has to count
    everywhere. Tracking it per-loop meant a background loop whose calls kept
    raising declared the lease lost after one TTL while per-save renewals
    were succeeding the whole time -- stopping a generation that provably
    still held it (external review).

    Stamps are taken BEFORE the call that they describe, never after: the
    database anchors ``expires_at`` on a clock read taken before its own
    UPDATE, so crediting the round trip to ourselves would put our local
    deadline later than the real one -- exactly wrong when the database is
    slow, which is the only time any of this matters.
    """

    __slots__ = ("_last_confirmed",)

    def __init__(self, started: Optional[float] = None) -> None:
        self._last_confirmed = time.monotonic() if started is None else started

    def confirm(self, at: Optional[float] = None) -> None:
        """Record a renewal that the database acknowledged."""
        stamp = time.monotonic() if at is None else at
        if stamp > self._last_confirmed:
            self._last_confirmed = stamp

    def stale_for(self) -> float:
        """Seconds since the last renewal we can actually vouch for."""
        return time.monotonic() - self._last_confirmed


async def keep_renewed(
    denial,
    epoch: int,
    clock: "RenewalClock",
    *,
    on_lost: Optional[Callable[[str], None]] = None,
) -> None:
    """Renew a held lease until ownership is provably lost, then return.

    One policy, shared by the interactive flow and the appeal journey, so the
    two cannot drift apart.

    A renewal that RETURNS False is proof of loss: the row's epoch moved or
    the lease expired, so someone else owns it -- stop immediately. An
    EXCEPTION proves nothing of the kind; it says the database was briefly
    unreachable, not that ownership changed, and the lease stays valid until
    its TTL. Give up on errors only once more than a TTL has passed since the
    last confirmed renewal, at which point it really has expired.

    Each call is bounded by the extend interval so a HANGING renewal cannot
    park the loop forever without the deadline ever being evaluated -- a hang
    surfaces as a timeout the elapsed check can see.
    """
    interval = EXTEND_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        started = time.monotonic()
        try:
            renewed = await asyncio.wait_for(aextend(denial, epoch), timeout=interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            stale = clock.stale_for()
            if stale < DEFAULT_TTL_SECONDS:
                continue
            if on_lost is not None:
                on_lost(
                    f"no confirmed renewal for {stale:.0f}s (TTL "
                    f"{DEFAULT_TTL_SECONDS}s); lease has expired"
                )
            return
        if not renewed:
            if on_lost is not None:
                on_lost("renewal reported the lease is no longer held")
            return
        clock.confirm(started)


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
    with transaction.atomic():
        row = (
            AppealGenerationLease.objects.select_for_update()
            .filter(for_denial=denial)
            .first()
        )
        # The clock is read AFTER the row lock, never before: waiting on
        # select_for_update can itself cross the expiry, which would make a
        # stale `now` refuse a lease that has since expired -- or, when
        # stealing, write an expiry already in the past (external review).
        now = _now()
        until = now + timedelta(seconds=ttl_seconds)
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
                # Lost the first-use race; fall through to the locked row,
                # re-reading the clock after that second lock as well.
                row = (
                    AppealGenerationLease.objects.select_for_update()
                    .filter(for_denial=denial)
                    .get()
                )
                now = _now()
                until = now + timedelta(seconds=ttl_seconds)
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

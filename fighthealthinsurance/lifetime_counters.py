"""Lifetime counters that only ever go up.

Melanie (2026-09-11): the lifetime numbers on the staff status page must be
counters that deletion cannot change, not live counts with something added
back. So:

* ``appeals_generated`` moves at the moment a generated draft row is saved
  (post_save on ProposedAppeal: the streaming generator and the speculative
  precompute both create rows through ``save()``; a user's chosen/edited
  copy is created ``chosen=True`` and is a pick, not a generation, so it
  does not count).
* ``people_with_draft`` moves when a person's first generated draft is
  counted, which sets ``person_counted`` on every Denial row of theirs.
  The person is the hash the denial carries in the database at the time,
  read under the person's lock (``person_lock``: a per-hash advisory lock
  on PostgreSQL, taken before any row lock by every writer of the flag,
  plus FOR UPDATE on the rows), so two racing first drafts serialize and
  exactly one counts. A denial created later, or one whose hash changes,
  adopts the counted state of the person it belongs to under the same
  lock (Denial.save, deciding against the locked row, never against a
  cached hash), and any row still unflagged is flagged on the person's
  next draft, so the flag never depends on one particular denial
  surviving. It lives on rows that already carry the hash, survives draft
  churn (the precompute's rows are deleted on a text change), and leaves
  with the person's data on deletion, so nothing is retained beyond what
  the denials already are. The one ordering caveat: an outer transaction
  that writes denials of two different people holds both people's locks
  until it commits; no caller does that today.
* ``faxes_sent`` moves in finalize_fax the first time a row is finalized
  (an attempt was made, whatever its result), and ``faxes_delivered`` the
  first time it is finalized as delivered; each once per fax, recorded by
  once-only markers on the row (``attempt_counted``, ``delivery_counted``)
  that a resend never resets, unlike ``sent``/``fax_success`` which
  describe the latest attempt. Fax rows "ever created" is the id
  sequence: a row exists from the moment a fax is staged, before the user
  confirms and before any dial.

Every bump runs in its own savepoint, so a counter failure never breaks the
write that triggered it; the increments are single atomic UPDATEs and no
lock is taken on the hot path. The row is seeded once, at migration time,
from the rows present then.

Known limitation, deliberately not repaired: during a rolling deploy, pods
still on the previous image create drafts and finalize faxes without this
code until the rollout replaces them (minutes for the web pods; the fax
worker exits on SIGTERM as of the same release). Events in that window are
not counted, ever. A later repair from surviving rows was built and
withdrawn: it can only double-count an event whose receiver has not yet
committed, resurrect the hash of a person who was deleted meanwhile, or
mask a surviving uncounted event behind earlier deletions (review). A
bounded, documented gap is better than a repair that lies. Deletions before
the seed are unrecoverable, and a person who deletes and returns is counted
again (their new denials start unflagged).
"""

import hashlib
from typing import Any, Dict

from django.db import connection, transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from loguru import logger


def _add(**deltas: int) -> None:
    """One atomic UPDATE per bump; creates the singleton if needed."""
    from fighthealthinsurance.models import LifetimeCounters

    pk = LifetimeCounters.SINGLETON_ID
    LifetimeCounters.objects.get_or_create(pk=pk)
    changes: Dict[str, Any] = {k: F(k) + v for k, v in deltas.items() if v}
    changes["since"] = Coalesce(F("since"), Value(timezone.now()))
    LifetimeCounters.objects.filter(pk=pk).update(**changes)


def _advisory_key(hashed_email: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock."""
    digest = hashlib.blake2b(hashed_email.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def person_lock(hashed_email: str, *, blocking: bool = True) -> bool:
    """Serialize every writer of ``person_counted`` for one person.

    On PostgreSQL this is a transaction-scoped advisory lock keyed on the
    hash, released at commit or rollback, so a first-draft count, a denial
    created for the same person and a denial moving to that person cannot
    interleave, even while the person has no rows yet to lock. Other
    backends (sqlite in tests) have no equivalent and run single-writer
    here. Call it inside transaction.atomic(), before taking any row lock,
    so every writer acquires locks in the same order.

    Returns whether the lock is held. ``blocking=False`` uses the try
    variant and returns False rather than waiting: a caller that already
    holds another person's locks must never wait, or a re-key going the
    other way completes a deadlock cycle (review).
    """
    if not hashed_email or connection.vendor != "postgresql":
        return True
    key = _advisory_key(hashed_email)
    with connection.cursor() as cursor:
        if blocking:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
            return True
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        row = cursor.fetchone()
        return bool(row and row[0])


def _persisted_hash(denial_id: int) -> str:
    """The hash the denial carries in the database right now.

    Blank when the denial is gone or has no email: either way there is no
    person to count.
    """
    from fighthealthinsurance.models import Denial

    return (
        Denial.objects.filter(pk=denial_id)
        .values_list("hashed_email", flat=True)
        .first()
        or ""
    )


class _ReKeyed(Exception):
    """The denial left the locked person between the read and the lock."""


def _mark_person(denial_id: int) -> bool:
    """True exactly once per person, however many writers race.

    The person is the hash the draft's denial carries IN THE DATABASE, not
    whatever instance the writer held: a precompute can hold a denial whose
    email was changed by another request meanwhile (review). Takes the
    person's lock, then FOR UPDATE on their denial rows, and flips
    ``person_counted`` on every one that lacks it; the person counts only
    if none had it.

    If the denial is not among the locked rows it was re-keyed between the
    read and the lock. That attempt's savepoint is rolled back, releasing
    its row locks, and the next attempt reads the new hash -- but it takes
    the new person's lock with the try variant and gives up rather than
    waiting. Waiting there, while possibly still holding the previous
    person's advisory lock, is exactly the cycle a re-key going the other
    way completes (review); only the first attempt, which holds nothing,
    may block. If the row is gone (the person was deleted between the
    draft insert and this call) or has no hash, nothing is counted.
    """
    from fighthealthinsurance.models import Denial

    for attempt in range(3):
        hashed = _persisted_hash(denial_id)
        if not hashed:
            return False
        try:
            with transaction.atomic():  # savepoint: row locks go on rollback
                if not person_lock(hashed, blocking=attempt == 0):
                    logger.warning(
                        "Lifetime counters: person lock busy after a re-key; "
                        "draft counted, person not"
                    )
                    return False
                rows = list(
                    Denial.objects.select_for_update(of=("self",))
                    .filter(hashed_email=hashed)
                    .values_list("pk", "person_counted")
                )
                if denial_id not in {pk for pk, _flag in rows}:
                    raise _ReKeyed
                Denial.objects.filter(hashed_email=hashed, person_counted=False).update(
                    person_counted=True
                )
                return not any(flag for _pk, flag in rows)
        except _ReKeyed:
            continue
    logger.warning("Lifetime counters: denial re-keyed repeatedly; not counted")
    return False


@receiver(
    post_save,
    sender="fighthealthinsurance.ProposedAppeal",
    dispatch_uid="lifetime_counters_draft_saved",
)
def _on_draft_saved(sender, instance, created, raw=False, **kwargs) -> None:
    if not created or raw or instance.chosen:
        return
    try:
        with transaction.atomic():
            first = _mark_person(instance.for_denial_id)
            _add(appeals_generated=1, people_with_draft=1 if first else 0)
    except Exception:
        logger.opt(exception=True).warning("Lifetime counters: draft bump failed")


def note_fax_events(*, newly_sent: bool, newly_delivered: bool) -> None:
    """Called by finalize_fax inside the transaction that flips the row, and
    only for the transitions that actually matched (once per fax each)."""
    if not (newly_sent or newly_delivered):
        return
    try:
        with transaction.atomic():
            _add(
                faxes_sent=1 if newly_sent else 0,
                faxes_delivered=1 if newly_delivered else 0,
            )
    except Exception:
        logger.opt(exception=True).warning("Lifetime counters: fax bump failed")


def _present_people(Denial):
    return (
        Denial.objects.filter(proposedappeal__chosen=False)
        .exclude(hashed_email="")
        .values_list("hashed_email", flat=True)
        .distinct()
    )


def seed_from_present_rows(apps, schema_editor) -> None:
    """Migration seed: start the counters from the rows present today, and
    flag every present person's denials as counted. Reversible as a no-op."""
    LifetimeCounters = apps.get_model("fighthealthinsurance", "LifetimeCounters")
    ProposedAppeal = apps.get_model("fighthealthinsurance", "ProposedAppeal")
    Denial = apps.get_model("fighthealthinsurance", "Denial")
    FaxesToSend = apps.get_model("fighthealthinsurance", "FaxesToSend")
    if LifetimeCounters.objects.exists():
        return
    people = list(_present_people(Denial))
    Denial.objects.filter(hashed_email__in=people).update(person_counted=True)
    # Present faxes that were attempted / delivered are counted now and
    # marked, so a later resend of one of them cannot count it again.
    faxes_sent = FaxesToSend.objects.filter(sent=True).update(attempt_counted=True)
    faxes_delivered = FaxesToSend.objects.filter(fax_success=True).update(
        delivery_counted=True
    )
    LifetimeCounters.objects.create(
        pk=1,
        appeals_generated=ProposedAppeal.objects.filter(chosen=False).count(),
        people_with_draft=len(people),
        faxes_sent=faxes_sent,
        faxes_delivered=faxes_delivered,
        since=timezone.now(),
    )

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
  The decision is made under a row lock on those denials, so two racing
  first drafts serialize and exactly one counts; the flag lives on rows
  that already carry the hash, survives draft churn (the precompute's rows
  are deleted on a text change), and leaves with the person's data on
  deletion, so nothing is retained beyond what the denials already are.
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

from typing import Any, Dict

from django.db import transaction
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


def _mark_person(hashed_email: str) -> bool:
    """True exactly once per person, however many writers race.

    Locks the person's denial rows, then flips ``person_counted`` on all of
    them if none was set. A second writer waits on the lock and sees the
    flag. If the rows are gone (the person was deleted between the draft
    insert and this call), nothing is created and nothing is counted.
    """
    from fighthealthinsurance.models import Denial

    flags = list(
        Denial.objects.select_for_update(of=("self",))
        .filter(hashed_email=hashed_email)
        .values_list("person_counted", flat=True)
    )
    if not flags or any(flags):
        return False
    Denial.objects.filter(hashed_email=hashed_email).update(person_counted=True)
    return True


@receiver(
    post_save,
    sender="fighthealthinsurance.ProposedAppeal",
    dispatch_uid="lifetime_counters_draft_saved",
)
def _on_draft_saved(sender, instance, created, raw=False, **kwargs) -> None:
    if not created or raw or instance.chosen:
        return
    hashed = getattr(instance.for_denial, "hashed_email", "") or ""
    try:
        with transaction.atomic():
            first = _mark_person(hashed) if hashed else False
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

"""Idempotent, conflict-safe backfill of ``ProposedAppeal.text_fingerprint``.

One implementation shared by migration 0202 and the
``backfill_appeal_fingerprints`` management command, because the backfill
must run TWICE in a mixed-version deployment (external review):

1. The migration runs while old writer processes (pre-fingerprint
   ``save()``) may still be inserting NULL rows behind its cursor.
2. After the deploy completes and every old writer is gone, the post-rollout
   Job re-runs the same backfill in ``--strict`` mode, which only succeeds
   once a full pass finds nothing left to fill (i.e. the writers are gone).

After step 2, a ``chosen=False, text_fingerprint IS NULL`` row means
exactly one thing: a known historical duplicate (its fingerprint is
already taken for that denial). Journey counting relies on that meaning.

Every row is handled independently (no batch transaction), each row is
fingerprinted from a FRESH read whose observed text guards the write, and a
losing race on any single row is counted, never allowed to abort the run.
"""

import hashlib
from typing import Any

from django.db import IntegrityError, transaction

from loguru import logger

FILLED = "filled"
SKIPPED_DUPLICATE = "skipped_duplicate"
SKIPPED_EMPTY = "skipped_empty"
LOST_RACE = "lost_race"
OUTCOMES = (FILLED, SKIPPED_DUPLICATE, SKIPPED_EMPTY, LOST_RACE)


def fingerprint_text(text: Any) -> Any:
    """Stable copy of ``ProposedAppeal.fingerprint``'s normalization.

    Kept here (and referenced by the migration) so the backfill's meaning
    cannot drift if the model's normalization ever changes; any such change
    needs its own re-key migration, not a silent semantic shift here.
    """
    if not text or not str(text).strip():
        return None
    normalized = " ".join(str(text).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def fill_row(ProposedAppeal: Any, pk: int, attempts: int = 3) -> str:
    """Fingerprint one NULL row; returns the outcome (one of OUTCOMES).

    The text is re-read fresh on every attempt and the UPDATE is guarded by
    the observed ``appeal_text`` and ``for_denial_id``: a concurrent edit
    landing between the read and the write matches zero rows instead of
    attaching a stale fingerprint to the new text (external review). A
    zero-row match loops to a fresh read; the row is NEVER written from a
    stale snapshot.
    """
    for _ in range(attempts):
        row = (
            ProposedAppeal.objects.filter(
                pk=pk, text_fingerprint__isnull=True, chosen=False
            )
            .only("id", "appeal_text", "for_denial_id")
            .first()
        )
        if row is None:
            # Filled (or re-keyed/removed) by another writer since the scan.
            return LOST_RACE
        fp = fingerprint_text(row.appeal_text)
        if fp is None:
            return SKIPPED_EMPTY
        if (
            ProposedAppeal.objects.filter(
                for_denial_id=row.for_denial_id, text_fingerprint=fp
            )
            .exclude(pk=pk)
            .exists()
        ):
            return SKIPPED_DUPLICATE  # deliberately left NULL (known duplicate)
        try:
            with transaction.atomic():
                updated = ProposedAppeal.objects.filter(
                    pk=pk,
                    text_fingerprint__isnull=True,
                    appeal_text=row.appeal_text,
                    for_denial_id=row.for_denial_id,
                ).update(text_fingerprint=fp)
        except IntegrityError:
            # A concurrent writer took this fingerprint between our check
            # and the update; this row is therefore a duplicate now.
            return LOST_RACE
        if updated:
            return FILLED
        # Text changed under us: loop and recompute from the fresh row.
    return LOST_RACE


def run_backfill(ProposedAppeal: Any) -> dict:
    """Fill NULL fingerprints on un-chosen rows; returns counters.

    ``ProposedAppeal`` is passed in so the migration can hand over its
    historical model while the management command passes the live one.
    """
    counts = {k: 0 for k in OUTCOMES}
    pks = list(
        ProposedAppeal.objects.filter(text_fingerprint__isnull=True, chosen=False)
        .order_by("id")
        .values_list("id", flat=True)
    )
    for pk in pks:
        counts[fill_row(ProposedAppeal, pk)] += 1
    counts["remaining_null"] = ProposedAppeal.objects.filter(
        text_fingerprint__isnull=True, chosen=False
    ).count()
    logger.info(f"appeal fingerprint backfill: {counts}")
    return counts


REKEYED = "rekeyed"
MISMATCH_DUPLICATE = "mismatch_duplicate"


def verify_fingerprints(ProposedAppeal: Any) -> dict:
    """Integrity pass: every un-chosen fingerprinted row must satisfy
    ``text_fingerprint == fingerprint_text(appeal_text)``.

    NULL checks alone cannot prove the invariant during a rollout: a pod
    still on pre-fingerprint code can EDIT text under a fingerprint that
    no longer matches it (external review). Such rows are re-keyed with the
    same observed-text-guarded conditional update ``fill_row`` uses; a
    re-key that would collide with a twin is left as-is and counted as
    ``mismatch_duplicate`` (it is a duplicate now). Returns counters; a
    non-zero ``rekeyed`` is proof an old writer touched the table.
    """
    counts = {REKEYED: 0, MISMATCH_DUPLICATE: 0, "checked": 0}
    qs = (
        ProposedAppeal.objects.filter(text_fingerprint__isnull=False, chosen=False)
        .order_by("id")
        .only("id", "appeal_text", "text_fingerprint", "for_denial_id")
    )
    for row in qs.iterator(chunk_size=500):
        counts["checked"] += 1
        expected = fingerprint_text(row.appeal_text)
        if expected is None or expected == row.text_fingerprint:
            continue
        if (
            ProposedAppeal.objects.filter(
                for_denial_id=row.for_denial_id, text_fingerprint=expected
            )
            .exclude(pk=row.pk)
            .exists()
        ):
            counts[MISMATCH_DUPLICATE] += 1
            continue
        try:
            with transaction.atomic():
                updated = ProposedAppeal.objects.filter(
                    pk=row.pk,
                    appeal_text=row.appeal_text,
                    text_fingerprint=row.text_fingerprint,
                ).update(text_fingerprint=expected)
        except IntegrityError:
            counts[MISMATCH_DUPLICATE] += 1
            continue
        if updated:
            counts[REKEYED] += 1
    logger.info(f"appeal fingerprint verify: {counts}")
    return counts

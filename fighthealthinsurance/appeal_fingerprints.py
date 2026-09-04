"""Idempotent, conflict-safe backfill of ``ProposedAppeal.text_fingerprint``.

One implementation shared by migration 0202 and the
``backfill_appeal_fingerprints`` management command, because the backfill
must run TWICE in a mixed-version deployment (external review):

1. The migration runs while old writer processes (pre-fingerprint
   ``save()``) may still be inserting NULL rows behind its cursor.
2. After the deploy completes and every old writer is gone, the management
   command re-runs the same backfill to catch rows the migration raced,
   then audits what remains.

After step 2, a ``chosen=False, text_fingerprint IS NULL`` row means
exactly one thing: a known historical duplicate (its fingerprint is
already taken for that denial). Journey counting relies on that meaning.

Every row is handled independently (no batch transaction) and a losing
race on any single row -- another writer inserting the same fingerprint
between our check and update -- is caught and skipped, never allowed to
abort the run.
"""

import hashlib

from django.db import IntegrityError, transaction

from loguru import logger


def fingerprint_text(text):
    """Stable copy of ``ProposedAppeal.fingerprint``'s normalization.

    Kept here (and referenced by the migration) so the backfill's meaning
    cannot drift if the model's normalization ever changes; any such change
    needs its own re-key migration, not a silent semantic shift here.
    """
    if not text or not str(text).strip():
        return None
    normalized = " ".join(str(text).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def run_backfill(ProposedAppeal) -> dict:
    """Fill NULL fingerprints on un-chosen rows; returns counters.

    ``ProposedAppeal`` is passed in so the migration can hand over its
    historical model while the management command passes the live one.
    Duplicate content (fingerprint already taken for the denial) is left
    NULL deliberately -- that IS the known-duplicate marker.
    """
    filled = skipped_duplicate = skipped_empty = lost_race = 0
    qs = (
        ProposedAppeal.objects.filter(text_fingerprint__isnull=True, chosen=False)
        .order_by("id")
        .only("id", "appeal_text", "for_denial_id")
    )
    for row in qs.iterator(chunk_size=500):
        fp = fingerprint_text(row.appeal_text)
        if fp is None:
            skipped_empty += 1
            continue
        if (
            ProposedAppeal.objects.filter(
                for_denial_id=row.for_denial_id, text_fingerprint=fp
            )
            .exclude(pk=row.pk)
            .exists()
        ):
            skipped_duplicate += 1
            continue
        try:
            with transaction.atomic():
                ProposedAppeal.objects.filter(
                    pk=row.pk, text_fingerprint__isnull=True
                ).update(text_fingerprint=fp)
            filled += 1
        except IntegrityError:
            # A concurrent writer took this fingerprint between our check
            # and the update; this row is therefore a duplicate now.
            lost_race += 1
    remaining = ProposedAppeal.objects.filter(
        text_fingerprint__isnull=True, chosen=False
    ).count()
    counts = {
        "filled": filled,
        "skipped_duplicate": skipped_duplicate,
        "skipped_empty": skipped_empty,
        "lost_race": lost_race,
        "remaining_null": remaining,
    }
    logger.info(f"appeal fingerprint backfill: {counts}")
    return counts

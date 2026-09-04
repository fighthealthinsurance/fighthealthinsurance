"""Backfill ``text_fingerprint`` for rows that predate the field.

0201 added the fingerprint column and the partial unique constraint but
left legacy rows NULL -- and NULL rows are exempt from the constraint, so
a retry regenerating a legacy draft's text could store it again (external
review). This fills eligible rows.

FROZEN ON PURPOSE: this migration carries its own copy of the normalization
and fill logic instead of importing :mod:`fighthealthinsurance.appeal_fingerprints`.
A migration must keep doing exactly what it did when it first ran; the
importable module is the LIVE copy used by the post-rollout command and may
evolve. If the normalization ever changes, that is a new re-key migration,
not an edit here (external review).

MIXED-VERSION SAFETY: this runs while OLD writer processes (whose save()
does not fingerprint) may still be inserting rows behind its cursor, so it
cannot be the last word:

- ``atomic = False`` + per-row handling means a concurrent writer racing
  one row skips that row instead of rolling back the whole migration.
- The deploy script waits for every writer Deployment and the Ray cluster
  to finish rolling, then runs ``backfill_appeal_fingerprints --strict``
  (a Job with backoff), which re-fills, re-verifies every fingerprint
  against its current text, and fails until the table is quiescent.

Collision policy: a row whose fingerprint is already taken for its denial
stays NULL -- those are the historical double-stores; nothing is deleted,
and journey counting excludes NULL rows. Chosen rows are skipped: they are
deliberate copies of the picked draft and carry no fingerprint by design.
"""

import hashlib

from django.db import IntegrityError, migrations, transaction


def _fingerprint(text):
    if not text or not str(text).strip():
        return None
    normalized = " ".join(str(text).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _fill_row(ProposedAppeal, pk, attempts=3):
    # Fresh read per attempt; the UPDATE is guarded by the observed text and
    # denial so a concurrent edit can never attach a stale fingerprint.
    for _ in range(attempts):
        row = (
            ProposedAppeal.objects.filter(
                pk=pk, text_fingerprint__isnull=True, chosen=False
            )
            .only("id", "appeal_text", "for_denial_id")
            .first()
        )
        if row is None:
            return
        fp = _fingerprint(row.appeal_text)
        if fp is None:
            return
        if (
            ProposedAppeal.objects.filter(
                for_denial_id=row.for_denial_id, text_fingerprint=fp
            )
            .exclude(pk=pk)
            .exists()
        ):
            return  # known duplicate: deliberately left NULL
        try:
            with transaction.atomic():
                updated = ProposedAppeal.objects.filter(
                    pk=pk,
                    text_fingerprint__isnull=True,
                    appeal_text=row.appeal_text,
                    for_denial_id=row.for_denial_id,
                ).update(text_fingerprint=fp)
        except IntegrityError:
            return
        if updated:
            return


def backfill_fingerprints(apps, schema_editor):
    ProposedAppeal = apps.get_model("fighthealthinsurance", "ProposedAppeal")
    pks = list(
        ProposedAppeal.objects.filter(text_fingerprint__isnull=True, chosen=False)
        .order_by("id")
        .values_list("id", flat=True)
    )
    for pk in pks:
        _fill_row(ProposedAppeal, pk)


class Migration(migrations.Migration):

    # Deliberately non-atomic: each row commits independently so one lost
    # race with a live writer cannot roll back the entire backfill.
    atomic = False

    dependencies = [
        ("fighthealthinsurance", "0201_proposedappeal_text_fingerprint"),
    ]

    operations = [
        migrations.RunPython(backfill_fingerprints, migrations.RunPython.noop),
    ]

"""Backfill ``text_fingerprint`` for rows that predate the field.

0201 added the fingerprint column and the partial unique constraint but
left legacy rows NULL -- and NULL rows are exempt from the constraint, so
a retry regenerating a legacy draft's text could store it again (external
review). This fills eligible rows via the shared, idempotent backfill in
:mod:`fighthealthinsurance.appeal_fingerprints`.

MIXED-VERSION SAFETY (external review): this migration runs while OLD
writer processes (whose save() does not fingerprint) may still be
inserting rows behind its cursor, so it cannot be the last word:

- ``atomic = False`` + per-row handling means a concurrent writer racing
  one row skips that row instead of rolling back the whole migration.
- After the deployment completes and old writers are drained, ops re-runs
  the same backfill with ``python manage.py backfill_appeal_fingerprints``
  and audits the remaining NULLs (the command prints counts). Only after
  that re-run does ``chosen=False, text_fingerprint IS NULL`` mean
  exactly "known historical duplicate".

Collision policy: a row whose fingerprint is already taken for its denial
stays NULL -- those are the historical double-stores; nothing is deleted,
and journey counting excludes NULL rows.

Chosen rows are skipped: they are deliberate copies of the picked draft
(see ``ProposedAppeal.save``) and carry no fingerprint by design.

The shared module touches only stable columns (id, appeal_text,
for_denial_id, text_fingerprint, chosen), so handing it the historical
model is safe.
"""

from django.db import migrations

from fighthealthinsurance.appeal_fingerprints import run_backfill


def backfill_fingerprints(apps, schema_editor):
    run_backfill(apps.get_model("fighthealthinsurance", "ProposedAppeal"))


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

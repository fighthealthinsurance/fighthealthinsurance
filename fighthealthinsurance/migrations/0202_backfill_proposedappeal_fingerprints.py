"""Backfill ``text_fingerprint`` for rows that predate the field.

0201 added the fingerprint column and the partial unique constraint but
left legacy rows NULL -- and NULL rows are exempt from the constraint, so
a retry regenerating a legacy draft's text could store it again (external
review). This fills eligible rows.

Collision policy: when a row's computed fingerprint is already taken for
its denial (an already-fingerprinted twin, or an earlier row in this same
pass), the row is left NULL. Those are precisely the historical
double-stores; nothing is deleted, but journey counting treats NULL as
"known legacy duplicate" and excludes it, so three copies of one draft no
longer satisfy the three-draft target.

Chosen rows are skipped: they are deliberate copies of the picked draft
(see ``ProposedAppeal.save``) and carry no fingerprint by design.
"""

import hashlib

from django.db import migrations


def _fingerprint(text):
    # Frozen copy of ProposedAppeal.fingerprint's normalization: migrations
    # must not drift with future model-code changes.
    if not text or not str(text).strip():
        return None
    normalized = " ".join(str(text).lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def backfill_fingerprints(apps, schema_editor):
    ProposedAppeal = apps.get_model("fighthealthinsurance", "ProposedAppeal")
    taken = set()  # (denial_id, fingerprint) claimed during this pass
    qs = (
        ProposedAppeal.objects.filter(text_fingerprint__isnull=True, chosen=False)
        .order_by("id")
        .only("id", "appeal_text", "for_denial_id")
    )
    for row in qs.iterator(chunk_size=500):
        fp = _fingerprint(row.appeal_text)
        if fp is None:
            continue
        key = (row.for_denial_id, fp)
        if (
            key in taken
            or ProposedAppeal.objects.filter(
                for_denial_id=row.for_denial_id, text_fingerprint=fp
            )
            .exclude(pk=row.pk)
            .exists()
        ):
            continue  # duplicate content: stays NULL (see module docstring)
        taken.add(key)
        ProposedAppeal.objects.filter(pk=row.pk).update(text_fingerprint=fp)


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0201_proposedappeal_text_fingerprint"),
    ]

    operations = [
        migrations.RunPython(backfill_fingerprints, migrations.RunPython.noop),
    ]

from django.db import migrations, models


def purge_generic_context(apps, schema_editor):
    """Drop ALL cached GenericContextGeneration rows, not just duplicates.

    Rows written before this migration may embed supplemental citations
    (microsite evidence snippets/extralinks, ECRI guidelines): the old miss
    path appended them to the ML result BEFORE caching, and they are not
    distinguishable from ML content once stored. Retaining such a row would
    keep leaking one patient's microsite content into every later patient's
    citations and re-duplicate the extras on every cache hit (the read path
    appends them again) — this cache has no TTL, so indefinitely. Purging is
    safe: it is a pure cache and rebuilds lazily, from ML output only, under
    the unique constraint added below.

    This also trivially satisfies the dedup this migration originally set out
    to do (issue #907): no rows, no duplicates, and the constraint keeps it
    that way.
    """
    GenericContextGeneration = apps.get_model(
        "fighthealthinsurance", "GenericContextGeneration"
    )
    GenericContextGeneration.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0197_denial_candidate_denial_text_summary"),
    ]

    operations = [
        migrations.RunPython(
            purge_generic_context,
            # Purged cache rows are unrecoverable (and don't need to be — the
            # cache rebuilds on demand); reversing the constraint doesn't need
            # them back.
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="genericcontextgeneration",
            constraint=models.UniqueConstraint(
                fields=("procedure", "diagnosis"),
                name="generic_ctx_proc_diag_uniq",
            ),
        ),
        # The unique constraint above is backed by its own unique index on the
        # same columns, so the original plain index (from 0112) is redundant
        # write overhead.
        migrations.RemoveIndex(
            model_name="genericcontextgeneration",
            name="fighthealth_procedu_ac1633_idx",
        ),
    ]

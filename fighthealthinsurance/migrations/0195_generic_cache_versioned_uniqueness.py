# Versioned uniqueness for the generic ML caches.
#
# GenericContextGeneration / GenericQuestionGeneration previously had no
# uniqueness on (procedure, diagnosis), so concurrent cache misses could
# insert duplicate rows — and once duplicates exist, update_or_create raises
# MultipleObjectsReturned, permanently breaking the cache write for that pair.
#
# This migration is deliberately safe to run on databases that already contain
# duplicates:
#   1. Add `version` (default CURRENT_VERSION=1) and `updated_at` (null =
#      "stale", so pre-existing rows get refreshed organically by the TTL).
#   2. For each (procedure, diagnosis) group with more than one row at
#      version=1, keep the newest row live and park every older duplicate at
#      version=-pk. Parked versions are unique by construction (pk is unique),
#      nothing is deleted, and the loop cannot violate the constraint because
#      the constraint is added afterwards.
#   3. Add the (procedure, diagnosis, version) UniqueConstraint, which now
#      cannot fail: at most one row per pair remains at version=1.
#
# Rolling-deploy note: between this migration and the code rollout, still-
# running old code inserting a brand-new (procedure, diagnosis) pair will hit
# the NOT NULL `version` column it doesn't populate... except AddField ships a
# default at the schema level during the migration itself; after the default
# is dropped, such inserts fail and are swallowed by the helpers' existing
# try/except (cache writes silently skip until the deploy completes). Standard
# Django rolling-deploy tradeoff; reads are unaffected.

from django.db import migrations, models

# Matches <Model>.CURRENT_VERSION at the time this migration was written.
# Kept as a literal because historical models in migrations don't carry class
# attributes.
_CURRENT_VERSION = 1


def _park_duplicates(Model, db_alias) -> None:
    """Keep the newest row per (procedure, diagnosis) at version=1; park the
    rest at version=-pk."""
    from django.db.models import Count

    objects = Model.objects.using(db_alias)
    dupe_groups = (
        objects.filter(version=_CURRENT_VERSION)
        .values("procedure", "diagnosis")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for group in dupe_groups.iterator():
        # .only() keeps memory bounded even for a pathological duplicate
        # group — the JSON blobs aren't needed to pick and park rows.
        rows = list(
            objects.filter(
                procedure=group["procedure"],
                diagnosis=group["diagnosis"],
                version=_CURRENT_VERSION,
            )
            .only("id", "created_at", "version")
            .order_by("-created_at", "-id")
        )
        to_park = rows[1:]
        for row in to_park:
            row.version = -row.id
        if to_park:
            objects.bulk_update(to_park, ["version"], batch_size=500)


def dedupe_generic_caches(apps, schema_editor):
    # Pin all reads/writes to the connection being migrated so a multi-DB
    # setup can't dedupe one database while constraining another.
    db_alias = schema_editor.connection.alias
    for model_name in ("GenericContextGeneration", "GenericQuestionGeneration"):
        _park_duplicates(apps.get_model("fighthealthinsurance", model_name), db_alias)


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0194_regulator_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="genericcontextgeneration",
            name="updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="genericcontextgeneration",
            name="version",
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name="genericquestiongeneration",
            name="updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="genericquestiongeneration",
            name="version",
            field=models.IntegerField(default=1),
        ),
        # Park duplicates BEFORE the constraint lands so it cannot fail.
        migrations.RunPython(dedupe_generic_caches, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="genericcontextgeneration",
            constraint=models.UniqueConstraint(
                fields=("procedure", "diagnosis", "version"),
                name="generic_ctx_proc_diag_ver_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="genericquestiongeneration",
            constraint=models.UniqueConstraint(
                fields=("procedure", "diagnosis", "version"),
                name="generic_q_proc_diag_ver_uniq",
            ),
        ),
    ]

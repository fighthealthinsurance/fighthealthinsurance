"""Add a Postgres trigram GIN index on IMRDecision.search_text.

Postgres-only. On other backends (SQLite for dev/tests) the migration is a
no-op so it can run unchanged everywhere.
"""

from django.db import migrations

PG_INDEX_NAME = "imrdec_search_trgm_idx"


def add_pg_trgm_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    schema_editor.execute(
        f"CREATE INDEX IF NOT EXISTS {PG_INDEX_NAME} "
        f"ON fighthealthinsurance_imrdecision "
        f"USING gin (search_text gin_trgm_ops)"
    )


def drop_pg_trgm_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f"DROP INDEX IF EXISTS {PG_INDEX_NAME}")


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0162_imrdecision_search_text"),
    ]

    operations = [
        migrations.RunPython(add_pg_trgm_index, drop_pg_trgm_index),
    ]

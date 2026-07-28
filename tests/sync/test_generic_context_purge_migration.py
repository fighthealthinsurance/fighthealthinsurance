"""Regression coverage for migration 0198's GenericContextGeneration purge.

Migrates the test database back to 0197 (before the unique constraint), seeds
legacy cache rows — including duplicates — then applies 0198 for real,
verifying that the purge removes every pre-existing row (legacy rows may
embed per-denial supplemental citations, so none can be retained) and that
the unique constraint applies cleanly and is enforced afterwards.
TransactionTestCase keeps the schema changes outside a wrapping transaction,
which SQLite requires; the final migrate returns the database to the latest
state for subsequent tests.
"""

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

APP = "fighthealthinsurance"
BEFORE = [(APP, "0197_denial_candidate_denial_text_summary")]
AFTER = [(APP, "0198_generic_context_purge_and_unique")]


class GenericContextPurgeMigrationTest(TransactionTestCase):
    def test_migration_purges_legacy_rows_and_applies_constraint(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(BEFORE)
            old_apps = executor.loader.project_state(BEFORE).apps
            OldModel = old_apps.get_model(APP, "GenericContextGeneration")

            # Legacy rows, duplicate and unique pairs alike. All must go: a
            # pre-0198 row may embed supplemental (per-denial) citations that
            # cannot be told apart from ML content.
            OldModel.objects.create(
                procedure="ct scan", diagnosis="headache", generated_context=["old"]
            )
            OldModel.objects.create(
                procedure="ct scan", diagnosis="headache", generated_context=["new"]
            )
            OldModel.objects.create(
                procedure="xray",
                diagnosis="fracture",
                generated_context=["ml", "Microsite evidence snippet"],
            )

            executor.loader.build_graph()
            executor.migrate(AFTER)
            NewModel = executor.loader.project_state(AFTER).apps.get_model(
                APP, "GenericContextGeneration"
            )

            self.assertEqual(NewModel.objects.count(), 0)

            # The constraint is live: fresh writes work once per pair, and a
            # duplicate pair is rejected.
            NewModel.objects.create(
                procedure="xray", diagnosis="fracture", generated_context=["fresh"]
            )
            with self.assertRaises(IntegrityError), transaction.atomic():
                NewModel.objects.create(
                    procedure="xray",
                    diagnosis="fracture",
                    generated_context=["duplicate"],
                )
        finally:
            call_command("migrate", verbosity=0)

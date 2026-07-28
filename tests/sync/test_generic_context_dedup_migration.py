"""Regression coverage for migration 0198's GenericContextGeneration dedup.

Migrates the test database back to 0197 (before the unique constraint), seeds
duplicate cache rows, then applies 0198 for real — verifying both that the
dedup keeps the most recent row per (procedure, diagnosis) pair and that the
constraint applies cleanly on a database that held duplicates.
TransactionTestCase keeps the schema changes outside a wrapping transaction,
which SQLite requires; the final migrate returns the database to the latest
state for subsequent tests.
"""

from datetime import timedelta

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

APP = "fighthealthinsurance"
BEFORE = [(APP, "0197_denial_candidate_denial_text_summary")]
AFTER = [(APP, "0198_generic_context_dedup_and_unique")]


class GenericContextDedupMigrationTest(TransactionTestCase):
    def test_migration_dedupes_and_applies_constraint(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(BEFORE)
            old_apps = executor.loader.project_state(BEFORE).apps
            OldModel = old_apps.get_model(APP, "GenericContextGeneration")

            now = timezone.now()
            older = OldModel.objects.create(
                procedure="ct scan", diagnosis="headache", generated_context=["old"]
            )
            newer = OldModel.objects.create(
                procedure="ct scan", diagnosis="headache", generated_context=["new"]
            )
            # auto_now_add ignores values passed at create time, so set the
            # timestamps that decide "most recent" via queryset update.
            OldModel.objects.filter(pk=older.pk).update(
                created_at=now - timedelta(days=2)
            )
            OldModel.objects.filter(pk=newer.pk).update(created_at=now)
            # Identical timestamps: the higher pk must win the tie.
            tie_low = OldModel.objects.create(
                procedure="mri", diagnosis="tear", generated_context=["tie-low"]
            )
            tie_high = OldModel.objects.create(
                procedure="mri", diagnosis="tear", generated_context=["tie-high"]
            )
            OldModel.objects.filter(pk__in=[tie_low.pk, tie_high.pk]).update(
                created_at=now
            )
            unduplicated = OldModel.objects.create(
                procedure="xray", diagnosis="fracture", generated_context=["only"]
            )

            executor.loader.build_graph()
            executor.migrate(AFTER)
            NewModel = executor.loader.project_state(AFTER).apps.get_model(
                APP, "GenericContextGeneration"
            )

            survivors = {
                (row.procedure, row.diagnosis): row for row in NewModel.objects.all()
            }
            self.assertEqual(len(survivors), 3)
            self.assertEqual(survivors[("ct scan", "headache")].pk, newer.pk)
            self.assertEqual(
                survivors[("ct scan", "headache")].generated_context, ["new"]
            )
            self.assertEqual(survivors[("mri", "tear")].pk, tie_high.pk)
            self.assertEqual(survivors[("xray", "fracture")].pk, unduplicated.pk)

            # The constraint is live: a duplicate pair is rejected.
            with self.assertRaises(IntegrityError), transaction.atomic():
                NewModel.objects.create(
                    procedure="xray",
                    diagnosis="fracture",
                    generated_context=["duplicate"],
                )
        finally:
            call_command("migrate", verbosity=0)

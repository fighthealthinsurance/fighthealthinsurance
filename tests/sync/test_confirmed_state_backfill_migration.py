"""Regression coverage for migration 0209's confirmed-state backfill.

Starts from rows in the old shape -- the shape production is in right now,
where the review POST wrote the state the person typed to ``state`` only and
``your_state`` still holds the guess made from their zip code -- then applies
0209 for real and checks the correction has moved onto ``your_state``, the
column the appeal generator and the UCR lookup read.

TransactionTestCase keeps the migrate calls outside a wrapping transaction; the
final migrate returns the database to the latest state for subsequent tests.
"""

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

APP = "fighthealthinsurance"
BEFORE = [(APP, "0208_dataremovaltotals")]
AFTER = [(APP, "0209_backfill_confirmed_state_to_your_state")]


class ConfirmedStateBackfillMigrationTest(TransactionTestCase):
    def _make(self, Denial, label, **kwargs):
        return Denial.objects.create(
            hashed_email=f"hashed-{label}",
            denial_text=f"Denied: {label}",
            **kwargs,
        )

    def test_the_backfill_moves_a_correction_onto_the_column_the_appeal_reads(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(BEFORE)
            old_apps = executor.loader.project_state(BEFORE).apps
            OldDenial = old_apps.get_model(APP, "Denial")

            # The row the bug already bit: they typed CA on the review page,
            # their zip guessed NY, and only `state` got the correction.
            corrected = self._make(OldDenial, "corrected", state="CA", your_state="NY")
            # Same correction, but the zip lookup had failed so there is no
            # guess at all. NULL must be filled too, not skipped.
            no_guess = self._make(OldDenial, "no-guess", state="CA", your_state=None)
            blank_guess = self._make(
                OldDenial, "blank-guess", state="CA", your_state=""
            )
            # Nobody confirmed anything: the zip inference is still the best
            # value this row has and must survive untouched.
            never_confirmed = self._make(
                OldDenial, "never-confirmed", state="", your_state="NY"
            )
            null_state = self._make(
                OldDenial, "null-state", state=None, your_state="NY"
            )
            # Already in step; nothing to do.
            agreeing = self._make(OldDenial, "agreeing", state="TX", your_state="TX")

            executor.loader.build_graph()
            executor.migrate(AFTER)
            NewDenial = executor.loader.project_state(AFTER).apps.get_model(
                APP, "Denial"
            )

            def reread(row):
                return NewDenial.objects.get(pk=row.pk)

            self.assertEqual(reread(corrected).your_state, "CA")
            self.assertEqual(reread(corrected).state, "CA")
            self.assertEqual(reread(no_guess).your_state, "CA")
            self.assertEqual(reread(blank_guess).your_state, "CA")

            self.assertEqual(reread(never_confirmed).your_state, "NY")
            self.assertEqual(reread(null_state).your_state, "NY")
            self.assertEqual(reread(agreeing).your_state, "TX")
        finally:
            call_command("migrate", verbosity=0)

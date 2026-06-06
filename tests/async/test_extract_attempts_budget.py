"""Tests for the bounded extract_attempts retry budget.

Self-review of an earlier commit ("Don't mark extract-finished on
failure") surfaced that ``extract_entity``'s only re-entry gate was
``extract_procedure_diagnosis_finished`` — and that flag now stays
``False`` on failure.  Without an attempt counter, a denial whose
extraction reliably fails could re-run the full LLM extraction (and
fire-and-forget PubMed/citation tasks) on every WebSocket reconnect.

These tests pin down the budget enforcement: each failure bumps
``extract_attempts`` atomically, and ``extract_entity`` short-circuits
once the counter reaches 3.
"""

import asyncio
from unittest.mock import patch

import pytest
from django.test import TransactionTestCase

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.models import Denial


@pytest.mark.django_db
class ExtractAttemptsBudgetTests(TransactionTestCase):
    def _make_denial(self) -> Denial:
        return Denial.objects.create(
            hashed_email="hash:test",
            denial_text="Sample denial for testing extract_attempts.",
        )

    def test_failure_increments_extract_attempts(self):
        denial = self._make_denial()
        self.assertEqual(denial.extract_attempts, 0)

        # Mock the underlying ML call to raise.
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            side_effect=RuntimeError("LLM down"),
        ):
            asyncio.run(DenialCreatorHelper.extract_set_denial_and_diagnosis(denial.pk))

        denial.refresh_from_db()
        self.assertEqual(denial.extract_attempts, 1)
        # Flag must stay False so the *next* attempt is allowed.
        self.assertFalse(denial.extract_procedure_diagnosis_finished)

    def test_three_failures_then_extract_entity_short_circuits(self):
        denial = self._make_denial()

        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            side_effect=RuntimeError("LLM down"),
        ) as mock_extract:
            for _ in range(3):
                asyncio.run(
                    DenialCreatorHelper.extract_set_denial_and_diagnosis(denial.pk)
                )

        denial.refresh_from_db()
        self.assertEqual(denial.extract_attempts, 3)
        self.assertEqual(mock_extract.call_count, 3)

        # Now extract_entity should refuse to re-attempt extraction.
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            side_effect=RuntimeError("LLM still down"),
        ) as mock_again:

            async def _consume():
                async for _msg in DenialCreatorHelper.extract_entity(denial.pk):
                    pass

            asyncio.run(_consume())
        mock_again.assert_not_called()

        denial.refresh_from_db()
        # Counter must not advance once the budget is exhausted.
        self.assertEqual(denial.extract_attempts, 3)

    def test_extract_entity_runs_when_under_budget(self):
        """Sanity-check: extract_entity is willing to retry while counter < 3."""
        denial = self._make_denial()
        # Simulate one prior failure.
        Denial.objects.filter(pk=denial.pk).update(extract_attempts=1)

        # Stub all the optional extraction tasks to no-op fast; only the
        # diagnosis extraction matters for this assertion.
        with patch(
            "fighthealthinsurance.common_view_logic.appealGenerator."
            "get_procedure_and_diagnosis",
            side_effect=RuntimeError("transient"),
        ) as mock_extract:

            async def _consume():
                async for _msg in DenialCreatorHelper.extract_entity(denial.pk):
                    pass

            asyncio.run(_consume())

        # extract_entity ran => the required extract_set_denial_and_diagnosis
        # task was scheduled and our mock was called at least once.
        self.assertGreaterEqual(mock_extract.call_count, 1)
        denial.refresh_from_db()
        # Counter advanced by exactly one this call.
        self.assertEqual(denial.extract_attempts, 2)

"""Integration tests for appeal_journey_core against the REAL generator.

These exercise the database-backed core consuming the actual
``AppealsBackendHelper.generate_appeals`` iterator with only the model layer
(``appealGenerator``) stubbed -- the layer where the PR #963 review found the
failures: substituted re-served drafts fooling text-based progress counting,
empty streams reported as success, and speculative reserves counted as
delivered drafts. Denials are created with ``gen_attempts=3`` so the research
phase is skipped and the tests stay fast.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from fighthealthinsurance import appeal_journey_core
from fighthealthinsurance.models import Denial, ProposedAppeal
from fighthealthinsurance.generate_appeal import GeneratedAppeal


def _drafts(texts):
    """Wrap plain strings as the GeneratedAppeal drafts make_appeals emits."""
    return [
        GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
        for t in texts
    ]


def _make_denial(denial_id, gen_attempts=3):
    email = f"journey_core_{denial_id}@example.com"
    return Denial.objects.create(
        denial_id=denial_id,
        denial_text="Coverage for the requested MRI was denied as not medically necessary.",
        semi_sekret="sekret",
        hashed_email=Denial.get_hashed_email(email),
        gen_attempts=gen_attempts,
    )


class TestGenerateAndStoreAppeals(TestCase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_substituted_existing_drafts_cannot_satisfy_the_target(self, mock_gen):
        """Two existing placeholder drafts + one generated draft = exactly one
        new durable row. The generator re-serves existing drafts transformed
        by sub_in_appeals, so text-based progress counting saw them as new and
        stopped before generating; durable-ID counting must not."""
        denial = _make_denial(9101)
        for i in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"Dear [Insurance Company],\n\nExisting draft {i} for [Patient Name].",
            )
        mock_gen.make_appeals.return_value = iter(
            _drafts(["Dear Reviewer, a genuinely new appeal citing the denial."])
        )

        stored = appeal_journey_core.generate_and_store_appeals(denial)

        assert stored == 1
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == 3
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_empty_stream_raises_journey_incomplete(self, mock_gen):
        """A generator that produces nothing must surface as a retryable
        failure, never as a successful activity with zero drafts."""
        denial = _make_denial(9102)
        mock_gen.make_appeals.return_value = iter([])

        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_retry_after_partial_attempt_tops_up_to_target(self, mock_gen):
        """First attempt persists one draft and fails the postcondition; the
        retry generates the remainder and reaches exactly the target."""
        denial = _make_denial(9103)
        mock_gen.make_appeals.return_value = iter(_drafts(["Only draft, attempt one."]))
        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

        mock_gen.make_appeals.return_value = iter(
            _drafts(["Second draft, attempt two.", "Third draft, attempt two."])
        )
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored == 2
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == appeal_journey_core.TARGET_APPEALS
        )

    def test_speculative_reserves_do_not_satisfy_the_precheck(self):
        """Reserve precompute rows are not delivered drafts: with three
        speculative rows present the precheck still asks for generation.
        (The generation-side guarantee -- that the run then produces new
        durable drafts instead of re-serving the reserves -- arrives with
        the dedicated generation entry point in the tier-2 PR, where it is
        tested end to end.)"""
        denial = _make_denial(9104)
        for i in range(3):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"Reserve draft {i}.",
                speculative=True,
            )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK
        )


class TestLoadDenial(TestCase):
    def test_malformed_uuid_is_terminal_not_retry_fuel(self):
        """An invalid uuid must return None (terminal not_found), not raise a
        ValidationError into the precheck's unlimited retry."""
        assert appeal_journey_core.load_denial("h", "not-a-uuid") is None

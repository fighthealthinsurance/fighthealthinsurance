"""Integration tests for appeal_journey_core against the REAL generator.

These exercise the database-backed core consuming the actual
``AppealsBackendHelper.generate_appeals`` iterator with only the model layer
(``appealGenerator``) stubbed -- the layer where the PR #963 review found the
failures: substituted re-served drafts fooling text-based progress counting,
empty streams reported as success, and speculative reserves counted as
delivered drafts. Denials are created with ``gen_attempts=3`` so the research
phase is skipped and the tests stay fast.

KNOWN LOCAL-RIG QUIRK (Postgres-in-Docker on macOS): running the full
TestGenerateAndStoreAppeals class in one process can fail the retry test
with one draft short; every test passes in isolation and in any pair, so
the interaction is cumulative cross-test executor/connection state, not a
defect in the code under test. Linux CI runs the full suite as the gate.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.test import TransactionTestCase

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


class _JourneyTestBase(TransactionTestCase):
    """Shared setup: stub the generator's fire-and-forget context warmers
    (RAG, ML citations, payer policy). Left real, their background tasks
    outlive one test and interfere with the next test's saves under
    TransactionTestCase -- and they have no bearing on journey semantics."""

    def setUp(self):
        super().setUp()
        for target in (
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        ):
            patcher = patch(target, new_callable=AsyncMock, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        pmt_patcher = patch(
            "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
        )
        pmt = pmt_patcher.start()
        pmt.find_context_for_denial = AsyncMock(return_value=None)
        self.addCleanup(pmt_patcher.stop)


# TransactionTestCase, not TestCase: the generator's connection hygiene
# closes connections mid-flow, which kills TestCase's single wrapped
# test transaction; without the wrapper, closed connections just reopen
# (matching production behavior).
class TestGenerateAndStoreAppeals(_JourneyTestBase):
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
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    "Dear Reviewer, this appeal contests the denial on medical "
                    "necessity grounds: my physician documented the failure of "
                    "conservative treatment and the need for advanced imaging."
                ]
            )
        )
        with pytest.raises(appeal_journey_core.JourneyIncomplete):
            appeal_journey_core.generate_and_store_appeals(denial)
        assert ProposedAppeal.objects.filter(for_denial=denial).count() == 1

        # Two genuinely DIFFERENT letters: near-identical texts would be
        # (correctly) suppressed by the near-duplicate check and never count.
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    "To the appeals board: the plan's own coverage policy "
                    "states imaging is covered after failed conservative care, "
                    "which my records demonstrate across six documented visits.",
                    "I am requesting an independent review because the denial "
                    "letter mischaracterizes my treatment history and ignores "
                    "the specialist's written recommendation for this study.",
                ]
            )
        )
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored == 2
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == appeal_journey_core.TARGET_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_speculative_reserves_do_not_satisfy_the_target(self, mock_gen):
        """Reserve precompute rows are not delivered drafts: with three
        speculative rows present the journey still generates."""
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
        mock_gen.make_appeals.return_value = iter(
            _drafts(
                [
                    # Long enough to clear the generator's runt filter.
                    f"Dear Reviewer, appeal draft {i}: my physician documented "
                    "months of conservative treatment without improvement and "
                    "the requested imaging is medically necessary to plan care."
                    for i in ("one", "two", "three")
                ]
            )
        )
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored == 3


class TestLoadDenial(TransactionTestCase):
    def test_malformed_uuid_is_terminal_not_retry_fuel(self):
        """An invalid uuid must return None (terminal not_found), not raise a
        ValidationError into the precheck's unlimited retry."""
        assert appeal_journey_core.load_denial("h", "not-a-uuid") is None


class TestCandidateCounting(_JourneyTestBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_chosen_row_means_journey_complete(self, mock_gen):
        """A chosen row is the user's pick, not a draft: precheck must be
        terminal even with fewer than three candidate rows."""
        denial = _make_denial(9106)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The letter the user picked and finished with.",
            chosen=True,
        )
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_ALREADY_HAS_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_runt_rows_do_not_satisfy_the_target(self, mock_gen):
        """Legacy empty/runt rows are not deliverable drafts; three of them
        must not convince precheck the journey is done."""
        denial = _make_denial(9107)
        for i in range(3):
            ProposedAppeal.objects.create(for_denial=denial, appeal_text=f"x{i}")
        assert (
            appeal_journey_core.precheck_appeal_journey(denial)
            == appeal_journey_core.STATUS_OK
        )

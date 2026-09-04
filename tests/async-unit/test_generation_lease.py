"""The per-denial generation lease: single writer, fencing epoch, expiry.

Lease API tests run directly; the journey-vs-generator tests drive the REAL
generator with only the model layer stubbed (same harness as
test_appeal_journey_core), which is where the reviewers' concurrency
scenario actually lives.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TransactionTestCase

from fighthealthinsurance import appeal_journey_core, generation_lease
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.models import AppealGenerationLease, Denial, ProposedAppeal


def _drafts(texts):
    return [
        GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
        for t in texts
    ]


def _make_denial(denial_id):
    email = f"lease_{denial_id}@example.com"
    return Denial.objects.create(
        denial_id=denial_id,
        denial_text="Coverage for the requested MRI was denied as not medically necessary.",
        semi_sekret="sekret",
        hashed_email=Denial.get_hashed_email(email),
        gen_attempts=3,
    )


LETTERS = [
    "Dear Reviewer, appeal draft one: my physician documented months of "
    "conservative treatment without improvement before this imaging.",
    "To the appeals board: the plan's own coverage policy states imaging is "
    "covered after failed conservative care, which my records demonstrate.",
    "I am requesting an independent review because the denial letter "
    "mischaracterizes my treatment history and the specialist's recommendation.",
]


class TestLeaseApi(TransactionTestCase):
    def test_first_acquire_creates_the_row_at_epoch_one(self):
        denial = _make_denial(9201)
        lease = generation_lease.acquire(denial, "journey:a")
        assert lease.acquired and lease.epoch == 1
        assert AppealGenerationLease.objects.get(for_denial=denial).holder == "journey:a"

    def test_held_lease_refuses_a_second_acquire(self):
        denial = _make_denial(9202)
        generation_lease.acquire(denial, "journey:a")
        second = generation_lease.acquire(denial, "journey:b")
        assert not second.acquired and second.epoch == 1

    def test_steal_takes_a_held_lease_and_bumps_the_epoch(self):
        denial = _make_denial(9203)
        generation_lease.acquire(denial, "journey:a")
        stolen = generation_lease.acquire(denial, "interactive:x", steal=True)
        assert stolen.acquired and stolen.epoch == 2
        assert generation_lease.current_epoch(denial) == 2

    def test_expired_lease_is_free_and_the_epoch_increments(self):
        denial = _make_denial(9204)
        dead = generation_lease.acquire(denial, "journey:crashed", ttl_seconds=0)
        assert dead.acquired and dead.epoch == 1
        revived = generation_lease.acquire(denial, "journey:next")
        assert revived.acquired and revived.epoch == 2

    def test_extend_and_release_are_fenced_by_epoch(self):
        denial = _make_denial(9205)
        lease = generation_lease.acquire(denial, "journey:a")
        assert generation_lease.extend(denial, lease.epoch)
        assert not generation_lease.extend(denial, lease.epoch + 1)
        assert not generation_lease.release(denial, lease.epoch + 1)
        assert generation_lease.release(denial, lease.epoch)
        # Released -> free for the next acquirer without stealing.
        assert generation_lease.acquire(denial, "journey:b").acquired


class _JourneyTestBase(TransactionTestCase):
    """Stub the generator's fire-and-forget context warmers (see
    test_appeal_journey_core for why)."""

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


class TestLeaseGovernsGenerators(_JourneyTestBase):
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_two_concurrent_journeys_one_generates(self, mock_gen):
        """The reviewers' concurrency test: two generators, one denial ->
        exactly one calls the model layer, at most TARGET distinct drafts,
        the other is told the lease is held."""
        denial = _make_denial(9206)
        calls = {"n": 0}

        def counting_make_appeals(*args, **kwargs):
            calls["n"] += 1
            return iter(_drafts(LETTERS))

        mock_gen.make_appeals.side_effect = counting_make_appeals

        async def race():
            return await asyncio.gather(
                appeal_journey_core.agenerate_and_store_appeals(denial),
                appeal_journey_core.agenerate_and_store_appeals(denial),
                return_exceptions=True,
            )

        results = async_to_sync(race)()
        held = [r for r in results if isinstance(r, appeal_journey_core.LeaseHeld)]
        assert len(held) == 1, results
        assert calls["n"] == 1
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            <= appeal_journey_core.TARGET_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_interactive_steal_mid_run_stops_the_journey_quietly(self, mock_gen):
        """A human arrives while the journey generates: the interactive
        steal moves the epoch, the journey stops with what it has and does
        NOT raise JourneyIncomplete (no retry storm)."""
        denial = _make_denial(9207)

        def stealing_make_appeals(*args, **kwargs):
            yield _drafts([LETTERS[0]])[0]
            # The interactive flow takes over between drafts (runs on the
            # generator's worker thread, hence the sync API).
            generation_lease.acquire(denial, "interactive:user", steal=True)
            yield _drafts([LETTERS[1]])[0]
            yield _drafts([LETTERS[2]])[0]

        mock_gen.make_appeals.side_effect = stealing_make_appeals
        stored = appeal_journey_core.generate_and_store_appeals(denial)
        assert stored >= 1
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            < appeal_journey_core.TARGET_APPEALS
        )
        # The journey never released the interactive lease it did not own.
        assert generation_lease.current_epoch(denial) == 2

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_held_lease_is_a_retryable_refusal(self, mock_gen):
        denial = _make_denial(9208)
        generation_lease.acquire(denial, "interactive:user")
        mock_gen.make_appeals.return_value = iter(_drafts(LETTERS))
        with pytest.raises(appeal_journey_core.LeaseHeld):
            appeal_journey_core.generate_and_store_appeals(denial)
        assert not mock_gen.make_appeals.called

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_journey_releases_its_lease_on_completion(self, mock_gen):
        denial = _make_denial(9209)
        mock_gen.make_appeals.return_value = iter(_drafts(LETTERS))
        assert (
            appeal_journey_core.generate_and_store_appeals(denial)
            == appeal_journey_core.TARGET_APPEALS
        )
        # Released (expired now): a fresh acquire succeeds without stealing.
        assert generation_lease.acquire(denial, "journey:next").acquired

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
        assert (
            AppealGenerationLease.objects.get(for_denial=denial).holder == "journey:a"
        )

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

    def test_extend_never_revives_an_expired_lease(self):
        """A late extend from a holder whose lease already lapsed must be
        refused: the next acquirer may be seconds away, and reviving the old
        epoch would silently fence them out (review)."""
        denial = _make_denial(9210)
        dead = generation_lease.acquire(denial, "journey:slow", ttl_seconds=0)
        assert not generation_lease.extend(denial, dead.epoch)
        assert generation_lease.acquire(denial, "journey:next").acquired

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
        # Exactly the pre-steal draft persisted: every later insert is
        # fenced at the write boundary (save_appeal), not just skipped by
        # the per-frame check.
        assert stored == 1
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == 1
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


class _Clock:
    """A controllable lease clock: tests advance it between drafts instead
    of racing wall-clock sleeps against a shrunk TTL (review)."""

    def __init__(self):
        import datetime as _dt

        self._dt = _dt
        self.offset = _dt.timedelta(0)

    def now(self):
        from django.utils import timezone

        return timezone.now() + self.offset

    def advance(self, seconds):
        self.offset += self._dt.timedelta(seconds=seconds)


class TestInteractiveLease(_JourneyTestBase):
    """The public (interactive) path: steals at start, renews from the
    moment of acquisition and on every saved draft, and releases when the
    stream finishes. Driven by a controlled clock: every journey attempt
    below is provably past the UNEXTENDED expiry and inside the extended
    one."""

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_interactive_run_extends_on_each_save_and_releases_at_end(
        self, mock_gen
    ):
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9211)
        email = "lease_9211@example.com"
        clock = _Clock()
        ttl = generation_lease.DEFAULT_TTL_SECONDS  # 300s, unchanged
        journey_attempts: list = []

        def clocked_make_appeals(*args, **kwargs):
            # Acquired at T0 (expires T0+ttl). First draft saved at T0+200
            # extends to T0+500.
            clock.advance(200)
            yield _drafts([LETTERS[0]])[0]
            # T0+400: past the unextended expiry, inside the extended one.
            clock.advance(200)
            journey_attempts.append(
                generation_lease.acquire(denial, "journey:late").acquired
            )
            yield _drafts([LETTERS[1]])[0]  # saved at T0+400 -> T0+700
            clock.advance(200)  # T0+600: past T0+500, inside T0+700
            journey_attempts.append(
                generation_lease.acquire(denial, "journey:later").acquired
            )
            yield _drafts([LETTERS[2]])[0]

        mock_gen.make_appeals.side_effect = clocked_make_appeals

        async def drive():
            frames = 0
            async for chunk in AppealsBackendHelper.generate_appeals(
                {"denial_id": denial.denial_id, "email": email, "semi_sekret": "sekret"}
            ):
                if appeal_journey_core._appeal_text_from_chunk(chunk) is not None:
                    frames += 1
            return frames

        with patch.object(generation_lease, "_now", clock.now):
            frames = async_to_sync(drive)()
            assert frames >= 3
            # Both journey attempts fell in a window that only per-save
            # extension keeps closed.
            assert journey_attempts == [False, False]
            assert ttl == 300
            # Released at the end of the stream: a journey may now proceed.
            assert generation_lease.acquire(denial, "journey:after").acquired
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == appeal_journey_core.TARGET_APPEALS
        )

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_slow_first_draft_is_kept_alive_by_renewal_from_acquisition(
        self, mock_gen
    ):
        """make_appeals can run past the TTL before its first draft. The
        renewal task started at acquisition keeps the lease live across
        that window, so the eventual first draft persists and a journey
        arriving mid-wait is refused (review). Ordering is enforced by
        events, not sleeps: each clock step waits for one renewal."""
        import threading

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9214)
        email = "lease_9214@example.com"
        clock = _Clock()
        renewed = threading.Event()
        real_aextend = generation_lease.aextend

        async def observed_aextend(denial_, epoch, ttl_seconds=None):
            ok = await real_aextend(denial_, epoch, ttl_seconds)
            if ok:
                renewed.set()
            return ok

        journey_attempt: list = []

        def slow_first_draft(*args, **kwargs):
            # Three 200s steps = T0+600, twice the TTL, each step waiting
            # for the renewal task to have extended the lease first.
            for _ in range(3):
                renewed.clear()
                clock.advance(200)
                assert renewed.wait(timeout=15), "renewal task never ran"
            journey_attempt.append(
                generation_lease.acquire(denial, "journey:midwait").acquired
            )
            yield _drafts([LETTERS[0]])[0]

        mock_gen.make_appeals.side_effect = slow_first_draft

        async def drive():
            async for _chunk in AppealsBackendHelper.generate_appeals(
                {"denial_id": denial.denial_id, "email": email, "semi_sekret": "sekret"}
            ):
                pass

        with patch.object(generation_lease, "_now", clock.now), patch.object(
            generation_lease, "EXTEND_INTERVAL_SECONDS", 0.05
        ), patch.object(generation_lease, "aextend", observed_aextend):
            async_to_sync(drive)()
            assert journey_attempt == [False]
            assert generation_lease.acquire(denial, "journey:after").acquired
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == 1
        )


class TestInteractiveFencing(_JourneyTestBase):
    """Every writer is fenced, interactive included (review 9's barrier test
    produced six drafts from two concurrent interactive runs when only
    background runs were fenced)."""

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_two_concurrent_interactive_runs_persist_at_most_the_target(
        self, mock_gen
    ):
        import threading

        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9212)
        email = "lease_9212@example.com"
        letters_by_run = {
            0: [
                "Dear Reviewer, run A draft one: my physician documented months "
                "of conservative treatment before requesting this imaging.",
                "To the appeals board, run A draft two: the plan's coverage "
                "policy covers imaging after failed conservative care.",
                "I request an independent review, run A draft three: the denial "
                "mischaracterizes my treatment history and the specialist's note.",
            ],
            1: [
                "Dear Reviewer, run B draft one: the specialist recommended this "
                "study after documented conservative care failed to help.",
                "To the appeals board, run B draft two: my records show six "
                "documented visits before this imaging was requested.",
                "I request an independent review, run B draft three: the plan "
                "ignored the treating physician's written recommendation.",
            ],
        }
        # Both runs reach generation before either yields a draft, so both
        # have stolen the lease and only the LATER epoch may persist.
        barrier = threading.Barrier(2, timeout=30)
        calls = {"n": 0}
        lock = threading.Lock()

        def barrier_make_appeals(*args, **kwargs):
            with lock:
                run = calls["n"]
                calls["n"] += 1
            barrier.wait()
            for text in letters_by_run[run]:
                yield _drafts([text])[0]

        mock_gen.make_appeals.side_effect = barrier_make_appeals

        async def drive_one():
            async for _chunk in AppealsBackendHelper.generate_appeals(
                {"denial_id": denial.denial_id, "email": email, "semi_sekret": "sekret"}
            ):
                pass

        async def drive_both():
            await asyncio.gather(drive_one(), drive_one())

        async_to_sync(drive_both)()
        rows = list(
            ProposedAppeal.objects.filter(
                for_denial=denial, speculative=False
            ).values_list("appeal_text", flat=True)
        )
        assert len(rows) <= appeal_journey_core.TARGET_APPEALS, rows
        # Exactly one run persisted after the steal: every stored letter
        # comes from a single run's set.
        owners = {run for run, texts in letters_by_run.items() if set(rows) & set(texts)}
        assert len(owners) == 1, rows

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_superseded_interactive_run_stops_generating(self, mock_gen):
        """Another tab steals the lease mid-stream: the first run's later
        drafts are not persisted AND it stops pulling from the model
        (bounded invocations), rather than burning spend on drafts it can
        no longer store."""
        from fighthealthinsurance.common_view_logic import AppealsBackendHelper

        denial = _make_denial(9213)
        email = "lease_9213@example.com"
        pulled = {"n": 0}

        def stolen_mid_stream(*args, **kwargs):
            pulled["n"] += 1
            yield _drafts([LETTERS[0]])[0]
            # A second tab takes over (worker thread, hence the sync API).
            generation_lease.acquire(denial, "interactive:tab2", steal=True)
            for i in range(9):
                pulled["n"] += 1
                yield _drafts(
                    [
                        f"Dear Reviewer, post-steal draft {i}: my physician "
                        "documented conservative care before this imaging request."
                    ]
                )[0]

        mock_gen.make_appeals.side_effect = stolen_mid_stream

        async def drive():
            frames = 0
            async for chunk in AppealsBackendHelper.generate_appeals(
                {"denial_id": denial.denial_id, "email": email, "semi_sekret": "sekret"}
            ):
                if appeal_journey_core._appeal_text_from_chunk(chunk) is not None:
                    frames += 1
            return frames

        async_to_sync(drive)()
        assert (
            ProposedAppeal.objects.filter(for_denial=denial, speculative=False).count()
            == 1
        )
        # Stopped early: the producer was not drained to the end.
        assert pulled["n"] < 10, pulled
        # The stealing tab's lease was left untouched (not released by the loser).
        assert generation_lease.current_epoch(denial) == 2

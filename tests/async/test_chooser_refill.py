"""Tests for chooser back-fill (refill) thresholds and synthesized candidates.

Covers two behaviors added alongside synthesis tracking:

- ``_count_unscored_tasks`` / ``check_and_refill_task_pool`` back-fill the
  pool whenever fewer than ``CHOOSER_MIN_UNSCORED_TASKS`` fresh (unvoted)
  synthetic tasks remain for a type, independent of the overall READY pool.
- ``_maybe_add_synthesized_candidate`` adds a single synthesized candidate
  combining the per-model drafts so the chooser can compare synthesis to the
  individual models.
- ``_claim_generation`` lets one process at a time generate a task type, and
  both the refill's batches and page prefills take it.
"""

import asyncio
import contextlib
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from fighthealthinsurance.chooser_tasks import (
    CASE_VERSIONS,
    CHOOSER_BACKFILL_STOP_AFTER_FAILURES,
    CHOOSER_GENERATION_CLAIM_SECONDS,
    _claim_generation,
    _count_unscored_tasks,
    _generate_batch_tasks,
    _generate_appeal_candidates,
    _maybe_add_synthesized_candidate,
    _refill_reason,
    _synthesize_appeal_candidate,
    _synthesize_chat_candidate,
    backfill_reason,
    check_and_refill_task_pool,
    generation_underway,
    launch_backfill,
    prefill_if_needed,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
)


async def _make_task(
    task_type="appeal", status="READY", source="synthetic", case_version=None
):
    """A task built by the current case pipeline for its type, unless
    ``case_version`` says otherwise."""
    return await ChooserTask.objects.acreate(
        task_type=task_type,
        status=status,
        source=source,
        case_version=case_version or CASE_VERSIONS[task_type],
    )


async def _make_candidate(task, index=0, kind="appeal_letter", content="x"):
    return await ChooserCandidate.objects.acreate(
        task=task,
        candidate_index=index,
        kind=kind,
        model_name=f"model-{index}",
        content=content,
    )


def _after_the_window():
    """The clock once a generation claim taken now has lapsed."""
    return patch(
        "django.utils.timezone.now",
        return_value=timezone.now()
        + datetime.timedelta(seconds=CHOOSER_GENERATION_CLAIM_SECONDS + 1),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestGenerationClaim:
    """One process at a time generates a task type, fleet-wide: the refill
    actor's batches and every web worker's prefills take the same claim."""

    async def test_a_second_claim_while_one_is_held_fails(self):
        assert await _claim_generation("appeal") is not None

        assert await _claim_generation("appeal") is None

    async def test_a_claim_on_one_type_leaves_the_other_free(self):
        await _claim_generation("appeal")

        assert await _claim_generation("chat") is not None

    async def test_a_claim_after_the_window_succeeds(self):
        await _claim_generation("appeal")

        with _after_the_window():
            assert await _claim_generation("appeal") is not None

    async def test_a_released_claim_can_be_taken_at_once(self):
        claim = await _claim_generation("appeal")
        await claim.release()

        assert await _claim_generation("appeal") is not None

    async def test_a_held_claim_is_renewed_while_it_is_held(self):
        """A task slower than the window used to lose the claim mid-task:
        the arena then read nothing underway and stopped waiting."""
        claim = await _claim_generation("appeal")
        taken_at = claim.token
        renewals = []
        enough = asyncio.Event()
        renew = claim.renew

        async def counting_renew():
            renewed = await renew()
            renewals.append(renewed)
            if len(renewals) >= 3:
                enough.set()
            return renewed

        with patch(
            "fighthealthinsurance.chooser_tasks._CLAIM_KEEPALIVE_SECONDS", 0.02
        ), patch.object(claim, "renew", counting_renew):
            async with claim:
                # Several renewals, not one: a keeper that renewed once and
                # stopped would pass a single check. The timeout only bounds
                # a failing run.
                await asyncio.wait_for(enough.wait(), timeout=10)
                assert all(renewals)
                assert claim.token > taken_at
                assert await _claim_generation("appeal") is None

        # And given back on the way out.
        assert await _claim_generation("appeal") is not None

    async def test_the_claim_is_released_when_the_exit_is_cancelled(self):
        """Cancelled while it waits for the keep-alive to stop, the exit
        still gives the claim back, or it held the type off for its window."""
        claim = await _claim_generation("appeal")
        await claim.__aenter__()
        started_keeper = claim._keeper
        # A keep-alive that never finishes, so the exit is waiting on it
        # when it is cancelled.
        never = asyncio.create_task(asyncio.Event().wait())
        claim._keeper = never

        exiting = asyncio.create_task(claim.__aexit__(None, None, None))
        await asyncio.sleep(0.05)
        exiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exiting
        never.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await never
        # The real keep-alive saw the stop and ended on its own.
        await asyncio.wait_for(started_keeper, timeout=5)

        assert await _claim_generation("appeal") is not None

    async def test_the_keep_alive_and_a_batch_renewal_do_not_race(self):
        """Both renew the same claim; unserialised, the second matched the
        token the first had just replaced and read the claim as taken."""
        claim = await _claim_generation("appeal")

        async with claim:
            assert await asyncio.gather(claim.renew(), claim.renew()) == [
                True,
                True,
            ]

    async def test_a_stale_owner_cannot_release_a_newer_claim(self):
        stale = await _claim_generation("appeal")
        with _after_the_window():
            # The stale owner's claim lapsed and another process took it.
            assert await _claim_generation("appeal") is not None
            await stale.release()

            assert await _claim_generation("appeal") is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestCountUnscoredTasks:
    """_count_unscored_tasks should only count fresh, READY, synthetic tasks."""

    async def test_counts_only_ready_synthetic_unvoted_tasks(self):
        # Counts: READY synthetic appeal with no votes.
        await _make_task()
        await _make_task()

        # Excluded: READY synthetic appeal that already has a vote.
        voted = await _make_task()
        cand = await _make_candidate(voted)
        await ChooserVote.objects.acreate(
            task=voted,
            chosen_candidate=cand,
            presented_candidate_ids=[cand.id],
            session_key="sess-voted",
        )

        # Excluded: not READY.
        await _make_task(status="QUEUED")
        # Excluded: not synthetic.
        await _make_task(source="real")
        # Excluded: different task type.
        await _make_task(task_type="chat")
        # Excluded: built by an older case pipeline (votable, served later).
        await _make_task(case_version=1)

        assert await _count_unscored_tasks("appeal") == 2

    async def test_chat_counted_separately(self):
        await _make_task(task_type="chat")
        await _make_task(task_type="appeal")

        assert await _count_unscored_tasks("chat") == 1
        assert await _count_unscored_tasks("appeal") == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRefillThreshold:
    """check_and_refill_task_pool back-fills based on the unscored threshold."""

    async def test_refill_triggers_when_unscored_below_threshold(self):
        # Empty DB: every type is below threshold and should be back-filled.
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(),
        ) as mock_batch:
            await check_and_refill_task_pool()

        # Once for "appeal" and once for "chat", awaited in place: the batch
        # used to be handed to a background thread with the guard already
        # released, so batches could overlap.
        assert mock_batch.await_count == 2

    async def test_no_refill_when_enough_unscored_and_ready(self):
        # One fresh READY synthetic task per type, with thresholds lowered to 1
        # so the pool is considered healthy and no generation is triggered.
        await _make_task(task_type="appeal")
        await _make_task(task_type="chat")

        with patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_READY_TASKS", 1
        ), patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_UNSCORED_TASKS", 1
        ), patch(
            "fighthealthinsurance.chooser_tasks._comparable_backends",
            return_value=[],
        ), patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(),
        ) as mock_batch:
            await check_and_refill_task_pool()

        mock_batch.assert_not_awaited()

    async def test_refill_triggers_on_low_unscored_when_ready_pool_healthy(self):
        # Each type has a READY task that has already been voted on, so the
        # READY pool is healthy (>=MIN_READY) but zero tasks are unscored.
        # Isolates the unscored branch: only it can trigger the back-fill.
        for tt in ("appeal", "chat"):
            task = await _make_task(task_type=tt)
            cand = await _make_candidate(task)
            await ChooserVote.objects.acreate(
                task=task,
                chosen_candidate=cand,
                presented_candidate_ids=[cand.id],
                session_key=f"sess-{tt}",
            )

        with patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_READY_TASKS", 1
        ), patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_UNSCORED_TASKS", 5
        ), patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(),
        ) as mock_batch:
            await check_and_refill_task_pool()

        # ready=1>=1 (healthy) for both; unscored=0<5 -> both refill solely via
        # the unscored branch. Dropping that clause would make this 0.
        assert mock_batch.await_count == 2

    async def test_refill_triggers_on_low_ready_pool_when_unscored_healthy(self):
        # Two fresh (unvoted) READY tasks per type: unscored=2 is healthy
        # (>=MIN_UNSCORED) but the READY pool (2) is below MIN_READY.
        # Isolates the ready-pool branch: only it can trigger the back-fill.
        for tt in ("appeal", "chat"):
            await _make_task(task_type=tt)
            await _make_task(task_type=tt)

        with patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_READY_TASKS", 5
        ), patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_UNSCORED_TASKS", 1
        ), patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(),
        ) as mock_batch:
            await check_and_refill_task_pool()

        # ready=2<5 -> both refill; unscored=2>=1 is healthy, so the trigger is
        # solely the ready-pool branch. Dropping that clause would make this 0.
        assert mock_batch.await_count == 2


class _Backend:
    """A router backend as the coverage check sees it: a stamped name, an
    ``external`` flag and the configured model id."""

    def __init__(self, name, external, model=None):
        self.name = name
        self.external = external
        self.model = model


async def _fresh_task_with(task_type, *model_names):
    task = await _make_task(task_type=task_type)
    for index, name in enumerate(model_names):
        await ChooserCandidate.objects.acreate(
            task=task,
            candidate_index=index,
            kind="appeal_letter" if task_type == "appeal" else "chat_response",
            model_name=name,
            content="x" * 120,
        )
    return task


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestCoverageRefill:
    """A pool that is full but never compares a configured external backend
    is refilled. READY is monotonic, so without this trigger a provider
    configured after the pool was bootstrapped never got a task."""

    @contextlib.contextmanager
    def _thresholds(self, backends=None, max_unscored=10):
        if backends is None:
            backends = [
                _Backend("fhi-2025-may", external=False),
                _Backend("azure-openai/gpt-5.5", external=True),
            ]
        with contextlib.ExitStack() as stack:
            for patcher in (
                patch("fighthealthinsurance.chooser_tasks.CHOOSER_MIN_READY_TASKS", 1),
                patch(
                    "fighthealthinsurance.chooser_tasks.CHOOSER_MIN_UNSCORED_TASKS", 1
                ),
                patch(
                    "fighthealthinsurance.chooser_tasks.CHOOSER_MAX_UNSCORED_TASKS",
                    max_unscored,
                ),
                patch(
                    "fighthealthinsurance.chooser_tasks._comparable_backends",
                    return_value=backends,
                ),
            ):
                stack.enter_context(patcher)
            yield

    async def test_an_external_backend_with_no_fresh_tasks_triggers_a_refill(self):
        # READY and unscored are both healthy (one task each, thresholds 1),
        # but the only fresh task compares the internal backend alone.
        await _fresh_task_with("appeal", "fhi-2025-may")
        await _fresh_task_with("chat", "fhi-2025-may")

        with self._thresholds():
            reason = await _refill_reason("appeal")
            with patch(
                "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
                new=AsyncMock(),
            ) as mock_batch:
                await check_and_refill_task_pool()

        assert reason is not None and "azure-openai/gpt-5.5" in reason
        assert mock_batch.await_count == 2

    async def test_a_covered_external_backend_needs_no_refill(self):
        await _fresh_task_with("appeal", "fhi-2025-may", "azure-openai/gpt-5.5")
        await _fresh_task_with("chat", "fhi-2025-may", "azure-openai/gpt-5.5")

        with self._thresholds():
            assert await _refill_reason("appeal") is None
            with patch(
                "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
                new=AsyncMock(),
            ) as mock_batch:
                await check_and_refill_task_pool()

        mock_batch.assert_not_awaited()

    async def test_a_voted_task_does_not_count_as_coverage(self):
        task = await _fresh_task_with("appeal", "fhi-2025-may", "azure-openai/gpt-5.5")
        cand = await ChooserCandidate.objects.aget(task=task, candidate_index=1)
        await ChooserVote.objects.acreate(
            task=task,
            chosen_candidate=cand,
            presented_candidate_ids=[cand.id],
            session_key="sess-cov",
        )
        # One fresh task keeps the unscored count healthy; it holds no external.
        await _fresh_task_with("appeal", "fhi-2025-may")

        with self._thresholds():
            reason = await _refill_reason("appeal")

        assert reason is not None and "azure-openai/gpt-5.5" in reason

    async def test_coverage_names_a_backend_as_its_candidates_are_stored(self):
        # Candidates are stored under canonical_model_name, which falls back
        # to the configured model id for a backend with no stamped name; the
        # coverage check keyed on str(backend) instead and never saw them.
        await _fresh_task_with("appeal", "fhi-2025-may", "gpt-5.5")

        with self._thresholds(
            backends=[
                _Backend("fhi-2025-may", external=False),
                _Backend(None, external=True, model="gpt-5.5"),
            ]
        ):
            reason = await _refill_reason("appeal")

        assert reason is None

    async def test_coverage_refills_stop_at_the_unscored_ceiling(self):
        # A backend that never yields a usable candidate must not justify a
        # batch every tick forever: past the ceiling the trigger is off.
        await _fresh_task_with("appeal", "fhi-2025-may")

        with self._thresholds(max_unscored=1):
            reason = await _refill_reason("appeal")

        assert reason is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRefillTakesTheGenerationClaim:
    """The refill was kept apart only from itself, by a lock local to its
    process, so a page prefill could generate a type alongside its batch.
    Each type's batch now runs under that type's generation claim. (The
    pool is empty, so both types need a batch.)"""

    async def _refill(self, **batch):
        """(the patched batch, the refill's outcome)"""
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(**batch),
        ) as mock_batch:
            outcome = await check_and_refill_task_pool()
        return mock_batch, outcome

    async def test_a_type_whose_claim_is_held_is_skipped(self):
        await _claim_generation("appeal")

        mock_batch, _ = await self._refill(return_value=1)

        assert [call.args[0] for call in mock_batch.await_args_list] == ["chat"]

    async def test_a_skipped_type_is_not_a_failed_refill(self):
        # Another process is refilling it, so the actor must not count the
        # tick against its health.
        await _claim_generation("appeal")
        await _claim_generation("chat")

        _, outcome = await self._refill(return_value=0)

        assert outcome is True

    async def test_the_claim_is_held_for_the_batch(self):
        seen = []

        async def batch(task_type, size, claim):
            seen.append(await _claim_generation(task_type))
            return 1

        await self._refill(side_effect=batch)

        assert seen == [None, None]

    async def test_the_claim_is_released_after_the_batch(self):
        await self._refill(return_value=1)

        assert await _claim_generation("appeal") is not None

    async def test_the_claim_is_released_when_the_batch_raises(self):
        with pytest.raises(RuntimeError):
            await self._refill(side_effect=RuntimeError("boom"))

        assert await _claim_generation("appeal") is not None

    async def test_a_claim_that_cannot_be_checked_still_refills(self):
        # The actor is the pool's main supplier, and prefills fail closed on
        # the same error, so it fails open.
        with patch(
            "fighthealthinsurance.chooser_tasks._claim_generation",
            new=AsyncMock(side_effect=RuntimeError("no table")),
        ):
            mock_batch, _ = await self._refill(return_value=1)

        assert mock_batch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestABatchRenewsItsClaim:
    """A batch is a round of model calls per task and can outlast the
    claim's window; a lapsed claim would let another process generate the
    type alongside it."""

    async def test_a_batch_longer_than_the_window_keeps_its_claim(self):
        claim = await _claim_generation("appeal")

        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task",
            new=AsyncMock(return_value=True),
        ), _after_the_window():
            await _generate_batch_tasks("appeal", 2, claim)

            assert await _claim_generation("appeal") is None

    async def test_a_batch_whose_claim_was_taken_stops(self):
        claim = await _claim_generation("appeal")

        async def task_then_claim_taken(task_type):
            # Meanwhile the claim lapsed and another process took it.
            with _after_the_window():
                assert await _claim_generation(task_type) is not None
            return True

        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task",
            new=AsyncMock(side_effect=task_then_claim_taken),
        ) as single:
            await _generate_batch_tasks("appeal", 3, claim)

        assert single.await_count == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRefillOutcome:
    """check_and_refill_task_pool says whether a needed refill produced a
    usable task, which the refill actor's health check counts on: generation
    errors are caught per task, so a refill whose models all failed still
    returns normally."""

    async def test_a_needed_refill_that_produced_nothing_reports_failure(self):
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(return_value=0),
        ):
            assert await check_and_refill_task_pool() is False

    async def test_a_needed_refill_that_produced_a_task_reports_success(self):
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(return_value=2),
        ):
            assert await check_and_refill_task_pool() is True

    async def test_one_type_failing_while_the_other_refills_reports_success(self):
        """Counted as a failed tick, it got the actor replaced, which cannot
        fix the failing type's backends and could kill a batch mid-run."""
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=AsyncMock(side_effect=[0, 3]),
        ):
            assert await check_and_refill_task_pool() is True

    async def test_a_batch_counts_the_tasks_that_came_out_ready(self):
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task",
            new=AsyncMock(side_effect=[True, False, True]),
        ):
            assert await _generate_batch_tasks("appeal", 3) == 2


async def _run_in_place(work):
    """fire_and_forget_in_new_threadpool without the thread: the work runs to
    its end before the prefill goes on, and what it raises is dropped (the
    real one logs it)."""
    try:
        await work
    except Exception:
        pass


async def _prefilled_types(exhausted=None, generate=None):
    """The task types one prefill_if_needed(min_ready=1) pass generates."""
    with patch(
        "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
        new=_run_in_place,
    ), patch(
        "fighthealthinsurance.chooser_tasks._generate_single_task",
        new=generate or AsyncMock(return_value=True),
    ) as single:
        await prefill_if_needed(min_ready=1, exhausted=exhausted)
    return [call.args[0] for call in single.await_args_list]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPrefillTakesTheGenerationClaim:
    """The prefill throttle is per process, so each web worker could start a
    generation while the pool was empty, and looking for a QUEUED task first
    still let two workers that looked at once both start one. A prefill now
    generates only under the type's generation claim."""

    async def test_a_type_whose_claim_is_held_is_not_generated(self):
        await _claim_generation("appeal")

        assert await _prefilled_types() == ["chat"]

    async def test_a_claim_left_behind_long_ago_holds_nothing_off(self):
        await _claim_generation("appeal")

        with _after_the_window():
            assert await _prefilled_types() == ["appeal", "chat"]

    async def test_the_claim_is_held_while_the_task_is_generated(self):
        seen = []

        async def generate(task_type):
            seen.append(await _claim_generation(task_type))
            return True

        await _prefilled_types(generate=AsyncMock(side_effect=generate))

        assert seen == [None, None]

    async def test_the_claim_is_released_when_the_task_is_done(self):
        await _prefilled_types()

        assert await _claim_generation("appeal") is not None

    async def test_the_claim_is_released_when_generation_raises(self):
        await _prefilled_types(
            generate=AsyncMock(side_effect=RuntimeError("backend down"))
        )

        assert await _claim_generation("appeal") is not None

    async def test_the_claim_is_released_when_no_thread_could_start(self):
        with patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(side_effect=RuntimeError("can't start new thread")),
        ), patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task",
            new=AsyncMock(return_value=True),
        ):
            with pytest.raises(RuntimeError):
                await prefill_if_needed(min_ready=1)

        assert await _claim_generation("appeal") is not None

    async def test_a_claim_that_cannot_be_checked_generates_nothing(self):
        # Fails closed: the refill actor still supplies the pool.
        with patch(
            "fighthealthinsurance.chooser_tasks._claim_generation",
            new=AsyncMock(side_effect=RuntimeError("no table")),
        ):
            assert await _prefilled_types() == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPrefillForATypeASessionUsedUp:
    """A session that has answered every READY task of a type gets a 404
    from next-task. The pool is not short, so a prefill that only counted it
    never made the session another task."""

    async def _stocked_pool(self):
        await _make_task("appeal")
        await _make_task("chat")

    async def test_a_used_up_type_is_generated_though_ready_tasks_remain(self):
        await self._stocked_pool()

        assert await _prefilled_types(exhausted="appeal") == ["appeal"]

    async def test_a_stocked_pool_generates_nothing_otherwise(self):
        await self._stocked_pool()

        assert await _prefilled_types() == []

    async def test_a_used_up_type_still_waits_for_a_generation_underway(self):
        await self._stocked_pool()
        await _claim_generation("appeal")

        assert await _prefilled_types(exhausted="appeal") == []


@pytest.mark.asyncio
async def test_appeal_synthesis_for_the_chooser_may_use_hosted_models():
    """The chooser's drafts come from every backend, hosted ones included,
    so their merge may fall back to a hosted model too."""
    with patch(
        "fighthealthinsurance.generate_appeal.AppealGenerator.synthesize_appeals",
        new=AsyncMock(return_value="A merged letter. " * 20),
    ) as synthesize:
        result = await _synthesize_appeal_candidate(
            {"procedure": "MRI", "diagnosis": "back pain"}, ["draft one", "draft two"]
        )

    assert result == "A merged letter. " * 20
    assert synthesize.call_args.kwargs["use_external"] is True
    assert synthesize.call_args.kwargs["purpose"] == "other"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestSynthesizedCandidate:
    """_maybe_add_synthesized_candidate appends a synthesized candidate."""

    async def test_adds_synthesized_appeal_candidate(self):
        task = await _make_task()
        task.context_json = {"procedure": "MRI", "diagnosis": "back pain"}
        task.num_candidates_generated = 2
        await task.asave()
        await _make_candidate(task, 0, content="First draft appeal letter body.")
        await _make_candidate(task, 1, content="Second draft appeal letter body.")

        synth_text = "Synthesized appeal letter " + "z" * 200
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=synth_text),
        ):
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        assert await ChooserCandidate.objects.filter(task=task).acount() == 3
        synth = await ChooserCandidate.objects.aget(task=task, synthesized=True)
        assert synth.candidate_index == 2
        assert synth.model_name == "synthesized"
        assert synth.kind == "appeal_letter"
        assert synth.content == synth_text
        await task.arefresh_from_db()
        assert task.num_candidates_generated == 3
        # The expectation never sits below what was generated, so the two
        # counters stay comparable on synthesized tasks.
        assert task.num_candidates_expected >= task.num_candidates_generated

    async def test_skips_synthesis_with_single_candidate(self):
        task = await _make_task()
        await _make_candidate(task, 0, content="Only one draft.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="should not be used"),
        ) as mock_synth:
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        mock_synth.assert_not_called()
        assert await ChooserCandidate.objects.filter(task=task).acount() == 1

    async def test_skips_verbatim_duplicate(self):
        task = await _make_task()
        # Drafts long enough to clear the min-length bar, so this exercises the
        # verbatim-duplicate skip rather than the too-short skip.
        draft_one = "Draft one appeal letter body. " * 5
        await _make_candidate(task, 0, content=draft_one)
        await _make_candidate(task, 1, content="Draft two appeal letter body. " * 5)

        # Synthesis returns a verbatim copy of an existing draft.
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=draft_one),
        ):
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        assert not await ChooserCandidate.objects.filter(
            task=task, synthesized=True
        ).aexists()
        assert await ChooserCandidate.objects.filter(task=task).acount() == 2

    async def test_skips_too_short_synthesis(self):
        task = await _make_task()
        await _make_candidate(task, 0, content="First appeal draft body text here.")
        await _make_candidate(task, 1, content="Second appeal draft body text here.")

        # A synthesized appeal under the 100-char base-candidate bar is rejected.
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="Too short."),
        ):
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        assert not await ChooserCandidate.objects.filter(
            task=task, synthesized=True
        ).aexists()
        assert await ChooserCandidate.objects.filter(task=task).acount() == 2

    async def test_skips_synthesis_when_drafts_identical(self):
        task = await _make_task()
        await _make_candidate(task, 0, content="Identical appeal draft body.")
        await _make_candidate(task, 1, content="Identical appeal draft body.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="x" * 200),
        ) as mock_synth:
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        # After dedupe there is only one distinct draft -> synthesis skipped.
        mock_synth.assert_not_called()
        assert await ChooserCandidate.objects.filter(task=task).acount() == 2

    async def test_disabled_when_synthesis_returns_none(self):
        task = await _make_task()
        await _make_candidate(task, 0, content="Draft one body text.")
        await _make_candidate(task, 1, content="Draft two body text.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=None),
        ):
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        assert await ChooserCandidate.objects.filter(task=task).acount() == 2

    async def test_respects_include_synthesis_flag(self):
        task = await _make_task()
        await _make_candidate(task, 0, content="Draft one body text.")
        await _make_candidate(task, 1, content="Draft two body text.")

        with patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_INCLUDE_SYNTHESIS", False
        ), patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="x" * 200),
        ) as mock_synth:
            await _maybe_add_synthesized_candidate(task, "appeal_letter")

        mock_synth.assert_not_called()
        assert await ChooserCandidate.objects.filter(task=task).acount() == 2

    async def test_adds_synthesized_chat_candidate(self):
        task = await _make_task(task_type="chat")
        task.context_json = {"prompt": "How do I appeal?", "history": []}
        await task.asave()
        await _make_candidate(
            task, 0, kind="chat_response", content="First chat response."
        )
        await _make_candidate(
            task, 1, kind="chat_response", content="Second chat response."
        )

        # Must clear the chat min-length bar (>50 chars).
        synth_text = (
            "Best combined chat response that pulls together the clearest guidance."
        )
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_chat_candidate",
            new=AsyncMock(return_value=synth_text),
        ):
            await _maybe_add_synthesized_candidate(task, "chat_response")

        synth = await ChooserCandidate.objects.aget(task=task, synthesized=True)
        assert synth.kind == "chat_response"
        assert synth.candidate_index == 2
        assert synth.content == synth_text


@pytest.mark.asyncio
class TestSynthesizeHelpers:
    """Direct tests for the synthesis helpers (these touch ML, not the DB)."""

    SYNTHESIS_BACKENDS = (
        "fighthealthinsurance.chooser_tasks.ml_router.synthesis_backends"
    )

    async def test_chat_synthesis_builds_prompt_and_returns_result(self):
        model = MagicMock()
        model._infer_no_context = AsyncMock(
            return_value="A synthesized chat answer comfortably longer than fifty chars."
        )
        context = {
            "prompt": "What documents do I need?",
            "history": [{"role": "user", "content": "My MRI was denied."}],
        }
        responses = ["You need your denial letter.", "Bring your EOB and notes."]
        with patch(
            self.SYNTHESIS_BACKENDS, return_value=([model, MagicMock()], [])
        ) as backends:
            result = await _synthesize_chat_candidate(context, responses)

        # The drafts came from hosted models too, so a hosted model may merge
        # them, as for the appeal candidate.
        assert backends.call_args.kwargs == {"use_external": True}
        assert (
            result == "A synthesized chat answer comfortably longer than fifty chars."
        )
        # The prompt fed to the model includes the conversation and every
        # candidate response to be combined, at the synthesis temperature.
        kwargs = model._infer_no_context.call_args.kwargs
        assert "What documents do I need?" in kwargs["prompt"]
        assert "My MRI was denied." in kwargs["prompt"]
        for r in responses:
            assert r in kwargs["prompt"]
        assert kwargs["temperature"] == 0.3

    async def test_chat_synthesis_uses_a_hosted_model_when_none_of_ours_can(self):
        hosted = MagicMock()
        hosted._infer_no_context = AsyncMock(
            return_value="A hosted model's merged answer, comfortably over fifty chars."
        )
        with patch(self.SYNTHESIS_BACKENDS, return_value=([], [hosted])):
            result = await _synthesize_chat_candidate(
                {"prompt": "hi", "history": []}, ["a", "b"]
            )

        assert result.startswith("A hosted model's merged answer")
        hosted._infer_no_context.assert_awaited_once()

    async def test_chat_synthesis_returns_none_without_a_model_that_can_merge(self):
        with patch(self.SYNTHESIS_BACKENDS, return_value=([], [])):
            result = await _synthesize_chat_candidate(
                {"prompt": "hi", "history": []}, ["a", "b"]
            )
        assert result is None

    async def test_chat_synthesis_rejects_too_short_model_output(self):
        model = MagicMock()
        model._infer_no_context = AsyncMock(return_value="short")
        with patch(self.SYNTHESIS_BACKENDS, return_value=([model], [])):
            result = await _synthesize_chat_candidate(
                {"prompt": "hi", "history": []}, ["a", "b"]
            )
        assert result is None

    async def test_appeal_synthesis_maps_context_fields(self):
        generator = MagicMock()
        generator.synthesize_appeals = AsyncMock(return_value="synthesized appeal text")
        context = {
            "denial_text_preview": "Denied as not medically necessary.",
            "procedure": "MRI lumbar spine",
            "diagnosis": "chronic low back pain",
            "insurance_company": "Acme Health",
        }
        with patch(
            "fighthealthinsurance.generate_appeal.AppealGenerator",
            return_value=generator,
        ):
            result = await _synthesize_appeal_candidate(context, ["draft a", "draft b"])

        assert result == "synthesized appeal text"
        kwargs = generator.synthesize_appeals.call_args.kwargs
        assert kwargs["appeal_texts"] == ["draft a", "draft b"]
        assert kwargs["denial_text"] == "Denied as not medically necessary."
        assert kwargs["procedure"] == "MRI lumbar spine"
        assert kwargs["diagnosis"] == "chronic low back pain"

    async def test_appeal_synthesis_swallows_errors(self):
        generator = MagicMock()
        generator.synthesize_appeals = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "fighthealthinsurance.generate_appeal.AppealGenerator",
            return_value=generator,
        ):
            result = await _synthesize_appeal_candidate({}, ["a", "b"])
        assert result is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRetryDoesNotAskAFailedModelAgain:
    """A backend that failed in the first pass used to be asked again by the
    retry pass, first, on every task: for a dead external that was another
    paid call and WARNING per task, and nothing to show for it."""

    async def _generate(self):
        """Candidates for a task where model-a writes the scenario and a
        draft, and model-b raises on its first draft (later ones would
        succeed). Returns (model-b, the candidates' model names in order)."""
        scenario = (
            "Procedure: MRI of lumbar spine\nDiagnosis: chronic lower back pain\n"
            "Insurance Company: Fictional Mutual\nDenial Reason: not medically "
            "necessary per the plan's clinical policy.\n"
        )
        letter = "Dear Reviewer, " + "this care is medically necessary. " * 10

        model_a = MagicMock()
        model_a.name = "model-a"
        model_a.external = False
        # First call writes the synthetic scenario, the rest are drafts.
        model_a._infer_no_context = AsyncMock(side_effect=[scenario] + [letter] * 10)
        model_b = MagicMock()
        model_b.name = "model-b"
        model_b.external = False
        model_b._infer_no_context = AsyncMock(
            side_effect=[Exception("flaky"), letter, letter, letter, letter]
        )
        task = await _make_task()

        with patch("fighthealthinsurance.chooser_tasks.ml_router") as router, patch(
            "fighthealthinsurance.chooser_tasks._maybe_add_synthesized_candidate",
            AsyncMock(),
        ):
            router.generate_text_backends.return_value = [model_a, model_b]
            await _generate_appeal_candidates(task)

        names = [
            name
            async for name in ChooserCandidate.objects.filter(task=task)
            .order_by("candidate_index")
            .values_list("model_name", flat=True)
        ]
        return model_b, names

    async def test_a_model_that_raised_is_not_asked_again(self):
        model_b, _ = await self._generate()

        assert model_b._infer_no_context.await_count == 1

    async def test_the_model_that_answered_is_resampled_to_reach_ready(self):
        # With no unused backend left, one task short of the two candidates
        # READY needs gets a second draft from the model that answered.
        _, names = await self._generate()

        assert names == ["model-a", "model-a"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestGenerationUnderway:
    """generation_underway reads a type's claim without taking it: the
    next-task endpoint asks so it can tell a client to wait for the task
    being written rather than report an empty pool."""

    async def test_a_held_claim_is_underway_for_its_type_only(self):
        assert await _claim_generation("appeal") is not None

        assert await sync_to_async(generation_underway)("appeal") is True
        assert await sync_to_async(generation_underway)("chat") is False

    async def test_a_released_claim_is_not(self):
        claim = await _claim_generation("appeal")
        assert claim is not None
        await claim.release()

        assert await sync_to_async(generation_underway)("appeal") is False

    async def test_a_lapsed_claim_is_not(self):
        assert await _claim_generation("appeal") is not None

        with _after_the_window():
            assert await sync_to_async(generation_underway)("appeal") is False

    async def test_reading_takes_nothing(self):
        assert await sync_to_async(generation_underway)("appeal") is False

        assert await _claim_generation("appeal") is not None


def _generating(ready=True):
    """Every task the backfill writes comes out READY (or not)."""
    return patch(
        "fighthealthinsurance.chooser_tasks._generate_single_task",
        new=AsyncMock(return_value=ready),
    )


@contextlib.contextmanager
def _waiting_for_the_claim(seconds=5.0):
    """A backfill that finds the claim held waits up to ``seconds`` for it,
    asking again without sleeping (tests give up at once by default)."""
    with patch(
        "fighthealthinsurance.chooser_tasks.CHOOSER_LAUNCH_BACKFILL_CLAIM_WAIT_SECONDS",
        seconds,
    ), patch("fighthealthinsurance.chooser_tasks._BACKFILL_CLAIM_POLL_SECONDS", 0):
        yield


def _held_until_asked(held, written=0):
    """A _claim_generation for a backfill to wait on: while ``held`` is
    taken, the first ask that finds it so lets the other holder write
    ``written`` tasks and give the claim back, as a refill batch does."""

    async def claim(task_type):
        taken = await _claim_generation(task_type)
        if taken is None:
            for _ in range(written):
                await _make_task(task_type)
            await held.release()
        return taken

    return patch("fighthealthinsurance.chooser_tasks._claim_generation", new=claim)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestLaunchBackfill:
    """launch_backfill writes a big batch of appeal tasks at launch, once the
    voters have judged every appeal task the current case pipeline made at
    least twice; the refill's ten-at-a-time keeps the pool at its minimums
    the rest of the time."""

    async def _judged(self, task, votes):
        candidate = await _make_candidate(task, 0)
        for i in range(votes):
            await ChooserVote.objects.acreate(
                task=task,
                chosen_candidate=candidate,
                presented_candidate_ids=[candidate.id],
                session_key=f"session-{task.id}-{i}",
            )
        return task

    async def test_due_once_every_current_task_has_two_votes(self):
        await self._judged(await _make_task(), 2)
        await self._judged(await _make_task(), 3)

        assert await backfill_reason("appeal") is not None

    async def test_not_due_while_a_current_task_has_fewer(self):
        await self._judged(await _make_task(), 2)
        await self._judged(await _make_task(), 1)

        assert await backfill_reason("appeal") is None

    async def test_an_older_version_task_does_not_hold_it_off(self):
        await self._judged(await _make_task(), 2)
        # Served after the current ones; not what the backfill is for.
        await self._judged(await _make_task(case_version=1), 0)

        assert await backfill_reason("appeal") is not None

    async def test_due_with_no_current_task_at_all(self):
        assert await backfill_reason("appeal") is not None

    async def test_the_vote_bar_can_be_raised(self):
        await self._judged(await _make_task(), 2)

        assert await backfill_reason("appeal", min_votes=3) is None

    async def test_writes_the_requested_tasks(self):
        with _generating() as generate:
            result = await launch_backfill(count=3)

        assert result == {"requested": 3, "ready": 3}
        assert generate.await_count == 3

    async def test_writes_nothing_when_not_due(self):
        await self._judged(await _make_task(), 1)

        with _generating() as generate:
            result = await launch_backfill(count=3)

        assert result == {"skipped": "not due"}
        generate.assert_not_awaited()

    async def test_writes_nothing_when_nothing_is_requested(self):
        with _generating() as generate:
            result = await launch_backfill(count=0)

        assert "skipped" in result
        generate.assert_not_awaited()

    async def test_gives_up_when_the_claim_stays_held(self):
        assert await _claim_generation("appeal") is not None

        with _generating() as generate:
            result = await launch_backfill(count=3)

        assert "claim" in result["skipped"]
        generate.assert_not_awaited()

    async def test_the_wait_for_a_held_claim_ends(self):
        assert await _claim_generation("appeal") is not None

        with _waiting_for_the_claim(seconds=0.05), _generating() as generate:
            result = await launch_backfill(count=3)

        assert "claim" in result["skipped"]
        generate.assert_not_awaited()

    async def test_writes_once_a_held_claim_comes_free(self):
        """The deploy kills the refill actor that held the claim without it
        releasing; the backfill waits that claim out rather than skipping."""
        held = await _claim_generation("appeal")

        with _waiting_for_the_claim(), _held_until_asked(
            held
        ), _generating() as generate:
            result = await launch_backfill(count=3)

        assert result == {"requested": 3, "ready": 3}
        assert generate.await_count == 3

    async def test_tasks_written_while_it_waited_count_toward_the_total(self):
        """The gate is not asked again: the refill's batch that held the
        claim writes unvoted tasks, which would call the backfill off."""
        held = await _claim_generation("appeal")
        # Judged before the launch; not the backfill's.
        earlier = await self._judged(await _make_task(), 2)
        await ChooserTask.objects.filter(id=earlier.id).aupdate(
            created_at=timezone.now() - datetime.timedelta(hours=1)
        )

        with _waiting_for_the_claim(), _held_until_asked(
            held, written=2
        ), _generating() as generate:
            result = await launch_backfill(count=5)

        assert result == {"requested": 3, "ready": 3}
        assert generate.await_count == 3

    async def test_writes_nothing_once_the_count_was_written_while_it_waited(self):
        held = await _claim_generation("appeal")

        with _waiting_for_the_claim(), _held_until_asked(
            held, written=2
        ), _generating() as generate:
            result = await launch_backfill(count=2)

        assert "written while waiting" in result["skipped"]
        generate.assert_not_awaited()
        # And the claim was given back.
        assert await _claim_generation("appeal") is not None

    async def test_holds_the_claim_while_writing_and_releases_it_after(self):
        held = []

        async def generate(task_type):
            held.append(await _claim_generation(task_type))
            return True

        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task", new=generate
        ):
            await launch_backfill(count=2)

        assert held == [None, None]
        assert await _claim_generation("appeal") is not None

    async def test_stops_after_tasks_fail_in_a_row(self):
        with _generating(ready=False) as generate:
            result = await launch_backfill(count=20)

        assert result == {"requested": 20, "ready": 0}
        assert generate.await_count == CHOOSER_BACKFILL_STOP_AFTER_FAILURES

    async def test_a_task_that_comes_out_ready_resets_the_failure_count(self):
        outcomes = [False, False, True, False, False, True]
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task",
            new=AsyncMock(side_effect=outcomes),
        ) as generate:
            result = await launch_backfill(count=len(outcomes))

        assert result == {"requested": 6, "ready": 2}
        assert generate.await_count == 6

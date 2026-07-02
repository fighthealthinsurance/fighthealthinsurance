"""Tests for chooser back-fill (refill) thresholds and synthesized candidates.

Covers two behaviors added alongside synthesis tracking:

- ``_count_unscored_tasks`` / ``check_and_refill_task_pool`` back-fill the
  pool whenever fewer than ``CHOOSER_MIN_UNSCORED_TASKS`` fresh (unvoted)
  synthetic tasks remain for a type, independent of the overall READY pool.
- ``_maybe_add_synthesized_candidate`` adds a single synthesized candidate
  combining the per-model drafts so the chooser can compare synthesis to the
  individual models.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.chooser_tasks import (
    _count_unscored_tasks,
    _maybe_add_synthesized_candidate,
    _synthesize_appeal_candidate,
    _synthesize_chat_candidate,
    check_and_refill_task_pool,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
)


async def _make_task(task_type="appeal", status="READY", source="synthetic"):
    return await ChooserTask.objects.acreate(
        task_type=task_type, status=status, source=source
    )


async def _make_candidate(task, index=0, kind="appeal_letter", content="x"):
    return await ChooserCandidate.objects.acreate(
        task=task,
        candidate_index=index,
        kind=kind,
        model_name=f"model-{index}",
        content=content,
    )


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
            new=MagicMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(),
        ) as mock_fire:
            await check_and_refill_task_pool()

        # Once for "appeal" and once for "chat".
        assert mock_fire.await_count == 2

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
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=MagicMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(),
        ) as mock_fire:
            await check_and_refill_task_pool()

        mock_fire.assert_not_awaited()

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
            new=MagicMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(),
        ) as mock_fire:
            await check_and_refill_task_pool()

        # ready=1>=1 (healthy) for both; unscored=0<5 -> both refill solely via
        # the unscored branch. Dropping that clause would make this 0.
        assert mock_fire.await_count == 2

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
            new=MagicMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(),
        ) as mock_fire:
            await check_and_refill_task_pool()

        # ready=2<5 -> both refill; unscored=2>=1 is healthy, so the trigger is
        # solely the ready-pool branch. Dropping that clause would make this 0.
        assert mock_fire.await_count == 2


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
            "fighthealthinsurance.chooser_tasks.ml_router.best_internal_model",
            return_value=model,
        ):
            result = await _synthesize_chat_candidate(context, responses)

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

    async def test_chat_synthesis_returns_none_without_internal_model(self):
        with patch(
            "fighthealthinsurance.chooser_tasks.ml_router.best_internal_model",
            return_value=None,
        ):
            result = await _synthesize_chat_candidate(
                {"prompt": "hi", "history": []}, ["a", "b"]
            )
        assert result is None

    async def test_chat_synthesis_rejects_too_short_model_output(self):
        model = MagicMock()
        model._infer_no_context = AsyncMock(return_value="short")
        with patch(
            "fighthealthinsurance.chooser_tasks.ml_router.best_internal_model",
            return_value=model,
        ):
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

"""Tests for chooser back-fill (refill) thresholds and synthesized candidates.

Covers two behaviors added alongside synthesis tracking:

- ``_count_unscored_tasks`` / ``check_and_refill_task_pool`` back-fill the
  pool whenever fewer than ``CHOOSER_MIN_UNSCORED_TASKS`` fresh (unvoted)
  synthetic tasks remain for a type, independent of the overall READY pool.
- ``_maybe_add_synthesized_candidate`` adds a single synthesized candidate
  combining the per-model drafts so the chooser can compare synthesis to the
  individual models.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import TransactionTestCase

from fighthealthinsurance.chooser_tasks import (
    _count_unscored_tasks,
    _maybe_add_synthesized_candidate,
    check_and_refill_task_pool,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
)


def _make_task(task_type="appeal", status="READY", source="synthetic"):
    return ChooserTask.objects.create(task_type=task_type, status=status, source=source)


def _make_candidate(task, index=0, kind="appeal_letter", content="x"):
    return ChooserCandidate.objects.create(
        task=task,
        candidate_index=index,
        kind=kind,
        model_name=f"model-{index}",
        content=content,
    )


@pytest.mark.django_db
class CountUnscoredTasksTest(TransactionTestCase):
    """_count_unscored_tasks should only count fresh, READY, synthetic tasks."""

    def test_counts_only_ready_synthetic_unvoted_tasks(self):
        # Counts: READY synthetic appeal with no votes.
        _make_task()
        _make_task()

        # Excluded: READY synthetic appeal that already has a vote.
        voted = _make_task()
        cand = _make_candidate(voted)
        ChooserVote.objects.create(
            task=voted,
            chosen_candidate=cand,
            presented_candidate_ids=[cand.id],
            session_key="sess-voted",
        )

        # Excluded: not READY.
        _make_task(status="QUEUED")
        # Excluded: not synthetic.
        _make_task(source="real")
        # Excluded: different task type.
        _make_task(task_type="chat")

        count = asyncio.run(_count_unscored_tasks("appeal"))
        self.assertEqual(count, 2)

    def test_chat_counted_separately(self):
        _make_task(task_type="chat")
        _make_task(task_type="appeal")

        self.assertEqual(asyncio.run(_count_unscored_tasks("chat")), 1)
        self.assertEqual(asyncio.run(_count_unscored_tasks("appeal")), 1)


@pytest.mark.django_db
class RefillThresholdTest(TransactionTestCase):
    """check_and_refill_task_pool back-fills based on the unscored threshold."""

    def test_refill_triggers_when_unscored_below_threshold(self):
        # Empty DB: every type is below threshold and should be back-filled.
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_batch_tasks",
            new=MagicMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks.fire_and_forget_in_new_threadpool",
            new=AsyncMock(),
        ) as mock_fire:
            asyncio.run(check_and_refill_task_pool())

        # Once for "appeal" and once for "chat".
        self.assertEqual(mock_fire.await_count, 2)

    def test_no_refill_when_enough_unscored_and_ready(self):
        # One fresh READY synthetic task per type, with thresholds lowered to 1
        # so the pool is considered healthy and no generation is triggered.
        _make_task(task_type="appeal")
        _make_task(task_type="chat")

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
            asyncio.run(check_and_refill_task_pool())

        mock_fire.assert_not_awaited()

    def test_refill_triggers_when_ready_pool_low_even_if_scored(self):
        # A voted task keeps the READY pool nonzero but leaves zero unscored
        # tasks, so the unscored threshold still forces a back-fill.
        task = _make_task(task_type="appeal")
        cand = _make_candidate(task)
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=cand,
            presented_candidate_ids=[cand.id],
            session_key="sess-1",
        )

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
            asyncio.run(check_and_refill_task_pool())

        # appeal has a READY task (ready>=1) but 0 unscored -> still refills;
        # chat is empty -> refills. Both types trigger.
        self.assertEqual(mock_fire.await_count, 2)


@pytest.mark.django_db
class SynthesizedCandidateTest(TransactionTestCase):
    """_maybe_add_synthesized_candidate appends a synthesized candidate."""

    def test_adds_synthesized_appeal_candidate(self):
        task = _make_task()
        task.context_json = {"procedure": "MRI", "diagnosis": "back pain"}
        task.num_candidates_generated = 2
        task.save()
        _make_candidate(task, 0, content="First draft appeal letter body.")
        _make_candidate(task, 1, content="Second draft appeal letter body.")

        synth_text = "Synthesized appeal letter " + "z" * 200
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=synth_text),
        ):
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        candidates = ChooserCandidate.objects.filter(task=task).order_by(
            "candidate_index"
        )
        self.assertEqual(candidates.count(), 3)
        synth = candidates.get(synthesized=True)
        self.assertEqual(synth.candidate_index, 2)
        self.assertEqual(synth.model_name, "synthesized")
        self.assertEqual(synth.kind, "appeal_letter")
        self.assertEqual(synth.content, synth_text)
        task.refresh_from_db()
        self.assertEqual(task.num_candidates_generated, 3)

    def test_skips_synthesis_with_single_candidate(self):
        task = _make_task()
        _make_candidate(task, 0, content="Only one draft.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="should not be used"),
        ) as mock_synth:
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        mock_synth.assert_not_called()
        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 1)

    def test_skips_verbatim_duplicate(self):
        task = _make_task()
        # Drafts long enough to clear the min-length bar, so this exercises the
        # verbatim-duplicate skip rather than the too-short skip.
        draft_one = "Draft one appeal letter body. " * 5
        _make_candidate(task, 0, content=draft_one)
        _make_candidate(task, 1, content="Draft two appeal letter body. " * 5)

        # Synthesis returns a verbatim copy of an existing draft.
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=draft_one),
        ):
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        self.assertFalse(
            ChooserCandidate.objects.filter(task=task, synthesized=True).exists()
        )
        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 2)

    def test_skips_too_short_synthesis(self):
        task = _make_task()
        _make_candidate(task, 0, content="First appeal draft body text here.")
        _make_candidate(task, 1, content="Second appeal draft body text here.")

        # A synthesized appeal under the 100-char base-candidate bar is rejected.
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="Too short."),
        ):
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        self.assertFalse(
            ChooserCandidate.objects.filter(task=task, synthesized=True).exists()
        )
        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 2)

    def test_skips_synthesis_when_drafts_identical(self):
        task = _make_task()
        _make_candidate(task, 0, content="Identical appeal draft body.")
        _make_candidate(task, 1, content="Identical appeal draft body.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="x" * 200),
        ) as mock_synth:
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        # After dedupe there is only one distinct draft -> synthesis skipped.
        mock_synth.assert_not_called()
        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 2)

    def test_disabled_when_synthesis_returns_none(self):
        task = _make_task()
        _make_candidate(task, 0, content="Draft one body text.")
        _make_candidate(task, 1, content="Draft two body text.")

        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value=None),
        ):
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 2)

    def test_respects_include_synthesis_flag(self):
        task = _make_task()
        _make_candidate(task, 0, content="Draft one body text.")
        _make_candidate(task, 1, content="Draft two body text.")

        with patch(
            "fighthealthinsurance.chooser_tasks.CHOOSER_INCLUDE_SYNTHESIS", False
        ), patch(
            "fighthealthinsurance.chooser_tasks._synthesize_appeal_candidate",
            new=AsyncMock(return_value="x" * 200),
        ) as mock_synth:
            asyncio.run(_maybe_add_synthesized_candidate(task, "appeal_letter"))

        mock_synth.assert_not_called()
        self.assertEqual(ChooserCandidate.objects.filter(task=task).count(), 2)

    def test_adds_synthesized_chat_candidate(self):
        task = _make_task(task_type="chat")
        task.context_json = {"prompt": "How do I appeal?", "history": []}
        task.save()
        _make_candidate(task, 0, kind="chat_response", content="First chat response.")
        _make_candidate(task, 1, kind="chat_response", content="Second chat response.")

        # Must clear the chat min-length bar (>50 chars).
        synth_text = (
            "Best combined chat response that pulls together the clearest guidance."
        )
        with patch(
            "fighthealthinsurance.chooser_tasks._synthesize_chat_candidate",
            new=AsyncMock(return_value=synth_text),
        ):
            asyncio.run(_maybe_add_synthesized_candidate(task, "chat_response"))

        synth = ChooserCandidate.objects.get(task=task, synthesized=True)
        self.assertEqual(synth.kind, "chat_response")
        self.assertEqual(synth.candidate_index, 2)
        self.assertEqual(synth.content, synth_text)

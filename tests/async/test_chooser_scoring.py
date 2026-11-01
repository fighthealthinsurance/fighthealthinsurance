import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

from fighthealthinsurance import chooser_scoring
from fighthealthinsurance.chooser_scoring import score_task_candidates
from fighthealthinsurance.chooser_tasks import _generate_single_task
from fighthealthinsurance.ml import chat_gate, letter_quality
from fighthealthinsurance.models import ChooserCandidate, ChooserTask

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _task(task_type="appeal", count=3):
    context = (
        {
            "procedure": "MRI",
            "diagnosis": "back pain",
            "insurance_company": "Example Mutual",
            "denial_text_preview": "Not medically necessary",
        }
        if task_type == "appeal"
        else {"prompt": "How do I appeal this MRI denial?", "history": []}
    )
    task = await ChooserTask.objects.acreate(
        task_type=task_type, status="QUEUED", context_json=context
    )
    candidates = []
    for index in range(count):
        candidates.append(
            await ChooserCandidate.objects.acreate(
                task=task,
                candidate_index=index,
                kind="appeal_letter" if task_type == "appeal" else "chat_response",
                model_name=f"model-{index}",
                content=f"Draft {index}",
            )
        )
    return task, candidates


def _local(scores):
    model = MagicMock()
    model.name = "fhi-local"
    model._infer_no_context = AsyncMock(
        return_value=json.dumps({"scores": scores})
    )
    return model


def _local_judge(model):
    return patch.multiple(
        chooser_scoring.ml_router,
        _healthy_general_internal=MagicMock(return_value=[model]),
        best_internal_model=MagicMock(return_value=model),
    )


def _letter_score(quality):
    return letter_quality.LetterScore(
        quality=quality,
        grounding=2.0,
        scores={},
        input_tokens=0,
        scorer="typesafe/jev-1.13.0/rubric-2",
    )


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_appeal_picks_store_each_judges_winner_without_candidate_labels():
    task, candidates = await _task()
    local = _local([0.4, 0.7, 0.95])

    async def score_letter(_context, content):
        return _letter_score({"Draft 0": 0.9, "Draft 1": 0.5, "Draft 2": 0.6}[content])

    with _local_judge(local), patch.object(
        chooser_scoring.letter_quality, "score_letter", new=score_letter
    ):
        await score_task_candidates(task)

    await task.arefresh_from_db()
    assert task.scoring_json == {
        "jev": {
            "candidate_id": candidates[0].id,
            "candidate_index": 0,
            "model_name": "model-0",
            "score": 0.9,
            "scorer": "typesafe/jev-1.13.0/rubric-2",
        },
        "fhi_local": {
            "candidate_id": candidates[2].id,
            "candidate_index": 2,
            "model_name": "model-2",
            "score": 0.95,
            "scorer": "fhi-local/chooser-rubric-1",
        },
    }
    local_prompt = local._infer_no_context.await_args.kwargs["prompt"]
    assert all(f"model-{index}" not in local_prompt for index in range(3))


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_chat_scoring_includes_the_fifth_candidate_in_balanced_batches():
    task, candidates = await _task(task_type="chat", count=5)
    local = _local([0.99, 0.1, 0.1, 0.1, 0.1])
    batches = []

    async def rank_replies(_message, replies, *, timeout):
        batches.append(list(replies))
        scores = tuple(
            chat_gate.GateScores(
                answers=0.99 if reply == "Draft 4" else 0.2,
                verdict=0.0,
                asks_again=0.0,
                promises=0.0,
            )
            for reply in replies
        )
        return chat_gate.RankResult(
            outcome=chat_gate.RANK_PICKED,
            scores=scores,
            scorer="typesafe/jev-1.13.0/chat-gate-rubric-3",
        )

    with _local_judge(local), patch.object(
        chooser_scoring.chat_gate, "enabled", return_value=True
    ), patch.object(
        chooser_scoring.chat_gate, "rank_replies", new=rank_replies
    ):
        await score_task_candidates(task)

    await task.arefresh_from_db()
    assert [len(batch) for batch in batches] == [3, 2]
    assert task.scoring_json["jev"]["candidate_id"] == candidates[4].id
    assert task.scoring_json["fhi_local"]["candidate_id"] == candidates[0].id


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_one_unavailable_judge_does_not_erase_the_other_pick():
    task, candidates = await _task(count=2)
    local = _local([0.2, 0.8])

    with _local_judge(local), patch.object(
        chooser_scoring.letter_quality,
        "score_letter",
        new=AsyncMock(return_value=None),
    ):
        await score_task_candidates(task)

    await task.arefresh_from_db()
    assert set(task.scoring_json) == {"fhi_local"}
    assert task.scoring_json["fhi_local"]["candidate_id"] == candidates[1].id


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_malformed_scores_from_every_judge_leave_no_pick():
    task, _ = await _task(count=2)
    local = _local([0.2, 0.8])
    local._infer_no_context.return_value = "not json"

    with _local_judge(local), patch.object(
        chooser_scoring.letter_quality,
        "score_letter",
        new=AsyncMock(return_value=None),
    ):
        await score_task_candidates(task)

    await task.arefresh_from_db()
    assert task.scoring_json == {}


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_disabled_scoring_does_not_call_a_judge():
    task, _ = await _task(count=2)
    score_letter = AsyncMock()
    best_internal = MagicMock()

    with override_settings(CHOOSER_SCORE_CANDIDATES=False), patch.object(
        chooser_scoring.letter_quality, "score_letter", new=score_letter
    ), patch.object(
        chooser_scoring.ml_router, "best_internal_model", new=best_internal
    ):
        await score_task_candidates(task)

    score_letter.assert_not_called()
    best_internal.assert_not_called()


async def test_local_score_parser_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        chooser_scoring._local_scores('{"scores": [0.5, 1.1]}', 2)


@override_settings(CHOOSER_SCORE_CANDIDATES=True)
async def test_a_scoring_failure_does_not_prevent_a_task_from_becoming_ready():
    async def seat_candidates(task):
        for index in range(2):
            await ChooserCandidate.objects.acreate(
                task=task,
                candidate_index=index,
                kind="appeal_letter",
                model_name=f"model-{index}",
                content=f"Draft {index}",
            )
        task.num_candidates_generated = 2
        await task.asave()

    with patch(
        "fighthealthinsurance.chooser_tasks._generate_appeal_candidates",
        new=seat_candidates,
    ), patch(
        "fighthealthinsurance.chooser_scoring.score_task_candidates",
        new=AsyncMock(side_effect=RuntimeError("judge unavailable")),
    ):
        await _generate_single_task("appeal")

    task = await ChooserTask.objects.alatest("id")
    assert task.status == "READY"

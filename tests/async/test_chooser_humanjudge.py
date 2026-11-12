import json

import httpx
import pytest
from django.test import override_settings

from fighthealthinsurance import chooser_humanjudge
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

ON = override_settings(
    CHOOSER_HUMANJUDGE=True,
    HUMANJUDGE_API_KEY="gj_sk_test_not_real",
    HUMANJUDGE_TOKEN="",
    HUMANJUDGE_MODEL_ID="arena-model",
    HUMANJUDGE_BASE_URL="https://humanjudge.invalid",
    CHOOSER_HUMANJUDGE_SUBMIT_BATCH=20,
    CHOOSER_HUMANJUDGE_MIN_VERDICTS=1,
)

CONTEXT = {
    "procedure": "MRI",
    "diagnosis": "back pain",
    "insurance_company": "Example Mutual",
    "denial_text_preview": "Not medically necessary",
    "facts": ["Six weeks of physical therapy did not help"],
}


class FakeHumanJudge:
    """Just enough of HumanJudge's API: trace ingest, models/me, votes."""

    def __init__(self):
        self.traces: list[dict] = []
        self.votes: list[dict] = []
        self.ingest_status: list[int] = []  # next statuses, then 201
        self.read_status = 200
        self.vote_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/traces/ingest":
            status = self.ingest_status.pop(0) if self.ingest_status else 201
            if status in (200, 201):
                self.traces.append(json.loads(request.content))
            return httpx.Response(status, json={})
        if request.url.path == "/api/v1/models/me":
            return httpx.Response(self.read_status, json=[{"id": "arena-model"}])
        if request.url.path == "/api/v1/models/arena-model/evaluations":
            self.vote_requests.append(request)
            offset = int(request.url.params.get("offset", 0))
            return httpx.Response(self.read_status, json=self.votes[offset:])
        return httpx.Response(404, json={})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def vote(self, inference_id, verdict, evaluator="r1", **extra):
        self.votes.append(
            {
                "trace_id": inference_id,
                "verdict": verdict,
                "evaluator_id": evaluator,
                "timestamp": "2026-11-10T12:00:00Z",
                "flag_category": "Wrong" if verdict == "flag" else "",
                "feedback": "",
                **extra,
            }
        )


async def _task(count=2, status="READY", task_type="appeal", source="synthetic"):
    task = await ChooserTask.objects.acreate(
        task_type=task_type, status=status, source=source, context_json=CONTEXT
    )
    candidates = [
        await ChooserCandidate.objects.acreate(
            task=task,
            candidate_index=index,
            kind="appeal_letter",
            model_name=f"secret-model-{index}",
            content=f"Dear insurer, draft {index}",
        )
        for index in range(count)
    ]
    return task, candidates


async def _inference_ids(candidates):
    return [
        (await HumanJudgeSubmission.objects.aget(candidate=c)).inference_id
        for c in candidates
    ]


async def test_sync_is_off_without_an_api_key():
    await _task()
    assert await chooser_humanjudge.sync(FakeHumanJudge().transport) is None


@ON
async def test_sync_sends_only_synthetic_appeal_drafts_of_cases_shown_to_voters():
    _, wanted = await _task()
    await _task(task_type="chat")
    await _task(source="user")
    await _task(status="DISABLED")
    await _task(status="QUEUED")
    fake = FakeHumanJudge()

    await chooser_humanjudge.sync(fake.transport)

    sent = sorted(t["metadata"]["candidate_id"] for t in fake.traces)
    assert sent == sorted(c.id for c in wanted)


@ON
async def test_sent_trace_never_names_the_model_that_wrote_the_draft():
    await _task()
    fake = FakeHumanJudge()

    await chooser_humanjudge.sync(fake.transport)

    assert fake.traces and "secret-model" not in json.dumps(fake.traces)


@ON
async def test_sent_trace_carries_the_synthetic_case_and_the_draft():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()

    await chooser_humanjudge.sync(fake.transport)

    trace = next(
        t for t in fake.traces if t["metadata"]["candidate_id"] == candidates[0].id
    )
    assert (
        "made-up" in trace["input"]
        and "Six weeks of physical therapy" in trace["input"]
        and trace["output"] == candidates[0].content
    )


@ON
async def test_failed_send_is_retried_with_the_same_inference_id():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [500, 500]

    await chooser_humanjudge.sync(fake.transport)
    first_ids = await _inference_ids(candidates)
    await chooser_humanjudge.sync(fake.transport)

    assert sorted(t["gj_inference_id"] for t in fake.traces) == sorted(first_ids)


@ON
async def test_refused_key_stops_the_round_without_spending_attempts():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [401]

    await chooser_humanjudge.sync(fake.transport)

    attempts = [
        (await HumanJudgeSubmission.objects.aget(candidate=c)).attempts
        for c in candidates
    ]
    assert attempts == [0, 0]


@ON
async def test_verdicts_are_stored_apart_from_arena_votes():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "flag")

    await chooser_humanjudge.sync(fake.transport)

    assert (await HumanJudgeVerdict.objects.acount(), await ChooserVote.objects.acount()) == (2, 0)


@ON
async def test_refetched_verdicts_are_not_stored_twice():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "flag", id="vote-7")

    await chooser_humanjudge.sync(fake.transport)
    await chooser_humanjudge.sync(fake.transport)

    assert await HumanJudgeVerdict.objects.acount() == 2


@ON
async def test_pick_is_the_draft_with_the_best_pass_rate_beside_other_judges():
    task, candidates = await _task(count=3)
    task.scoring_json = {"jev": {"candidate_id": candidates[2].id}}
    await task.asave()
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "flag")
    fake.vote(ids[1], "pass", evaluator="r1")
    fake.vote(ids[1], "pass", evaluator="r2")
    fake.vote(ids[2], "pass", evaluator="r1")
    fake.vote(ids[2], "flag", evaluator="r2")

    await chooser_humanjudge.sync(fake.transport)

    await task.arefresh_from_db()
    assert (
        task.scoring_json["humanjudge"]["candidate_id"],
        task.scoring_json["humanjudge"]["scorer"],
        task.scoring_json["jev"]["candidate_id"],
    ) == (candidates[1].id, chooser_humanjudge.SCORER, candidates[2].id)


@ON
async def test_no_pick_while_a_draft_has_no_verdict():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")

    await chooser_humanjudge.sync(fake.transport)

    await task.arefresh_from_db()
    assert "humanjudge" not in task.scoring_json


@ON
async def test_no_pick_when_the_best_drafts_are_tied():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "pass")

    await chooser_humanjudge.sync(fake.transport)

    await task.arefresh_from_db()
    assert "humanjudge" not in task.scoring_json


@ON
async def test_votes_on_other_traces_are_ignored():
    await _task(count=2)
    fake = FakeHumanJudge()
    fake.vote("gj_inf_someone_else", "pass")

    await chooser_humanjudge.sync(fake.transport)

    assert await HumanJudgeVerdict.objects.acount() == 0


@ON
async def test_later_syncs_read_votes_from_shortly_before_the_newest_stored():
    _, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await chooser_humanjudge.sync(fake.transport)
    fake.vote((await _inference_ids(candidates))[0], "pass")
    await chooser_humanjudge.sync(fake.transport)

    await chooser_humanjudge.sync(fake.transport)

    assert fake.vote_requests[-1].url.params.get("from_date") == "2026-11-08"


@pytest.mark.parametrize(
    "raw, expected",
    [("pass", "pass"), ("FLAG", "flag"), (True, "pass"), (False, "flag"), ("?", None)],
)
async def test_record_verdict_normalizes_pass_and_flag(raw, expected):
    assert chooser_humanjudge._record_verdict({"verdict": raw}) == expected


@ON
@override_settings(CHOOSER_HUMANJUDGE_SUBMIT_BATCH=2)
async def test_an_outage_does_not_queue_the_whole_backlog():
    for _ in range(3):
        await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [500] * 10

    await chooser_humanjudge.sync(fake.transport)
    await chooser_humanjudge.sync(fake.transport)

    assert await HumanJudgeSubmission.objects.acount() == 2

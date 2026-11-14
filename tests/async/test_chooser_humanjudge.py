import json
from unittest.mock import patch

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


async def _upload(fake, *tasks):
    return await chooser_humanjudge.upload_tasks([t.id for t in tasks], fake.transport)


async def _download(fake):
    return await chooser_humanjudge.download_verdicts(fake.transport)


async def _inference_ids(candidates):
    return [
        (await HumanJudgeSubmission.objects.aget(candidate=c)).inference_id
        for c in candidates
    ]


async def test_upload_refuses_when_humanjudge_is_not_configured():
    task, _ = await _task()
    with pytest.raises(chooser_humanjudge.HumanJudgeError):
        await _upload(FakeHumanJudge(), task)


@ON
async def test_upload_sends_only_synthetic_appeal_drafts_of_cases_shown_to_voters():
    wanted, candidates = await _task()
    others = [
        (await _task(task_type="chat"))[0],
        (await _task(source="user"))[0],
        (await _task(status="DISABLED"))[0],
        (await _task(status="QUEUED"))[0],
    ]
    fake = FakeHumanJudge()

    result = await _upload(fake, wanted, *others)

    sent = sorted(t["metadata"]["candidate_id"] for t in fake.traces)
    assert (sent, result.not_sendable) == (sorted(c.id for c in candidates), 4)


@ON
async def test_upload_refuses_more_cases_than_one_upload_may_send():
    with pytest.raises(chooser_humanjudge.HumanJudgeError):
        await chooser_humanjudge.upload_tasks(
            range(1, chooser_humanjudge.MAX_TASKS_PER_UPLOAD + 2)
        )


@ON
async def test_sent_trace_never_names_the_model_that_wrote_the_draft():
    task, _ = await _task()
    fake = FakeHumanJudge()

    await _upload(fake, task)

    assert fake.traces and "secret-model" not in json.dumps(fake.traces)


@ON
async def test_sent_trace_carries_the_synthetic_case_and_the_draft():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()

    await _upload(fake, task)

    trace = next(
        t for t in fake.traces if t["metadata"]["candidate_id"] == candidates[0].id
    )
    assert (
        "made-up" in trace["input"]
        and "Six weeks of physical therapy" in trace["input"]
        and trace["output"] == candidates[0].content
    )


@ON
async def test_uploading_a_case_again_sends_only_what_has_not_gone():
    task, _ = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [500]

    await _upload(fake, task)
    result = await _upload(fake, task)

    assert (len(fake.traces), result.sent, result.already_sent) == (2, 1, 1)


@ON
async def test_failed_send_is_retried_with_the_same_inference_id():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [500, 500]

    await _upload(fake, task)
    first_ids = await _inference_ids(candidates)
    await _upload(fake, task)

    assert sorted(t["gj_inference_id"] for t in fake.traces) == sorted(first_ids)


@ON
async def test_a_failure_never_overwrites_another_uploads_sent():
    task, candidates = await _task(count=1)
    submission = await HumanJudgeSubmission.objects.acreate(
        task=task, candidate=candidates[0], inference_id="gj_inf_1_race"
    )
    stale = await HumanJudgeSubmission.objects.select_related(
        "candidate", "task"
    ).aget(pk=submission.pk)
    # Another upload sends it while this one's request is in flight, and
    # this one's request then fails.

    async def handler(request):
        await HumanJudgeSubmission.objects.filter(pk=submission.pk).aupdate(
            status="SENT"
        )
        return httpx.Response(500, json={})

    async with chooser_humanjudge.HumanJudgeClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        went = await chooser_humanjudge._send(client, stale)

    assert went is False
    await submission.arefresh_from_db()
    assert submission.status == "SENT"


@ON
async def test_a_draft_already_sent_by_another_upload_is_not_sent_again():
    task, candidates = await _task(count=1)
    submission = await HumanJudgeSubmission.objects.acreate(
        task=task, candidate=candidates[0], inference_id="gj_inf_1_twice"
    )
    stale = await HumanJudgeSubmission.objects.select_related(
        "candidate", "task"
    ).aget(pk=submission.pk)
    await HumanJudgeSubmission.objects.filter(pk=submission.pk).aupdate(
        status="SENT"
    )
    fake = FakeHumanJudge()

    async with chooser_humanjudge.HumanJudgeClient(transport=fake.transport) as client:
        await chooser_humanjudge._send(client, stale)

    assert fake.traces == []


@ON
async def test_a_refused_key_in_a_background_upload_is_recorded_on_its_drafts():
    task, _ = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [401]

    real_upload = chooser_humanjudge.upload_tasks
    with patch.object(
        chooser_humanjudge,
        "upload_tasks",
        new=lambda ids: real_upload(ids, fake.transport),
    ):
        await chooser_humanjudge._upload_in_background([task.id])

    rows = {
        (status, error)
        async for status, error in HumanJudgeSubmission.objects.values_list(
            "status", "last_error"
        )
    }
    assert rows == {
        ("FAILED", "Upload stopped: HumanJudge refused the key (trace ingest: HTTP 401)")
    }


@ON
async def test_refused_key_stops_the_upload_without_spending_attempts():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    fake.ingest_status = [401]

    with pytest.raises(chooser_humanjudge.HumanJudgeAuthError):
        await _upload(fake, task)

    attempts = [
        (await HumanJudgeSubmission.objects.aget(candidate=c)).attempts
        for c in candidates
    ]
    assert attempts == [0, 0]


@ON
async def test_verdicts_are_stored_apart_from_arena_votes():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "flag")

    result = await _download(fake)

    assert (
        result.new_verdicts,
        await HumanJudgeVerdict.objects.acount(),
        await ChooserVote.objects.acount(),
    ) == (2, 2, 0)


@ON
async def test_refetched_verdicts_are_not_stored_twice():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "flag", id="vote-7")

    await _download(fake)
    await _download(fake)

    assert await HumanJudgeVerdict.objects.acount() == 2


@ON
async def test_pick_is_the_draft_with_the_best_pass_rate_beside_other_judges():
    task, candidates = await _task(count=3)
    task.scoring_json = {"jev": {"candidate_id": candidates[2].id}}
    await task.asave()
    fake = FakeHumanJudge()
    await _upload(fake, task)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "flag")
    fake.vote(ids[1], "pass", evaluator="r1")
    fake.vote(ids[1], "pass", evaluator="r2")
    fake.vote(ids[2], "pass", evaluator="r1")
    fake.vote(ids[2], "flag", evaluator="r2")

    await _download(fake)

    await task.arefresh_from_db()
    assert (
        task.scoring_json["humanjudge"]["candidate_id"],
        task.scoring_json["humanjudge"]["scorer"],
        task.scoring_json["jev"]["candidate_id"],
    ) == (candidates[1].id, chooser_humanjudge.SCORER, candidates[2].id)


@ON
async def test_a_download_cut_short_before_the_picks_is_repaired_by_the_next():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "flag")
    with patch.object(
        chooser_humanjudge, "update_picks", side_effect=RuntimeError("cut short")
    ), pytest.raises(RuntimeError):
        await _download(fake)

    await _download(fake)

    await task.arefresh_from_db()
    assert task.scoring_json["humanjudge"]["candidate_id"] == candidates[0].id


@ON
async def test_no_pick_while_a_draft_has_no_verdict():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    fake.vote((await _inference_ids(candidates))[0], "pass")

    await _download(fake)

    await task.arefresh_from_db()
    assert "humanjudge" not in task.scoring_json


@ON
async def test_no_pick_when_the_best_drafts_are_tied():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    ids = await _inference_ids(candidates)
    fake.vote(ids[0], "pass")
    fake.vote(ids[1], "pass")

    await _download(fake)

    await task.arefresh_from_db()
    assert "humanjudge" not in task.scoring_json


@ON
async def test_votes_on_other_traces_are_counted_and_left_out():
    fake = FakeHumanJudge()
    fake.vote("gj_inf_someone_else", "pass")

    result = await _download(fake)

    assert (result.unmatched, await HumanJudgeVerdict.objects.acount()) == (1, 0)


@ON
async def test_later_downloads_read_votes_from_shortly_before_the_newest_stored():
    task, candidates = await _task(count=2)
    fake = FakeHumanJudge()
    await _upload(fake, task)
    fake.vote((await _inference_ids(candidates))[0], "pass")
    await _download(fake)

    await _download(fake)

    assert fake.vote_requests[-1].url.params.get("from_date") == "2026-11-08"


@pytest.mark.parametrize(
    "raw, expected",
    [("pass", "pass"), ("FLAG", "flag"), (True, "pass"), (False, "flag"), ("?", None)],
)
async def test_record_verdict_normalizes_pass_and_flag(raw, expected):
    assert chooser_humanjudge._record_verdict({"verdict": raw}) == expected

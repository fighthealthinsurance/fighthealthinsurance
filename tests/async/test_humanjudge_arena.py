"""Our models in a HumanJudge arena: the export, the OpenAI-compatible
endpoint HumanJudge calls, the backfill, and the verdict download."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import AsyncClient, override_settings

from fighthealthinsurance import chooser_tasks, humanjudge_arena
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    HumanJudgeArenaAnswer,
    HumanJudgeArenaCase,
    HumanJudgeArenaVerdict,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

KEY = "test-callback-not-real"
ON = override_settings(
    HUMAN_JUDGE_OAI_CALLBACK=KEY,
    HUMANJUDGE_ARENA_ID="arena-1",
    HUMANJUDGE_TOKEN="gj_pat_test_not_real",
    HUMANJUDGE_BASE_URL="https://humanjudge.invalid",
    HUMANJUDGE_ARENA_ANSWER_TIMEOUT=5,
)
AUTH = {"Authorization": f"Bearer {KEY}"}
CHAT_URL = "/ziggy/humanjudge/v1/chat/completions"
MODELS_URL = "/ziggy/humanjudge/v1/models"

CONTEXT = {
    "procedure": "MRI of the lower back",
    "diagnosis": "lumbar radiculopathy",
    "insurance_company": "Example Mutual",
    "denial_text_preview": "Not medically necessary",
    "facts": ["Six weeks of physical therapy did not help"],
    "evidence": [
        {"pmid": "123", "title": "MRI in radiculopathy", "summary": "MRI helps."}
    ],
}
LETTER = "Dear Example Mutual, " + "the MRI is medically necessary. " * 10


class FakeModel:
    """The surface humanjudge_arena asks of a backend."""

    def __init__(self, name, reply=LETTER, delay=0.0):
        self.name = name
        self.reply = reply
        self.delay = delay
        self.prompts: list[str] = []
        self.system_prompts: list[list[str]] = []

    context_only = False

    async def _infer_no_context(self, system_prompts, prompt, **kwargs):
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompts)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply


@pytest.fixture
def backends():
    """Two arena models, fhi-local and outside-model, in place of the router's."""
    models = [FakeModel("fhi-local"), FakeModel("outside-model")]
    with patch.object(
        chooser_tasks, "_comparable_backends", MagicMock(return_value=models)
    ):
        yield {m.name: m for m in models}


async def _task(source="synthetic", status="READY", context=None, drafts=("fhi-local",)):
    task = await ChooserTask.objects.acreate(
        task_type="appeal", status=status, source=source, context_json=context or CONTEXT
    )
    for index, name in enumerate(drafts):
        await ChooserCandidate.objects.acreate(
            task=task,
            candidate_index=index,
            kind="appeal_letter",
            model_name=name,
            content=f"Arena draft by {name}: " + "it is necessary. " * 10,
        )
    return task


async def _exported(**kwargs):
    task = await _task(**kwargs)
    result = await humanjudge_arena.export_cases([task.id])
    return task, result.cases[0]


async def _ask(prompt, model="fhi-local", headers=AUTH):
    return await AsyncClient().post(
        CHAT_URL,
        data=json.dumps(
            {"model": model, "messages": [{"role": "user", "content": prompt}]}
        ),
        content_type="application/json",
        headers=headers,
    )


def _letter(response):
    return response.json()["choices"][0]["message"]["content"]


async def test_endpoint_answers_404_without_a_callback_key():
    response = await AsyncClient().get(MODELS_URL, headers=AUTH)
    assert response.status_code == 404


@ON
async def test_endpoint_refuses_a_call_without_the_key(backends):
    response = await _ask("anything", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@ON
async def test_models_lists_the_arena_backends_with_the_api_key_header(backends):
    response = await AsyncClient().get(MODELS_URL, headers={"api-key": KEY})
    assert [m["id"] for m in response.json()["data"]] == ["fhi-local", "outside-model"]


@ON
async def test_export_freezes_the_exact_prompt_our_models_are_given():
    task, case = await _exported()
    assert case.prompt == chooser_tasks._build_appeal_prompt(task.context_json)


@ON
async def test_export_paste_text_separates_cases_with_a_dashes_line():
    first, second = await _task(), await _task(context={**CONTEXT, "procedure": "CT"})
    result = await humanjudge_arena.export_cases([first.id, second.id])
    assert result.paste_batches()[0].count("\n---\n") == 1


@ON
async def test_export_skips_cases_that_are_not_synthetic():
    task = await _task(source="real")
    result = await humanjudge_arena.export_cases([task.id])
    assert (result.cases, result.not_sendable) == ([], 1)


@ON
async def test_export_skips_case_text_over_humanjudges_limit_rather_than_cutting_it():
    task = await _task(context={**CONTEXT, "facts": ["x" * 6000]})
    result = await humanjudge_arena.export_cases([task.id])
    assert result.too_long == [task.id]


@ON
async def test_known_case_is_answered_with_the_models_own_arena_draft(backends):
    _, case = await _exported()
    response = await _ask(case.prompt)
    assert _letter(response).startswith("Arena draft by fhi-local")
    assert backends["fhi-local"].prompts == []


@ON
async def test_known_case_without_the_models_draft_is_written_with_the_arena_prompt(
    backends,
):
    _, case = await _exported()
    response = await _ask(case.prompt, model="outside-model")
    assert _letter(response) == LETTER.strip()
    assert backends["outside-model"].prompts == [case.prompt]
    assert backends["outside-model"].system_prompts == [
        [chooser_tasks.APPEAL_SYSTEM_PROMPT]
    ]


@ON
async def test_a_written_answer_is_given_again_without_asking_the_model(backends):
    _, case = await _exported()
    await _ask(case.prompt, model="outside-model")
    await _ask(case.prompt, model="outside-model")
    assert len(backends["outside-model"].prompts) == 1


@ON
async def test_the_case_is_found_whatever_the_paste_box_did_to_whitespace(backends):
    _, case = await _exported()
    response = await _ask("  " + case.prompt.replace("\n", "\n\n  ") + "\n")
    assert _letter(response).startswith("Arena draft by fhi-local")


@ON
async def test_text_that_is_not_one_of_our_cases_is_answered_live(backends):
    response = await _ask("Write an appeal for a denied insulin pump.")
    assert response.status_code == 200
    answer = await HumanJudgeArenaAnswer.objects.aget()
    assert (answer.case_id, answer.source) == (None, "generated")


@ON
async def test_an_unknown_model_is_404(backends):
    response = await _ask("Write an appeal.", model="no-such-model")
    assert response.json()["error"]["code"] == "model_not_found"
    assert response.status_code == 404


@ON
async def test_a_model_that_writes_nothing_usable_is_a_failed_answer(backends):
    backends["outside-model"].reply = "Sorry."
    response = await _ask("Write an appeal.", model="outside-model")
    assert response.status_code == 502


@ON
async def test_streaming_is_refused(backends):
    response = await AsyncClient().post(
        CHAT_URL,
        data=json.dumps(
            {"model": "fhi-local", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        ),
        content_type="application/json",
        headers=AUTH,
    )
    assert response.status_code == 400


@ON
async def test_a_slow_letter_is_finished_and_given_on_the_next_call(backends):
    backends["outside-model"].delay = 0.3
    _, case = await _exported()
    with override_settings(HUMANJUDGE_ARENA_ANSWER_TIMEOUT=0.01):
        first = await _ask(case.prompt, model="outside-model")
    assert first.status_code == 503
    await asyncio.gather(*humanjudge_arena._in_flight.values())
    second = await _ask(case.prompt, model="outside-model")
    assert (second.status_code, len(backends["outside-model"].prompts)) == (200, 1)


@ON
async def test_backfill_stores_every_models_answer_to_every_case(backends):
    _, case = await _exported()
    result = await humanjudge_arena.backfill([case.id])
    assert (result.arena_drafts, result.generated) == (1, 1)


@ON
async def test_backfill_leaves_stored_answers_alone(backends):
    _, case = await _exported()
    await humanjudge_arena.backfill([case.id])
    result = await humanjudge_arena.backfill([case.id])
    assert (result.already, len(backends["outside-model"].prompts)) == (2, 1)


class FakeArena:
    """HumanJudge's arena votes endpoint."""

    def __init__(self, votes):
        self.votes = votes

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/benchmarks/arena-1/votes":
            offset = int(request.url.params.get("offset", 0))
            return httpx.Response(200, json=self.votes[offset:])
        return httpx.Response(404, json={})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def _vote(**fields):
    return {
        "trace_id": "tr-1",
        "model_slug": "fhi-local",
        "model_name": "fhi-local",
        "verdict": "flag",
        "flag_category": "Wrong",
        "feedback": "Cites a study not in the case",
        "evaluator_id": "rev-1",
        "timestamp": "2026-11-10T12:00:00Z",
        **fields,
    }


@ON
async def test_a_vote_carrying_the_case_text_is_matched_to_the_models_answer(backends):
    _, case = await _exported()
    await _ask(case.prompt)
    fake = FakeArena([_vote(input=case.prompt)])
    await humanjudge_arena.download_arena_verdicts(fake.transport)
    verdict = await HumanJudgeArenaVerdict.objects.select_related("answer").aget()
    assert (verdict.answer.model_name, verdict.case_id) == ("fhi-local", case.id)


@ON
async def test_a_vote_carrying_the_answer_text_is_matched_by_it(backends):
    _, case = await _exported()
    response = await _ask(case.prompt, model="outside-model")
    vote = _vote(model_name="Outside Model", output=_letter(response))  # slug match
    fake = FakeArena([vote])
    await humanjudge_arena.download_arena_verdicts(fake.transport)
    verdict = await HumanJudgeArenaVerdict.objects.select_related("answer").aget()
    assert verdict.answer.model_name == "outside-model"


@ON
async def test_a_vote_that_cannot_be_matched_yet_is_stored_and_matched_later(backends):
    _, case = await _exported()
    fake = FakeArena([_vote(input=case.prompt)])
    first = await humanjudge_arena.download_arena_verdicts(fake.transport)
    await _ask(case.prompt)
    second = await humanjudge_arena.download_arena_verdicts(fake.transport)
    assert (first.unmatched, second.rematched) == (1, 1)


@ON
async def test_downloading_a_vote_again_does_not_store_it_twice(backends):
    fake = FakeArena([_vote(id="v-1")])
    await humanjudge_arena.download_arena_verdicts(fake.transport)
    await humanjudge_arena.download_arena_verdicts(fake.transport)
    assert await HumanJudgeArenaVerdict.objects.acount() == 1


async def _matched_answer_of_only_vote():
    verdict = await HumanJudgeArenaVerdict.objects.aget()
    return verdict.answer_id


@ON
async def test_a_vote_with_only_humanjudges_documented_fields_is_stored_unmatched(
    backends,
):
    # trace_id, model_name, verdict, feedback, ...: no case or answer text.
    _, case = await _exported()
    await _ask(case.prompt)
    result = await humanjudge_arena.download_arena_verdicts(FakeArena([_vote()]).transport)
    assert (result.unmatched, await _matched_answer_of_only_vote()) == (1, None)


@ON
async def test_a_vote_naming_a_model_we_never_answered_as_is_not_credited_to_another(
    backends,
):
    _, case = await _exported()
    response = await _ask(case.prompt)
    vote = _vote(model_name="someone-else", output=_letter(response))
    await humanjudge_arena.download_arena_verdicts(FakeArena([vote]).transport)
    assert await _matched_answer_of_only_vote() is None


@ON
async def test_answer_text_that_fits_two_answers_leaves_the_vote_unmatched(backends):
    # Two cases, one letter: the vote's text cannot say which it judged.
    _, first = await _exported()
    _, second = await _exported(context={**CONTEXT, "procedure": "CT"})
    await _ask(first.prompt, model="outside-model")
    await _ask(second.prompt, model="outside-model")
    vote = _vote(model_name="outside-model", output=LETTER.strip())
    await humanjudge_arena.download_arena_verdicts(FakeArena([vote]).transport)
    assert await _matched_answer_of_only_vote() is None


@ON
async def test_answer_text_that_fits_no_answer_is_not_matched_by_case_and_model(
    backends,
):
    _, case = await _exported()
    await _ask(case.prompt)
    vote = _vote(input=case.prompt, output="An earlier letter, since replaced.")
    await humanjudge_arena.download_arena_verdicts(FakeArena([vote]).transport)
    assert await _matched_answer_of_only_vote() is None


@ON
async def test_id_less_votes_told_apart_only_by_their_aliases_are_all_stored(
    backends,
):
    votes = [
        {"model_name": "fhi-local", "vote": "pass", "voter_id": "rev-1"},
        {"model_name": "fhi-local", "vote": "flag", "voter_id": "rev-2"},
    ]
    await humanjudge_arena.download_arena_verdicts(FakeArena(votes).transport)
    assert await HumanJudgeArenaVerdict.objects.acount() == 2


async def test_download_refuses_when_no_arena_is_configured():
    with pytest.raises(humanjudge_arena.HumanJudgeError):
        await humanjudge_arena.download_arena_verdicts()


async def test_request_template_leaves_the_prompt_placeholder_for_humanjudge():
    body = json.loads(humanjudge_arena.request_template("fhi-local"))
    assert body["messages"][0]["content"] == "{{prompt}}"


async def test_reviewer_instructions_fit_humanjudges_fields():
    assert all(len(t) <= 2000 for t in humanjudge_arena.REVIEWER_INSTRUCTIONS.values())


@ON
async def test_a_slug_two_models_share_leaves_a_vote_giving_only_it_unmatched(
    backends,
):
    _, case = await _exported()
    for name in ("org/model", "org-model"):
        await HumanJudgeArenaAnswer.objects.acreate(
            case=case,
            prompt_sha256=case.prompt_sha256,
            model_name=name,
            content=f"Letter by {name}",
            content_sha256=humanjudge_arena.prompt_digest(f"Letter by {name}"),
            source="generated",
        )
    vote = _vote(model_name="Org Model", input=case.prompt)
    await humanjudge_arena.download_arena_verdicts(FakeArena([vote]).transport)
    assert await _matched_answer_of_only_vote() is None


@ON
async def test_id_less_votes_on_different_models_are_both_stored(backends):
    base = {"vote": "flag", "voter_id": "rev-1", "timestamp": "2026-11-10T12:00:00Z"}
    votes = [{**base, "model_name": "fhi-local"}, {**base, "model_name": "outside-model"}]
    await humanjudge_arena.download_arena_verdicts(FakeArena(votes).transport)
    assert await HumanJudgeArenaVerdict.objects.acount() == 2


@ON
async def test_a_model_slug_equal_to_one_stored_name_is_not_taken_as_that_name(
    backends,
):
    _, case = await _exported()
    for name in ("org/model", "org-model"):
        await HumanJudgeArenaAnswer.objects.acreate(
            case=case,
            prompt_sha256=case.prompt_sha256,
            model_name=name,
            content=f"Letter by {name}",
            content_sha256=humanjudge_arena.prompt_digest(f"Letter by {name}"),
            source="generated",
        )
    vote = _vote(input=case.prompt)
    del vote["model_name"]
    vote["model_slug"] = "org-model"  # org/model's slug as much as org-model's
    await humanjudge_arena.download_arena_verdicts(FakeArena([vote]).transport)
    assert await _matched_answer_of_only_vote() is None

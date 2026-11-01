"""The Appeal Arena's synthetic chat drafts run the lookups they ask for
(chooser_chat_tools) and keep what each found, for the voter.

Every model is a fake and every lookup is mocked at the tool: no test here
reaches PubMed or any other service. The lookups are off in tests
(CHOOSER_CHAT_TOOLS) and each test that wants them turns them on.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from fighthealthinsurance import chooser_chat_tools, chooser_tasks
from fighthealthinsurance.chat.tools import PubMedTool
from fighthealthinsurance.chooser_chat_tools import answer_with_tools
from fighthealthinsurance.chooser_tasks import (
    _generate_chat_candidates,
    _merged_tool_calls,
)
from fighthealthinsurance.models import ChooserCandidate, ChooserTask

QUESTION = "Is there research showing semaglutide helps with obesity?"
HISTORY = [
    {"role": "user", "content": "My plan denied semaglutide for my obesity."},
    {"role": "assistant", "content": "I'm sorry. Let's look at an appeal."},
]
SEARCH = "**pubmed_query: semaglutide obesity**"
FOUND = "pubmedcontext:[Title: Semaglutide in adults with obesity\nAbstract: ...]"
ANSWER = (
    "Yes. A trial of semaglutide in adults with obesity found meaningful "
    "weight loss, which you can cite in your appeal."
)
TOOLS_ON = override_settings(CHOOSER_CHAT_TOOLS=True)


class ScriptedModel:
    """Answers each generate_chat_response with ``reply(message)`` and
    remembers what it was asked."""

    def __init__(self, reply, name="fake-model"):
        self.reply = reply
        self.name = name
        self.asked = []

    def __str__(self):
        return self.name

    @property
    def external(self):
        return False

    @property
    def context_only(self):
        return False

    def quality(self):
        return 100

    async def generate_chat_response(
        self,
        current_message_for_llm,
        previous_context_summary=None,
        history=None,
        is_professional=True,
        is_logged_in=True,
        **kwargs,
    ):
        self.asked.append((current_message_for_llm, list(history or [])))
        return self.reply(current_message_for_llm), None


def _searches_then_answers(message):
    return ANSWER if message.startswith("pubmedcontext") else SEARCH


def _pubmed(found=FOUND, search=None):
    """PubMedTool with its searches mocked: ``found`` is the block it
    builds (empty for a search that finds nothing)."""
    return patch.multiple(
        PubMedTool,
        _normalize_drug_terms=AsyncMock(side_effect=lambda q: q),
        _search_articles=search or AsyncMock(return_value=["123"] if found else []),
        _build_article_context=AsyncMock(return_value=found),
    )


@pytest.mark.asyncio
class TestAnswerWithTools:
    async def test_a_search_is_run_and_the_model_answers_from_what_it_found(self):
        model = ScriptedModel(_searches_then_answers)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert answer.text == ANSWER

    async def test_the_search_and_what_it_found_are_kept_for_the_voter(self):
        model = ScriptedModel(_searches_then_answers)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        [call] = answer.tool_calls
        assert (call["tool"], call["call"], call["found"]) == (
            "PubMed",
            "semaglutide obesity",
            FOUND,
        )

    async def test_the_tools_status_lines_are_kept_as_notes(self):
        model = ScriptedModel(_searches_then_answers)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert any("Searching PubMed" in n for n in answer.tool_calls[0]["notes"])

    async def test_the_follow_up_sees_the_question_it_searched_for(self):
        model = ScriptedModel(_searches_then_answers)
        with TOOLS_ON, _pubmed():
            await answer_with_tools(model, QUESTION, HISTORY, 50)
        found_message, history = model.asked[-1]
        assert found_message == FOUND
        assert history[-2:] == [
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": SEARCH},
        ]

    async def test_a_reply_without_a_lookup_is_kept_as_written(self):
        model = ScriptedModel(lambda _m: ANSWER)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert (answer.text, answer.tool_calls, len(model.asked)) == (ANSWER, [], 1)

    async def test_with_the_tools_off_nothing_is_looked_up(self):
        model = ScriptedModel(_searches_then_answers)
        search = AsyncMock(return_value=["123"])
        with _pubmed(search=search):
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert (answer.text, answer.tool_calls, search.await_count) == (
            SEARCH,
            [],
            0,
        )

    async def test_a_search_that_finds_nothing_gets_an_answer_without_it(self):
        def reply(message):
            return ANSWER if "found nothing useful" in message else SEARCH

        model = ScriptedModel(reply)
        with TOOLS_ON, _pubmed(found=""):
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert (answer.text, answer.tool_calls[0]["found"]) == (ANSWER, "")

    async def test_lookups_stop_after_the_depth_limit(self):
        # A model that searches again whatever it is shown.
        model = ScriptedModel(lambda _m: "Let me check. " + SEARCH)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 5)
        assert len(answer.tool_calls) == chooser_chat_tools.MAX_TOOL_DEPTH

    async def test_no_tool_syntax_is_left_after_the_depth_limit(self):
        model = ScriptedModel(lambda _m: "Let me check. " + SEARCH)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 5)
        assert "pubmed_query" not in answer.text

    async def test_a_tool_that_is_never_run_is_stripped(self):
        fetch = 'Reading it now. **fetch_doc {"url": "https://example.com/p.pdf"}**'
        model = ScriptedModel(lambda _m: ANSWER + "\n" + fetch)
        with TOOLS_ON, _pubmed():
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert "fetch_doc" not in answer.text

    async def test_a_lookup_that_times_out_is_marked_and_dropped(self):
        async def slow(_query):
            await asyncio.sleep(5)
            return ["123"]

        model = ScriptedModel(
            lambda m: ANSWER if "found nothing useful" in m else SEARCH
        )
        with TOOLS_ON, _pubmed(search=AsyncMock(side_effect=slow)), patch.object(
            chooser_chat_tools, "CHOOSER_CHAT_TOOL_TIMEOUT", 0.05
        ):
            answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
        assert (answer.text, answer.tool_calls[0].get("timed_out")) == (ANSWER, True)


def test_the_synthesized_draft_carries_each_lookup_once():
    search = {"tool": "PubMed", "call": "semaglutide", "found": FOUND, "notes": []}
    other = {"tool": "USPSTF Lookup", "call": "{}", "found": "x", "notes": []}
    drafts = [
        ChooserCandidate(metadata={"tool_calls": [search]}),
        ChooserCandidate(metadata={"tool_calls": [dict(search), other]}),
        ChooserCandidate(metadata={}),
    ]
    assert [(c["tool"], c["call"]) for c in _merged_tool_calls(drafts)] == [
        ("PubMed", "semaglutide"),
        ("USPSTF Lookup", "{}"),
    ]


def test_the_synthesized_drafts_lookups_are_marked_as_the_drafts():
    search = {"tool": "PubMed", "call": "semaglutide", "found": FOUND, "notes": []}
    drafts = [ChooserCandidate(metadata={"tool_calls": [search]})]
    assert _merged_tool_calls(drafts)[0]["from_drafts"] is True


def test_a_timed_out_lookup_does_not_hide_the_same_search_that_finished():
    timed_out = {"tool": "PubMed", "call": "q", "found": "", "timed_out": True}
    finished = {"tool": "PubMed", "call": "q", "found": FOUND}
    drafts = [
        ChooserCandidate(metadata={"tool_calls": [timed_out]}),
        ChooserCandidate(metadata={"tool_calls": [finished]}),
    ]
    assert _merged_tool_calls(drafts)[0]["found"] == FOUND


def test_a_later_timed_out_lookup_does_not_replace_the_finished_one():
    finished = {"tool": "PubMed", "call": "q", "found": FOUND}
    timed_out = {"tool": "PubMed", "call": "q", "found": "", "timed_out": True}
    drafts = [
        ChooserCandidate(metadata={"tool_calls": [finished]}),
        ChooserCandidate(metadata={"tool_calls": [timed_out]}),
    ]
    assert _merged_tool_calls(drafts)[0]["found"] == FOUND


def test_findings_from_a_timed_out_lookup_beat_an_empty_finished_one():
    timed_out = {"tool": "PubMed", "call": "q", "found": FOUND, "timed_out": True}
    finished = {"tool": "PubMed", "call": "q", "found": ""}
    drafts = [
        ChooserCandidate(metadata={"tool_calls": [finished]}),
        ChooserCandidate(metadata={"tool_calls": [timed_out]}),
    ]
    assert _merged_tool_calls(drafts)[0]["found"] == FOUND


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestChatCandidatesKeepTheirLookups:
    async def _task(self):
        task = await ChooserTask.objects.acreate(
            task_type="chat",
            status="QUEUED",
            source="synthetic",
            context_json={"prompt": QUESTION, "history": HISTORY},
        )
        writer = ScriptedModel(lambda _m: "")
        writer._infer_no_context = AsyncMock(
            return_value=f"USER: {HISTORY[0]['content']}\nASSISTANT: ok\nUSER: {QUESTION}"
        )
        backends = [ScriptedModel(_searches_then_answers, name=f"m{i}") for i in (1, 2)]
        with TOOLS_ON, _pubmed(), patch.object(
            chooser_tasks.ml_router,
            "generate_text_backends",
            MagicMock(return_value=[writer]),
        ), patch.object(
            chooser_tasks, "_comparable_backends", MagicMock(return_value=backends)
        ), patch.object(
            chooser_tasks, "_maybe_add_synthesized_candidate", new=AsyncMock()
        ):
            await _generate_chat_candidates(task)
        return task

    async def test_each_draft_stores_the_lookups_it_made(self):
        task = await self._task()
        calls = [
            c.metadata.get("tool_calls")
            async for c in ChooserCandidate.objects.filter(task=task)
        ]
        assert [[c["call"] for c in cs] for cs in calls] == [
            ["semaglutide obesity"],
            ["semaglutide obesity"],
        ]

    async def test_each_draft_is_the_answer_not_the_search(self):
        task = await self._task()
        contents = {
            c.content async for c in ChooserCandidate.objects.filter(task=task)
        }
        assert contents == {ANSWER}


@pytest.mark.django_db
def test_the_next_task_serves_each_drafts_lookups():
    task = ChooserTask.objects.create(
        task_type="chat",
        status="READY",
        source="synthetic",
        context_json={"prompt": QUESTION, "history": []},
    )
    search = {"tool": "PubMed", "call": "semaglutide", "found": FOUND, "notes": []}
    for index, metadata in enumerate(
        [{"source": "synthetic", "tool_calls": [search]}, {"source": "synthetic"}]
    ):
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=index,
            kind="chat_response",
            model_name=f"m{index}",
            content=ANSWER + str(index),
            metadata=metadata,
        )
    served = APIClient().get(reverse("chooser-next-chat")).json()["candidates"]
    assert sorted((c["tool_calls"] for c in served), key=len) == [[], [search]]


def test_stripping_an_action_call_keeps_the_prose_after_it():
    text = (
        "I'll save that.\n"
        '**create_or_update_appeal** {"diagnosis": "obesity"}\n'
        "Meanwhile, ask your doctor for a letter of medical necessity."
    )
    stripped = chooser_chat_tools.strip_tool_syntax(text)
    assert (
        "create_or_update_appeal" not in stripped
        and stripped.endswith("letter of medical necessity.")
    )


def test_a_reply_that_is_only_a_call_strips_to_nothing():
    text = "**pubmed_query: semaglutide obesity randomized controlled trial outcomes**"
    assert chooser_chat_tools.strip_tool_syntax(text) == ""


def test_a_financial_assistance_call_is_stripped_with_its_payload():
    text = (
        "Here are some options.\n"
        '**financial_assistance {"drug": "Wegovy", "state": "CA"}**'
    )
    assert chooser_chat_tools.strip_tool_syntax(text) == "Here are some options."


@pytest.mark.asyncio
async def test_a_financial_assistance_lookup_records_what_it_asked_for():
    call = '**financial_assistance {"drug": "Wegovy", "state": "CA"}**'
    model = ScriptedModel(lambda m: ANSWER if "Wegovy" in m and m != call else call)
    with TOOLS_ON, patch(
        "fighthealthinsurance.chat.tools.financial_assistance_tool."
        "FinancialAssistanceTool.execute",
        AsyncMock(return_value=(ANSWER, "")),
    ) as execute:
        answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
    assert (execute.await_count, answer.tool_calls[0]["call"]) == (
        1,
        '{"drug": "Wegovy", "state": "CA"}',
    )


def test_the_medicaid_tools_get_the_state_their_handlers_index():
    """MedicaidEligibilityTool sets eligibility_computed[0] and
    MedicaidGovLookupTool reads lookup_count[0]: an empty list raised
    IndexError inside the handler, which swallowed the lookup."""
    from fighthealthinsurance.chat.tools import (
        MedicaidEligibilityTool,
        MedicaidGovLookupTool,
    )

    built = {
        kind: build(AsyncMock(), AsyncMock())
        for kind, build in chooser_chat_tools._TOOLS
    }
    assert (
        built[MedicaidEligibilityTool].eligibility_computed,
        built[MedicaidGovLookupTool]._lookup_count,
    ) == ([False], [0])


@pytest.mark.asyncio
async def test_a_short_answer_after_a_lookup_that_found_something_is_not_retried():
    def reply(message):
        if "found nothing useful" in message:
            return ANSWER
        return "Thanks." if message.startswith("pubmedcontext") else SEARCH

    model = ScriptedModel(reply)
    with TOOLS_ON, _pubmed():
        answer = await answer_with_tools(model, QUESTION, HISTORY, 50)
    assert not any("found nothing useful" in m for m, _ in model.asked)

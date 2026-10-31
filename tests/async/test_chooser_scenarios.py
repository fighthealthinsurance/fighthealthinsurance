"""Appeal scenarios in the chooser: seeded from the real-condition pool,
with case facts and the evidence already on file. Every model and every
outside service is a mock; a live PubMed search is off in tests
(CHOOSER_PUBMED_LIVE_SEARCH) and asserted never to run."""

import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from fighthealthinsurance.chooser_tasks import (
    _cached_pmids,
    _evidence_for,
    _generate_appeal_candidates,
    _generate_single_task,
)
from fighthealthinsurance.condition_rankings import SeedPair
from fighthealthinsurance.ml import research_judging
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    PubMedArticleJudgment,
    PubMedArticleSummarized,
    PubMedMiniArticle,
    PubMedQueryData,
)

_LETTER = "Dear Reviewer, " + "this care is medically necessary. " * 10


def _writer(scenario, *replies):
    """A backend that writes ``scenario``, then each of ``replies`` (the
    interview's answers, say), then letters."""
    model = MagicMock()
    model.name = "model-a"
    model.external = False
    model._infer_no_context = AsyncMock(
        side_effect=[scenario, *replies] + [_LETTER] * 10
    )
    return model


async def _cached_search(query, pmids, age_minutes=0):
    """A cached search row; ``age_minutes`` sets its age explicitly, since
    two rows created back to back can share a millisecond timestamp."""
    row = await PubMedQueryData.objects.acreate(query=query, articles=json.dumps(pmids))
    await PubMedQueryData.objects.filter(pk=row.pk).aupdate(
        created=timezone.now() - datetime.timedelta(minutes=age_minutes)
    )


async def _article(pmid, title, url=None, abstract=None):
    await PubMedMiniArticle.objects.acreate(
        pmid=pmid, title=title, article_url=url, abstract=abstract
    )


def _no_live_search():
    return patch(
        "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query",
        AsyncMock(side_effect=AssertionError("live PubMed search ran")),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestEvidenceFor:
    async def test_cached_searches_are_reused_newest_first(self):
        await _cached_search("MRI Lower back pain", ["1", "2"], age_minutes=10)
        await _cached_search("mri lower back pain", ["3"], age_minutes=1)
        await _article("1", "Imaging for low back pain", "https://example.org/1")
        await _article("2", "")  # no title: never shown
        await _article("3", "A newer study")

        with _no_live_search():
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert evidence == [
            {"pmid": "3", "title": "A newer study", "url": "https://pubmed.ncbi.nlm.nih.gov/3/"},
            {"pmid": "1", "title": "Imaging for low back pain", "url": "https://example.org/1"},
        ]

    async def test_an_article_is_summarized_by_its_abstract(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="  Imaging   helped. ")

        with _no_live_search():
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert evidence[0]["summary"] == "Imaging helped."

    async def test_the_summary_real_appeals_got_is_preferred(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="The abstract.")
        await PubMedArticleSummarized.objects.acreate(
            pmid="1", basic_summary="What appeals were told."
        )

        with _no_live_search():
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert evidence[0]["summary"] == "What appeals were told."

    async def test_a_long_abstract_is_cut_at_a_boundary(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging", abstract="A finding was made. " * 100)

        with _no_live_search():
            summary = (await _evidence_for("MRI", "Lower back pain"))[0]["summary"]

        assert len(summary) <= 600
        assert summary.endswith("...")

    async def test_an_article_with_nothing_to_summarize_has_no_summary(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain")

        with _no_live_search():
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert "summary" not in evidence[0]

    async def test_nothing_cached_and_no_live_search_means_no_evidence(self):
        with _no_live_search():
            assert await _evidence_for("MRI", "Lower back pain") == []

    @override_settings(CHOOSER_PUBMED_LIVE_SEARCH=True)
    async def test_a_cached_search_is_used_before_a_live_one(self):
        """With live search allowed, a pair that was searched before still
        costs no NCBI call."""
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain")
        assert await _cached_pmids("MRI Lower back pain") == ["1"]

        with _no_live_search():
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert [e["pmid"] for e in evidence] == ["1"]

    @override_settings(CHOOSER_PUBMED_LIVE_SEARCH=True)
    async def test_live_search_runs_only_when_nothing_is_cached(self):
        await _article("9", "Found live")
        search = AsyncMock(return_value=["9"])
        with patch(
            "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query",
            search,
        ):
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert [e["pmid"] for e in evidence] == ["9"]
        assert search.await_args.args[0] == "MRI Lower back pain"


    @override_settings(CHOOSER_PUBMED_LIVE_SEARCH=True)
    async def test_live_search_results_get_titles_fetched(self):
        """A live search stores only PMIDs; the articles it finds still
        reach the evidence list, with their titles fetched."""
        search = AsyncMock(return_value=["111", "222"])
        fetched = {
            "111": SimpleNamespace(title="Live study one", abstract="a"),
            "222": SimpleNamespace(title="Live study two", abstract="b"),
        }
        with patch(
            "fighthealthinsurance.pubmed_tools.PubMedTools.find_pubmed_article_ids_for_query",
            search,
        ), patch(
            "fighthealthinsurance.utils.pubmed_fetcher.article_by_pmid",
            side_effect=fetched.get,
        ):
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert [(e["pmid"], e["title"]) for e in evidence] == [
            ("111", "Live study one"),
            ("222", "Live study two"),
        ]


    @override_settings(CHOOSER_PUBMED_LIVE_SEARCH=True)
    async def test_titles_a_fetch_missed_are_fetched_on_the_next_use(self):
        """A cached search whose titles never arrived (the fetch timed out)
        gets them on a later case, with no new search."""
        await _cached_search("MRI Lower back pain", ["333"])
        with _no_live_search(), patch(
            "fighthealthinsurance.utils.pubmed_fetcher.article_by_pmid",
            return_value=SimpleNamespace(title="Fetched later", abstract=""),
        ):
            evidence = await _evidence_for("MRI", "Lower back pain")

        assert [(e["pmid"], e["title"]) for e in evidence] == [("333", "Fetched later")]


# Research judging on (its key and switch), and the arena allowed to use it.
JUDGING_ON = dict(
    TYPESAFE_API_KEY="test-key",
    TYPESAFE_RESEARCH_JUDGING_ENABLED=True,
    CHOOSER_JUDGE_EVIDENCE=True,
)


def _jev_answer(on_topic=0.9, supports=0.8, undermines=0.1):
    """What TypeSafe answers for one article (research_judging.parse)."""
    return {
        "model": "jev-1.13.0",
        "answers": {
            "on_topic": {"type": "noul", "noul": on_topic},
            "supports": {"type": "noul", "noul": supports},
            "undermines": {"type": "noul", "noul": undermines},
        },
        "usage": {"input_tokens": 300},
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestEvidenceJudging:
    """Jev's verdict on a case's evidence: asked for as the case is built,
    stored for the pair (so the voter's page and the next case read it),
    and never put on the case, which is what the drafts are given. Every
    request is stubbed at research_judging._post; the switches are off in
    tests and each case turns them on."""

    async def _evidence(self, post):
        with _no_live_search(), patch.object(research_judging, "_post", post):
            return await _evidence_for("MRI", "Lower back pain")

    async def test_the_verdict_is_stored_for_the_pair_and_kept_off_the_case(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="Imaging helped.")
        post = AsyncMock(return_value=_jev_answer(supports=0.8))

        with override_settings(**JUDGING_ON):
            evidence = await self._evidence(post)

        assert evidence == [
            {
                "pmid": "1",
                "title": "Imaging for low back pain",
                "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
                "summary": "Imaging helped.",
            }
        ]
        stored = await PubMedArticleJudgment.objects.aget(pmid="1")
        assert stored.treatment_key == research_judging.treatment_key(
            "MRI", "Lower back pain"
        )
        assert stored.supports == pytest.approx(0.8)
        # The article and the pair are what went; the case has nothing else.
        state, questions = post.await_args.args[0], post.await_args.args[1]
        assert "Imaging helped." in state
        assert '"MRI"' in questions["supports"]["instructions"]
        assert '"Lower back pain"' in questions["supports"]["instructions"]

    async def test_a_verdict_already_stored_for_the_pair_is_not_asked_again(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="Imaging helped.")
        await PubMedArticleJudgment.objects.acreate(
            pmid="1",
            treatment_key=research_judging.treatment_key("MRI", "Lower back pain"),
            on_topic=0.9,
            supports=0.7,
            undermines=0.1,
            scorer=research_judging.SCORER,
        )
        post = AsyncMock(side_effect=AssertionError("Jev was asked again"))

        with override_settings(**JUDGING_ON):
            evidence = await self._evidence(post)

        assert [e["pmid"] for e in evidence] == ["1"]
        post.assert_not_awaited()

    async def test_an_abstract_an_appeal_stored_stands_in_for_a_missing_one(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain")
        await PubMedArticleSummarized.objects.acreate(
            pmid="1", abstract="The abstract an appeal fetched."
        )
        post = AsyncMock(return_value=_jev_answer())

        with override_settings(**JUDGING_ON):
            await self._evidence(post)

        assert "The abstract an appeal fetched." in post.await_args.args[0]

    async def test_the_arena_switch_keeps_the_case_off_jev(self):
        """Research judging on for real appeals, the arena told not to use
        it: nothing is sent, and the case is built as before."""
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="Imaging helped.")
        post = AsyncMock(return_value=_jev_answer())

        with override_settings(**dict(JUDGING_ON, CHOOSER_JUDGE_EVIDENCE=False)):
            evidence = await self._evidence(post)

        assert [e["pmid"] for e in evidence] == ["1"]
        post.assert_not_awaited()

    async def test_research_judging_off_sends_nothing(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="Imaging helped.")
        post = AsyncMock(return_value=_jev_answer())

        with override_settings(CHOOSER_JUDGE_EVIDENCE=True):
            await self._evidence(post)

        post.assert_not_awaited()

    async def test_a_failing_judge_leaves_the_case_its_evidence(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain", abstract="Imaging helped.")
        post = AsyncMock(side_effect=RuntimeError("TypeSafe is down"))

        with override_settings(**JUDGING_ON):
            evidence = await self._evidence(post)

        assert [e["pmid"] for e in evidence] == ["1"]
        assert await PubMedArticleJudgment.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestSeededScenario:
    async def _generate(self, scenario, seed):
        task = await ChooserTask.objects.acreate(task_type="appeal", status="QUEUED")
        model = _writer(scenario)
        with patch("fighthealthinsurance.chooser_tasks.ml_router") as router, patch(
            "fighthealthinsurance.chooser_tasks._maybe_add_synthesized_candidate",
            AsyncMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks._pick_seed_pair",
            AsyncMock(return_value=seed),
        ), _no_live_search():
            router.generate_text_backends.return_value = [model]
            await _generate_appeal_candidates(task)
        await task.arefresh_from_db()
        return task, model

    async def test_the_seed_is_the_case_whatever_the_writer_echoed(self):
        await _cached_search("MRI Lower back pain", ["1"])
        await _article("1", "Imaging for low back pain")
        scenario = (
            "Procedure: Magnetic resonance imaging\nDiagnosis: lumbago\n"
            "Insurance Company: Fictional Mutual\nDenial Reason: not medically necessary.\n"
            "Case Facts: Patient in their 40s; eight months of symptoms; physical therapy failed.\n"
        )

        task, model = await self._generate(scenario, SeedPair("MRI", "Lower back pain", 8))

        assert task.status != "DISABLED"
        assert task.context_json["procedure"] == "MRI"
        assert task.context_json["diagnosis"] == "Lower back pain"
        assert task.context_json["seeded"] is True
        assert task.context_json["facts"] == [
            "Patient in their 40s",
            "eight months of symptoms",
            "physical therapy failed",
        ]
        assert [e["pmid"] for e in task.context_json["evidence"]] == ["1"]
        # The writer was told the pair, and the drafts were told the facts
        # and the evidence.
        scenario_prompt = model._infer_no_context.await_args_list[0].kwargs["prompt"]
        assert "MRI for Lower back pain" in scenario_prompt
        draft_prompt = model._infer_no_context.await_args_list[1].kwargs["prompt"]
        assert "- Patient in their 40s" in draft_prompt
        assert "Imaging for low back pain (PMID 1)" in draft_prompt
        assert await ChooserCandidate.objects.filter(task=task).acount() >= 1

    async def test_without_a_seed_the_writer_invents_the_pair(self):
        scenario = (
            "Procedure: Knee arthroscopy\nDiagnosis: Torn meniscus\n"
            "Insurance Company: Fictional Mutual\nDenial Reason: experimental.\n"
        )

        task, _ = await self._generate(scenario, None)

        assert task.context_json["procedure"] == "Knee arthroscopy"
        assert task.context_json["seeded"] is False
        assert "facts" not in task.context_json
        assert task.context_json["evidence"] == []

    async def test_a_seeded_scenario_still_needs_the_insurer_and_reason(self):
        task, _ = await self._generate("Procedure: MRI\nDiagnosis: pain\n", SeedPair("MRI", "Lower back pain", 8))

        assert task.status == "DISABLED"


_SCENARIO = (
    "Procedure: MRI\nDiagnosis: Lower back pain\n"
    "Insurance Company: Fictional Mutual\nDenial Reason: not medically necessary.\n"
    "Case Facts: Patient in their 40s; eight months of symptoms.\n"
)
_QUESTIONS = [
    ("Has the patient tried physical therapy?", "UNKNOWN"),
    ("How long have the symptoms lasted?", ""),
]
_ANSWERS = "A1: Yes, eight weeks, with no change.\nA2: About eight months.\n"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestInterview:
    """The simulated question-and-answer session: the app's own question
    generators (mocked) ask, a scenario writer answers as the patient. The
    switch is off in tests (hermetic); each case turns it on."""

    async def _generate(self, specific, *replies, generic=None, others=()):
        task = await ChooserTask.objects.acreate(task_type="appeal", status="QUEUED")
        model = _writer(_SCENARIO, *replies)
        specific_mock = AsyncMock(return_value=specific)
        generic_mock = AsyncMock(return_value=generic)
        with patch("fighthealthinsurance.chooser_tasks.ml_router") as router, patch(
            "fighthealthinsurance.chooser_tasks._maybe_add_synthesized_candidate",
            AsyncMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks._pick_seed_pair",
            AsyncMock(return_value=None),
        ), patch.object(
            MLAppealQuestionsHelper, "generate_specific_questions", specific_mock
        ), patch.object(
            MLAppealQuestionsHelper, "generate_generic_questions", generic_mock
        ), _no_live_search(), override_settings(CHOOSER_SIMULATED_QA=True):
            router.generate_text_backends.return_value = [model, *others]
            await _generate_appeal_candidates(task)
        await task.arefresh_from_db()
        return task, model, specific_mock, generic_mock

    async def test_the_answers_reach_the_case_and_every_draft(self):
        task, model, specific, _ = await self._generate(_QUESTIONS, _ANSWERS)

        assert task.context_json["qa_pairs"] == [
            {
                "question": "Has the patient tried physical therapy?",
                "answer": "Yes, eight weeks, with no change.",
            },
            {"question": "How long have the symptoms lasted?", "answer": "About eight months."},
        ]
        # The question generator saw the case as a denial and its facts as
        # the patient's history, and was allowed outside models.
        kwargs = specific.await_args.kwargs
        assert kwargs["denial_text"].startswith("Fictional Mutual denied coverage of MRI")
        assert kwargs["patient_context"] == "Patient in their 40s; eight months of symptoms"
        assert kwargs["use_external"] is True
        interview_prompt = model._infer_no_context.await_args_list[1].kwargs["prompt"]
        assert "Q1: Has the patient tried physical therapy?" in interview_prompt
        draft_prompt = model._infer_no_context.await_args_list[2].kwargs["prompt"]
        assert "A: About eight months." in draft_prompt
        assert await ChooserCandidate.objects.filter(task=task).acount() >= 1

    async def test_every_backend_drafts_from_the_answers(self):
        """Each backend asked for a draft is given the patient's answers, not
        only the one that also wrote the case."""
        other = MagicMock()
        other.name = "model-b"
        other.external = True
        other._infer_no_context = AsyncMock(return_value=_LETTER)

        task, model, _, _ = await self._generate(_QUESTIONS, _ANSWERS, others=[other])

        for backend in (model, other):
            drafts = [
                call.kwargs["prompt"]
                for call in backend._infer_no_context.await_args_list
                if call.kwargs["prompt"].startswith("Write an appeal letter")
            ]
            assert drafts, backend.name
            assert all("A: About eight months." in p for p in drafts), backend.name
        names = [c.model_name async for c in ChooserCandidate.objects.filter(task=task)]
        assert sorted(names) == ["model-a", "model-b"]

    async def test_generic_questions_stand_in_when_no_specific_ones_come_back(self):
        task, _, _, generic = await self._generate(None, _ANSWERS, generic=_QUESTIONS)

        assert generic.await_args.kwargs["procedure"] == "MRI"
        assert len(task.context_json["qa_pairs"]) == 2

    async def test_the_generic_stand_in_is_allowed_outside_models(self):
        """Synthetic case, like the specific questions: without outside
        models the generic set had only the appeal fine-tune to ask."""
        _, _, _, generic = await self._generate(None, _ANSWERS, generic=_QUESTIONS)

        assert generic.await_args.kwargs["use_external"] is True

    async def test_junk_and_repeated_questions_are_not_asked(self):
        questions = _QUESTIONS + [
            ("Has the patient tried physical therapy?", ""),
            ("[Question]?", ""),
        ]
        _, model, _, _ = await self._generate(questions, _ANSWERS)

        interview_prompt = model._infer_no_context.await_args_list[1].kwargs["prompt"]
        assert "Q3:" not in interview_prompt

    async def test_no_questions_means_no_interview_and_no_answer_call(self):
        task, model, _, _ = await self._generate([], generic=[])

        assert "qa_pairs" not in task.context_json
        # Scenario, then straight to the drafts.
        draft_prompt = model._infer_no_context.await_args_list[1].kwargs["prompt"]
        assert draft_prompt.startswith("Write an appeal letter")

    async def test_answers_in_the_wrong_shape_leave_the_case_without_them(self):
        task, _, _, _ = await self._generate(_QUESTIONS, "I would rather not say.")

        assert "qa_pairs" not in task.context_json
        assert task.status != "DISABLED"

    async def test_a_failing_question_generator_does_not_stop_the_case(self):
        task = await ChooserTask.objects.acreate(task_type="appeal", status="QUEUED")
        model = _writer(_SCENARIO)
        with patch("fighthealthinsurance.chooser_tasks.ml_router") as router, patch(
            "fighthealthinsurance.chooser_tasks._maybe_add_synthesized_candidate",
            AsyncMock(),
        ), patch(
            "fighthealthinsurance.chooser_tasks._pick_seed_pair",
            AsyncMock(return_value=None),
        ), patch.object(
            MLAppealQuestionsHelper,
            "generate_specific_questions",
            AsyncMock(side_effect=RuntimeError("down")),
        ), _no_live_search(), override_settings(CHOOSER_SIMULATED_QA=True):
            router.generate_text_backends.return_value = [model]
            await _generate_appeal_candidates(task)
        await task.arefresh_from_db()

        assert "qa_pairs" not in task.context_json
        assert await ChooserCandidate.objects.filter(task=task).acount() >= 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestCaseVersion:
    async def _generate(self, task_type):
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_appeal_candidates", AsyncMock()
        ), patch(
            "fighthealthinsurance.chooser_tasks._generate_chat_candidates", AsyncMock()
        ):
            await _generate_single_task(task_type)
        return await ChooserTask.objects.alatest("id")

    async def test_a_new_appeal_task_is_stamped_with_the_interview_pipeline(self):
        assert (await self._generate("appeal")).case_version == 2

    async def test_a_new_chat_task_keeps_the_first_version(self):
        assert (await self._generate("chat")).case_version == 1

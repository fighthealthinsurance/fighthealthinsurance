"""ml/research_judging.py and its use in pubmed_tools: Jev judging PubMed
articles against a denial's treatment.

Every request is stubbed at research_judging._post; nothing here reaches
TypeSafe. What matters most: only the article and the treatment are sent,
only with consent, a stored judgment is reused without a request, and an
article nobody judged keeps its place.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

from fighthealthinsurance.ml import research_judging as rj
from fighthealthinsurance.ml import spend, typesafe
from fighthealthinsurance.models import (
    Denial,
    ExternalServiceHealth,
    PubMedArticleJudgment,
    PubMedArticleSummarized,
)
from fighthealthinsurance.pubmed_tools import (
    PubMedTools,
    judge_missing,
    stored_judgments,
    stored_judgments_for,
)

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_RESEARCH_JUDGING_ENABLED=True)
TITLE = "Lumbar MRI for chronic low back pain: a randomized trial"
ABSTRACT = "We randomized 400 adults with chronic low back pain to MRI or usual care."


def _payload(on_topic=0.9, supports=0.8, undermines=0.1, model="jev-1.13.0"):
    return {
        "model": model,
        "answers": {
            "on_topic": {"type": "noul", "noul": on_topic},
            "supports": {"type": "noul", "noul": supports},
            "undermines": {"type": "noul", "noul": undermines},
        },
        "usage": {"input_tokens": 300},
    }


class TestQuestionsAndState:
    def test_the_state_is_the_article_alone(self):
        state = rj.build_state(TITLE, ABSTRACT)
        assert state.startswith("THE ARTICLE:")
        assert TITLE in state and ABSTRACT in state

    def test_the_questions_name_the_treatment_and_condition(self):
        questions = rj.build_questions("MRI lumbar spine", "low back pain")
        assert set(questions) == {"on_topic", "supports", "undermines"}
        assert all(q["type"] == "noul" for q in questions.values())
        assert '"MRI lumbar spine"' in questions["supports"]["instructions"]
        assert '"low back pain"' in questions["supports"]["instructions"]

    def test_without_a_condition_the_questions_name_the_treatment_only(self):
        questions = rj.build_questions("MRI lumbar spine", None)
        assert '"MRI lumbar spine"' in questions["supports"]["instructions"]
        assert 'for "' not in questions["supports"]["instructions"]

    def test_the_treatment_key_ignores_case_and_spacing(self):
        assert rj.treatment_key("MRI  Lumbar", "Back pain") == rj.treatment_key(
            "mri lumbar", "back   pain"
        )
        assert rj.treatment_key("MRI", "back pain") != rj.treatment_key("MRI", "neck pain")


class TestParse:
    def test_happy_path(self):
        judgment = rj.parse(_payload())
        assert judgment.on_topic == pytest.approx(0.9)
        assert judgment.scorer == rj.SCORER
        assert rj.same_rubric(judgment.scorer)

    @pytest.mark.parametrize(
        "payload",
        [
            {"answers": {}},
            {"answers": {"on_topic": {"noul": 0.5}, "supports": {"noul": 0.5}}},
            _payload(supports=1.5),
            _payload(undermines=True),
            {"nothing": 1},
        ],
    )
    def test_anything_odd_is_rejected(self, payload):
        with pytest.raises(rj.JudgingError):
            rj.parse(payload)

    def test_another_rubric_is_not_the_same(self):
        assert not rj.same_rubric("typesafe/jev-1.13.0/research-rubric-0")
        assert not rj.same_rubric("typesafe/jev-1.13.0/rubric-1")
        assert not rj.same_rubric("fhi/fhi-2025/research-rubric-0")


# What our own model is asked to reply (research_judging.build_own_model_prompt).
OWN_REPLY = "on_topic: 90\nsupports: 80\nundermines: 10"


def _own_model(reply=OWN_REPLY, name="fhi-2026-general"):
    model = MagicMock()
    model.name = name
    model._infer_no_context = AsyncMock(return_value=reply)
    return model


class TestOwnModel:
    """Our own model answering Jev's questions, for the Appeal Arena's
    articles Jev did not judge (chooser_tasks._judge_evidence)."""

    def test_the_prompt_is_the_article_and_jevs_three_questions(self):
        prompt = rj.build_own_model_prompt(TITLE, ABSTRACT, "MRI", "low back pain")
        assert prompt.startswith(rj.build_state(TITLE, ABSTRACT))
        for question in rj.build_questions("MRI", "low back pain").values():
            assert question["instructions"] in prompt

    @pytest.mark.parametrize(
        "reply",
        [
            OWN_REPLY,
            "<think>on_topic: 3, surely?</think>\non_topic: 90\nsupports: 80\nundermines: 10",
            "**on_topic**: 90%\n**supports**: 80%\n**undermines**: 10%",
            "- On topic: 90\n- Supports: 80\n- Undermines: 10.",
        ],
    )
    def test_parse_reads_the_three_likelihoods(self, reply):
        judgment = rj.parse_own_model(reply, scorer="fhi/m/research-rubric-1")
        assert (judgment.on_topic, judgment.supports, judgment.undermines) == (
            pytest.approx(0.9),
            pytest.approx(0.8),
            pytest.approx(0.1),
        )
        assert judgment.scorer == "fhi/m/research-rubric-1"

    @pytest.mark.parametrize(
        "reply",
        [
            None,
            "",
            "on_topic: 90\nsupports: 80",
            "on_topic: 0.9\nsupports: 0.8\nundermines: 0.1",
            "on_topic: 190\nsupports: 80\nundermines: 10",
            "on_topic: 90\non_topic: 20\nsupports: 80\nundermines: 10",
            "I cannot judge medical research.",
        ],
    )
    def test_parse_rejects_anything_odd(self, reply):
        with pytest.raises(rj.JudgingError):
            rj.parse_own_model(reply, scorer="fhi/m/research-rubric-1")

    def test_the_scorer_names_the_model_in_safe_characters(self):
        model = _own_model(name="totallylegitco/fhi model v0.5")
        scorer = rj.own_model_scorer(model)
        assert scorer == "fhi/totallylegitco-fhi-model-v0.5/research-rubric-1"
        assert rj.same_rubric(scorer)

    def test_happy_path_needs_no_jev_switch_and_sends_only_the_article(self):
        model = _own_model()
        judgment = asyncio.run(
            rj.judge_article_with(model, TITLE, ABSTRACT, "MRI", "low back pain")
        )
        assert judgment is not None and judgment.verdict == rj.VERDICT_SUPPORTS
        assert judgment.scorer == "fhi/fhi-2026-general/research-rubric-1"
        prompt = model._infer_no_context.await_args.kwargs["prompt"]
        assert prompt == rj.build_own_model_prompt(TITLE, ABSTRACT, "MRI", "low back pain")

    def test_nothing_to_read_or_ask_about_is_not_asked(self):
        model = _own_model()
        assert asyncio.run(rj.judge_article_with(model, TITLE, "", "MRI", "pain")) is None
        assert asyncio.run(rj.judge_article_with(model, TITLE, ABSTRACT, "", "pain")) is None
        model._infer_no_context.assert_not_awaited()

    @pytest.mark.parametrize("failure", [RuntimeError("backend down"), asyncio.TimeoutError()])
    def test_a_failing_model_is_none(self, failure):
        model = _own_model()
        model._infer_no_context = AsyncMock(side_effect=failure)
        assert asyncio.run(rj.judge_article_with(model, TITLE, ABSTRACT, "MRI", "pain")) is None

    def test_an_unreadable_reply_is_none(self):
        model = _own_model(reply="Looks supportive to me.")
        assert asyncio.run(rj.judge_article_with(model, TITLE, ABSTRACT, "MRI", "pain")) is None


class TestJudgeArticle:
    def test_off_by_default(self):
        with patch.object(rj, "_post") as post:
            assert asyncio.run(rj.judge_article(TITLE, ABSTRACT, "MRI", "pain")) is None
            post.assert_not_called()

    def test_nothing_to_read_or_ask_about_is_not_sent(self):
        with override_settings(**ENABLED), patch.object(rj, "_post") as post:
            assert asyncio.run(rj.judge_article(TITLE, "", "MRI", "pain")) is None
            assert asyncio.run(rj.judge_article(TITLE, ABSTRACT, "", "pain")) is None
            post.assert_not_called()

    def test_happy_path_sends_the_article_and_the_treatment(self):
        seen = {}

        async def fake_post(state, questions, timeout):
            seen["state"] = state
            seen["questions"] = questions
            return _payload()

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            judgment = asyncio.run(
                rj.judge_article(TITLE, ABSTRACT, "MRI lumbar spine", "low back pain")
            )
        assert judgment is not None and not judgment.drop
        assert seen["state"] == rj.build_state(TITLE, ABSTRACT)
        assert "MRI lumbar spine" in seen["questions"]["on_topic"]["instructions"]

    def test_a_failure_is_none_and_reported_by_status(self):
        seen = []

        async def fake_post(state, questions, timeout):
            raise typesafe.TypeSafeError("HTTP 500", status=500)

        async def note(summary):
            seen.append(summary)

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            assert (
                asyncio.run(rj.judge_article(TITLE, ABSTRACT, "MRI", "pain", on_failure=note))
                is None
            )
        assert seen == ["HTTP 500"]

    def test_a_spent_budget_is_skipped_not_failed(self):
        before = dict(rj.outcomes)

        async def fake_post(state, questions, timeout):
            raise typesafe.TypeSafeBudgetSpent("budget spent")

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            assert asyncio.run(rj.judge_article(TITLE, ABSTRACT, "MRI", "pain")) is None
        assert rj.outcomes["skipped"] == before["skipped"] + 1
        assert rj.outcomes["failed"] == before["failed"]

    def test_requests_count_against_the_research_use(self):
        ask = AsyncMock(return_value=_payload())
        with override_settings(**ENABLED), patch.object(typesafe, "ask", ask):
            asyncio.run(rj.judge_article(TITLE, ABSTRACT, "MRI", "pain"))
        assert ask.await_args.kwargs["use"] == spend.RESEARCH
        assert ask.await_args.kwargs["task"] == "research_judging"


class TestDropAndOrder:
    @pytest.mark.parametrize(
        "judgment,dropped",
        [
            (rj.Judgment(on_topic=0.1, supports=0.9, undermines=0.0), True),
            (rj.Judgment(on_topic=0.9, supports=0.2, undermines=0.8), True),
            # Mixed: argues both ways, still worth quoting.
            (rj.Judgment(on_topic=0.9, supports=0.6, undermines=0.7), False),
            (rj.Judgment(on_topic=0.9, supports=0.2, undermines=0.3), False),
        ],
    )
    def test_drop(self, judgment, dropped):
        assert judgment.drop is dropped

    @pytest.mark.parametrize(
        "judgment,verdict",
        [
            # Off topic comes first, however the other two read.
            (rj.Judgment(on_topic=0.1, supports=0.9, undermines=0.0), "off_topic"),
            (rj.Judgment(on_topic=0.9, supports=0.2, undermines=0.8), "against"),
            (rj.Judgment(on_topic=0.9, supports=0.6, undermines=0.7), "mixed"),
            (rj.Judgment(on_topic=0.9, supports=0.8, undermines=0.1), "supports"),
            # Neither for nor against with any confidence.
            (rj.Judgment(on_topic=0.9, supports=0.5, undermines=0.3), "unclear"),
            (rj.Judgment(on_topic=0.9, supports=0.2, undermines=0.3), "unclear"),
        ],
    )
    def test_verdict(self, judgment, verdict):
        assert judgment.verdict == verdict
        assert verdict in rj.VERDICTS
        # Dropping is the verdict read as a yes or no, never a third rule.
        assert judgment.drop is (verdict in ("off_topic", "against"))

    def test_as_dict_carries_the_verdict_and_rounded_probabilities_only(self):
        judgment = rj.Judgment(on_topic=0.912, supports=0.777, undermines=0.051)
        assert judgment.as_dict() == {
            "verdict": "supports",
            "on_topic": 0.91,
            "supports": 0.78,
            "undermines": 0.05,
        }

    def test_order_puts_support_first_and_unjudged_in_the_middle(self):
        articles = ["weak", "unjudged", "strong", "off", "strong"]
        judgments = {
            "weak": rj.Judgment(on_topic=0.9, supports=0.2, undermines=0.1),
            "strong": rj.Judgment(on_topic=0.9, supports=0.95, undermines=0.0),
            "off": rj.Judgment(on_topic=0.05, supports=0.0, undermines=0.0),
        }
        kept, dropped = rj.order(articles, judgments, pmid_of=lambda a: a)
        assert kept == ["strong", "unjudged", "weak"]
        assert dropped == ["off"]


class TestFocus:
    ON = rj.Judgment(on_topic=0.9, supports=0.8, undermines=0.1)
    MAYBE = rj.Judgment(on_topic=0.45, supports=0.8, undermines=0.1)

    def _focus(self, judgments, articles=("a", "b", "c")):
        return rj.focus(list(articles), judgments, pmid_of=lambda a: a)

    def test_two_on_topic_set_the_rest_aside(self):
        quoted, set_aside = self._focus({"a": self.ON, "b": self.MAYBE, "c": self.ON})
        assert (quoted, set_aside) == (["a", "c"], ["b"])

    def test_beside_two_on_topic_an_unjudged_one_is_set_aside_too(self):
        _, set_aside = self._focus({"a": self.ON, "b": self.ON})
        assert set_aside == ["c"]

    def test_one_on_topic_keeps_them_all(self):
        quoted, set_aside = self._focus({"a": self.ON, "b": self.MAYBE})
        assert (quoted, set_aside) == (["a", "b", "c"], [])

    def test_probably_off_topic_names_the_judged_ones_below_the_bar(self):
        assert rj.probably_off_topic(
            ["a", "b", "c"], {"a": self.ON, "b": self.MAYBE}, pmid_of=lambda a: a
        ) == {"b"}

    @pytest.mark.parametrize(
        "on_topic,probably_off", [(0.1, True), (0.45, True), (0.6, False), (0.9, False)]
    )
    def test_the_bar(self, on_topic, probably_off):
        judgment = rj.Judgment(on_topic=on_topic, supports=0.5, undermines=0.1)
        assert judgment.probably_off_topic is probably_off


class TestPubMedRelevanceNote:
    def _article(self, pmid="9", title="A trial"):
        return PubMedArticleSummarized(pmid=pmid, title=title, abstract=ABSTRACT)

    def test_a_probably_off_topic_article_carries_the_note(self):
        text = PubMedTools.format_article_short(
            self._article(), probably_off_topic=True
        )
        assert text.endswith(f"Relevance note: judged {rj.PROBABLY_OFF_TOPIC_NOTE}")

    def test_an_on_topic_article_has_no_note(self):
        assert "Relevance note" not in PubMedTools.format_article_short(self._article())

    def test_an_articles_own_summary_is_used_and_capped(self):
        article = self._article()
        article.basic_summary = "This study found MRI helped. " * 100
        text = PubMedTools.format_article_short(article)
        summary = text.split("Summary: ", 1)[1]
        assert summary.startswith("This study found MRI helped.")
        assert len(summary) <= 800


def _denial(use_external=True, **fields):
    return Denial.objects.create(
        hashed_email="h",
        denial_text="Denied: MRI not medically necessary.",
        use_external=use_external,
        procedure="MRI lumbar spine",
        diagnosis="low back pain",
        **fields,
    )


def _article(pmid, abstract=ABSTRACT):
    return PubMedArticleSummarized.objects.create(pmid=pmid, title=f"Title {pmid}", abstract=abstract)


@pytest.mark.django_db
class TestJudgeArticlesForADenial:
    def test_a_stored_judgment_is_reused_without_a_request(self):
        denial = _denial()
        article = _article("111")
        PubMedArticleJudgment.objects.create(
            pmid="111",
            treatment_key=rj.treatment_key(denial.procedure, denial.diagnosis),
            on_topic=0.9,
            supports=0.7,
            undermines=0.1,
            scorer=rj.SCORER,
        )
        with override_settings(**ENABLED), patch.object(rj, "_post") as post:
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        post.assert_not_called()
        assert judgments["111"].supports == pytest.approx(0.7)

    def test_switched_off_a_stored_judgment_is_not_used(self):
        denial = _denial()
        article = _article("444")
        PubMedArticleJudgment.objects.create(
            pmid="444",
            treatment_key=rj.treatment_key(denial.procedure, denial.diagnosis),
            on_topic=0.05,
            supports=0.0,
            undermines=0.0,
            scorer=rj.SCORER,
        )
        judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        assert judgments == {}

    def test_without_consent_nothing_is_sent(self):
        denial = _denial(use_external=False)
        article = _article("222")
        with override_settings(**ENABLED), patch.object(rj, "_post") as post:
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        post.assert_not_called()
        assert judgments == {}

    def test_consent_withdrawn_during_the_search_sends_nothing(self):
        denial = _denial()
        article = _article("555")
        # The row now says no; the copy the search started from still says yes.
        Denial.objects.filter(pk=denial.pk).update(use_external=False)
        with override_settings(**ENABLED), patch.object(rj, "_post") as post:
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        post.assert_not_called()
        assert judgments == {}

    def test_our_own_models_judgment_is_not_used_for_a_real_appeal(self):
        """Only the arena reads our own model's judgments: a real appeal
        still asks Jev, and its fax page shows nothing from them."""
        denial = _denial()
        article = _article("666")
        PubMedArticleJudgment.objects.create(
            pmid="666",
            treatment_key=rj.treatment_key(denial.procedure, denial.diagnosis),
            on_topic=0.05,
            supports=0.0,
            undermines=0.0,
            scorer="fhi/fhi-2026-general/research-rubric-1",
        )
        post = AsyncMock(return_value=_payload(supports=0.9))
        with override_settings(**ENABLED), patch.object(rj, "_post", post):
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
            stored = stored_judgments(denial, ["666"])
        post.assert_awaited_once()
        assert judgments["666"].supports == pytest.approx(0.9)
        assert stored["666"].scorer == rj.SCORER

    def test_a_new_judgment_is_stored_and_noted_healthy(self):
        denial = _denial()
        article = _article("333")
        with override_settings(**ENABLED), patch.object(
            rj, "_post", new=AsyncMock(return_value=_payload(supports=0.6))
        ):
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        assert judgments["333"].supports == pytest.approx(0.6)
        stored = PubMedArticleJudgment.objects.get(pmid="333")
        assert stored.treatment_key == rj.treatment_key(denial.procedure, denial.diagnosis)
        assert ExternalServiceHealth.objects.get(service=rj.SERVICE).last_success_at

    def test_searched_articles_judged_off_topic_are_left_out(self):
        denial = _denial(pubmed_ids_json=["1", "2"])
        articles = [_article("1"), _article("2")]
        answers = {
            "Title 1": _payload(on_topic=0.05),
            "Title 2": _payload(on_topic=0.9, supports=0.9),
        }

        async def fake_post(state, questions, timeout):
            return next(p for title, p in answers.items() if title in state)

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            kept, _ = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=True
            )
        assert [a.pmid for a in kept] == ["2"]
        denial.refresh_from_db()
        assert denial.pubmed_ids_json == ["2"]

    def _judge_three(self, on_topic_by_title, *, searched=True):
        denial = _denial(pubmed_ids_json=["1", "2", "3"])
        articles = [_article("1"), _article("2"), _article("3")]

        async def fake_post(state, questions, timeout):
            title = next(t for t in on_topic_by_title if t in state)
            return _payload(on_topic=on_topic_by_title[title], supports=0.9)

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            kept, off_topic = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=searched
            )
        denial.refresh_from_db()
        return [a.pmid for a in kept], off_topic, denial.pubmed_ids_json

    def test_beside_two_on_topic_a_probably_off_topic_one_is_left_out(self):
        kept, off_topic, row = self._judge_three(
            {"Title 1": 0.9, "Title 2": 0.45, "Title 3": 0.8}
        )
        assert (sorted(kept), off_topic, sorted(row)) == (["1", "3"], set(), ["1", "3"])

    def test_with_one_on_topic_the_probably_off_topic_ones_are_kept_and_named(self):
        kept, off_topic, _ = self._judge_three(
            {"Title 1": 0.9, "Title 2": 0.45, "Title 3": 0.5}
        )
        assert (sorted(kept), off_topic) == (["1", "2", "3"], {"2", "3"})

    def test_a_selection_on_the_row_is_never_focused_only_named(self):
        kept, off_topic, row = self._judge_three(
            {"Title 1": 0.9, "Title 2": 0.45, "Title 3": 0.8}, searched=False
        )
        assert (sorted(kept), off_topic, row) == (["1", "2", "3"], {"2"}, ["1", "2", "3"])

    def _context(self, articles, on_topic, summarize):
        denial = _denial()

        async def fake_post(state, questions, timeout):
            title = next(t for t in on_topic if t in state)
            return _payload(on_topic=on_topic[title], supports=0.9)

        with override_settings(**ENABLED), patch.object(
            rj, "_post", fake_post
        ), patch.object(
            PubMedTools,
            "find_pubmed_articles_for_denial",
            new=AsyncMock(return_value=articles),
        ), patch(
            "fighthealthinsurance.pubmed_tools.ml_router.summarize", new=summarize
        ):
            return async_to_sync(PubMedTools().find_context_for_denial)(denial)

    def test_each_article_is_its_own_entry_with_its_own_note(self):
        context = self._context(
            [_article("1"), _article("2")],
            {"Title 1": 0.9, "Title 2": 0.45},
            AsyncMock(side_effect=lambda **kw: f"Summary of {kw['title']}."),
        )
        one, two = context.split("\n")
        assert "PMID: 1" in one and "Summary of Title 1." in one
        assert "Relevance note" not in one
        assert "PMID: 2" in two and "Summary of Title 2." in two
        assert two.endswith(f"Relevance note: judged {rj.PROBABLY_OFF_TOPIC_NOTE}")

    def test_articles_are_summarized_one_at_a_time_never_together(self):
        summarize = AsyncMock(return_value="A summary.")
        self._context(
            [_article("1"), _article("2")], {"Title 1": 0.9, "Title 2": 0.9}, summarize
        )
        assert sorted(c.kwargs["title"] for c in summarize.await_args_list) == [
            "Title 1",
            "Title 2",
        ]

    def test_a_new_summary_is_stored_on_the_article(self):
        self._context(
            [_article("1")], {"Title 1": 0.9}, AsyncMock(return_value="Stored one.")
        )
        assert PubMedArticleSummarized.objects.get(pmid="1").basic_summary == (
            "Stored one."
        )

    def test_a_stored_summary_is_not_written_again(self):
        article = _article("1")
        PubMedArticleSummarized.objects.filter(pk=article.pk).update(
            basic_summary="Already here."
        )
        article.refresh_from_db()
        summarize = AsyncMock(return_value="New.")
        context = self._context([article], {"Title 1": 0.9}, summarize)
        summarize.assert_not_awaited()
        assert "Already here." in context

    def test_a_failed_summary_falls_back_to_the_abstract(self):
        context = self._context(
            [_article("1")], {"Title 1": 0.9}, AsyncMock(side_effect=RuntimeError)
        )
        assert f"Summary: {ABSTRACT}" in context

    def _too_long(self, summarize, cap=150):
        with patch("fighthealthinsurance.pubmed_tools.PUBMED_CONTEXT_MAX_CHARS", cap):
            return self._context(
                [_article("1"), _article("2"), _article("3")],
                {"Title 1": 0.9, "Title 2": 0.45, "Title 3": 0.8},
                summarize,
            )

    def test_too_long_falls_back_to_one_summary_of_the_relevant_articles(self):
        summarize = AsyncMock(return_value="Two trials found MRI helped.")
        context = self._too_long(summarize)
        assert context.startswith("Two trials found MRI helped.\n")
        assert "PMID 1 (Title 1)" in context and "PMID 3 (Title 3)" in context

    def test_the_summary_of_summaries_leaves_out_the_probably_off_topic_one(self):
        summarize = AsyncMock(return_value="Two trials found MRI helped.")
        context = self._too_long(summarize)
        summarize.assert_awaited_once()
        text = summarize.await_args.kwargs["text"]
        assert "PMID: 1" in text and "PMID: 3" in text and "PMID: 2" not in text
        assert "PMID 2" not in context and "Title 2" not in context

    def test_the_summary_of_summaries_reads_all_of_its_input(self):
        summarize = AsyncMock(return_value="Two trials found MRI helped.")
        self._too_long(summarize)
        kwargs = summarize.await_args.kwargs
        assert kwargs["max_input_chars"] >= len(kwargs["text"])

    def test_too_long_without_a_summary_keeps_whole_entries_relevant_first(self):
        context = self._too_long(AsyncMock(return_value=None), cap=300)
        entries = context.split("\n")
        assert len(context) <= 300 or len(entries) == 1
        assert all(e.startswith("Title: ") for e in entries)
        assert "PMID: 2" not in context

    def _too_long_selection(self, summarize, cap=150):
        """The person's own selection, on the row before the build: it is
        labelled, never cut."""
        denial = _denial(pubmed_ids_json=["1", "2", "3"])
        for pmid in ("1", "2", "3"):
            _article(pmid)
        on_topic = {"Title 1": 0.9, "Title 2": 0.45, "Title 3": 0.8}

        async def fake_post(state, questions, timeout):
            title = next(t for t in on_topic if t in state)
            return _payload(on_topic=on_topic[title], supports=0.9)

        with override_settings(**ENABLED), patch.object(
            rj, "_post", fake_post
        ), patch(
            "fighthealthinsurance.pubmed_tools.ml_router.summarize", new=summarize
        ), patch(
            "fighthealthinsurance.pubmed_tools.PUBMED_CONTEXT_MAX_CHARS", cap
        ):
            return async_to_sync(PubMedTools().find_context_for_denial)(denial)

    def test_a_selection_is_summarized_whole_with_the_off_topic_one_noted(self):
        summarize = AsyncMock(return_value="Three studies on MRI.")
        context = self._too_long_selection(summarize)
        assert "PMID: 2" in summarize.await_args.kwargs["text"]
        assert (
            f"PMID 2 (Title 2), judged {rj.PROBABLY_OFF_TOPIC_NOTE}" in context
        )

    def test_a_selection_without_a_summary_keeps_every_entry(self):
        context = self._too_long_selection(AsyncMock(return_value=None))
        assert sorted(line.split("PMID: ")[1][0] for line in context.split("\n")) == [
            "1",
            "2",
            "3",
        ]

    def test_a_retained_off_topic_one_is_left_out_of_the_summary_of_summaries(self):
        # One plainly on topic, so focus keeps PMID 2 (with its note); the
        # condensed step is what must leave it out.
        summarize = AsyncMock(return_value="One trial found MRI helped.")
        with patch("fighthealthinsurance.pubmed_tools.PUBMED_CONTEXT_MAX_CHARS", 100):
            context = self._context(
                [_article("1"), _article("2")],
                {"Title 1": 0.9, "Title 2": 0.45},
                summarize,
            )
        assert "PMID: 2" not in summarize.await_args.kwargs["text"]
        assert "PMID 2" not in context and "PMID 1 (Title 1)" in context

    def test_articles_past_the_summarizers_input_are_not_listed_as_summarized(self):
        summarize = AsyncMock(return_value="A trial found MRI helped.")
        with patch("fighthealthinsurance.pubmed_tools._CONDENSED_INPUT_CHARS", 10):
            context = self._too_long(summarize)
        assert "PMID: 3" not in summarize.await_args.kwargs["text"]
        assert "PMID 3" not in context.split("Articles summarized above:")[1]

    def test_a_selection_past_the_summarizers_input_is_listed_apart(self):
        summarize = AsyncMock(return_value="A trial found MRI helped.")
        with patch("fighthealthinsurance.pubmed_tools._CONDENSED_INPUT_CHARS", 10):
            context = self._too_long_selection(summarize)
        summarized, apart = context.split("Also selected, but not in the summary above")
        assert "PMID 1 (Title 1)" in summarized
        assert "PMID 2 (Title 2)" in apart and "PMID 3 (Title 3)" in apart

    def test_the_unsummarized_fallback_skips_one_too_big_for_a_later_one(self):
        articles = [
            PubMedArticleSummarized(pmid=p, title=f"Title {p}") for p in "123"
        ]
        entries = ["a" * 50, "b" * 200, "c" * 50]
        with patch("fighthealthinsurance.pubmed_tools.PUBMED_CONTEXT_MAX_CHARS", 120):
            context = async_to_sync(PubMedTools._condensed_context)(
                articles, entries, set(), 0.0
            )
        assert context.split("\n") == ["a" * 50, "c" * 50]

    def test_a_blank_stored_summary_is_replaced(self):
        article = _article("1")
        PubMedArticleSummarized.objects.filter(pk=article.pk).update(basic_summary="  ")
        article.refresh_from_db()
        self._context([article], {"Title 1": 0.9}, AsyncMock(return_value="Now one."))
        assert PubMedArticleSummarized.objects.get(pmid="1").basic_summary == "Now one."

    def test_under_the_cap_there_is_no_summary_of_summaries(self):
        summarize = AsyncMock(return_value="A summary.")
        self._context(
            [_article("1"), _article("2")], {"Title 1": 0.9, "Title 2": 0.9}, summarize
        )
        assert all(
            c.kwargs["title"].startswith("Title ") for c in summarize.await_args_list
        )

    def test_selected_articles_not_fetched_in_time_stay_on_the_row(self):
        # The search selected three; the fetch was cut after two. Judging
        # drops one of the two it saw, and the third, never judged, stays.
        denial = _denial(pubmed_ids_json=["1", "2", "3"])
        articles = [_article("1"), _article("2")]
        answers = {
            "Title 1": _payload(on_topic=0.05),
            "Title 2": _payload(on_topic=0.9, supports=0.9),
        }

        async def fake_post(state, questions, timeout):
            return next(p for title, p in answers.items() if title in state)

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            kept, _ = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=True, selected=["1", "2", "3"]
            )
        assert [a.pmid for a in kept] == ["2"]
        denial.refresh_from_db()
        assert denial.pubmed_ids_json == ["2", "3"]

    def test_a_selection_already_on_the_row_is_only_reordered(self):
        denial = _denial(pubmed_ids_json=["1", "2"])
        articles = [_article("1"), _article("2")]
        answers = {
            "Title 1": _payload(on_topic=0.05),
            "Title 2": _payload(on_topic=0.9, supports=0.9),
        }

        async def fake_post(state, questions, timeout):
            return next(p for title, p in answers.items() if title in state)

        with override_settings(**ENABLED), patch.object(rj, "_post", fake_post):
            kept, _ = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=False
            )
        assert [a.pmid for a in kept] == ["2", "1"]
        denial.refresh_from_db()
        assert denial.pubmed_ids_json == ["1", "2"]

    def test_judging_off_leaves_the_articles_as_they_are(self):
        denial = _denial()
        articles = [_article("1"), _article("2")]
        kept, _ = async_to_sync(PubMedTools()._judged_for_context)(
            denial, articles, searched=True
        )
        assert kept == articles


@pytest.mark.django_db
class TestJudgingWithinTheCallersBudget:
    def test_slow_judging_does_not_cost_the_context_its_articles(self):
        # The caller gives up on the whole call after its budget; judging
        # that still runs then would lose the articles already found.
        denial = _denial()
        article = _article("1")

        async def slow_post(state, questions, timeout):
            await asyncio.sleep(30)

        async def build():
            return await asyncio.wait_for(
                PubMedTools().find_context_for_denial(denial, budget=2.0),
                timeout=2.0,
            )

        with override_settings(**ENABLED), patch.object(
            rj, "_post", slow_post
        ), patch(
            "fighthealthinsurance.pubmed_tools._SUMMARY_RESERVE_SECONDS", 0.5
        ), patch.object(
            PubMedTools,
            "find_pubmed_articles_for_denial",
            new=AsyncMock(return_value=[article]),
        ), patch(
            "fighthealthinsurance.pubmed_tools.ml_router.summarize",
            new=AsyncMock(return_value=None),
        ):
            context = async_to_sync(build)()
        assert "Title 1" in context

    def test_a_slow_summary_does_not_cost_the_context_its_articles(self):
        denial = _denial()
        article = _article("2", abstract=ABSTRACT * 3)

        async def slow_summary(**kwargs):
            await asyncio.sleep(30)

        async def build():
            return await asyncio.wait_for(
                PubMedTools().find_context_for_denial(denial, budget=2.0),
                timeout=2.0,
            )

        with patch.object(
            PubMedTools,
            "find_pubmed_articles_for_denial",
            new=AsyncMock(return_value=[article]),
        ), patch(
            "fighthealthinsurance.pubmed_tools.ml_router.summarize",
            new=slow_summary,
        ):
            context = async_to_sync(build)()
        assert "Title 2" in context


OWN_SCORER = "fhi/fhi-2026-general/research-rubric-1"


@pytest.mark.django_db
class TestOwnModelJudgmentsStored:
    """Where our own model's judgments go, and who reads them back."""

    def _store(self, pmid, scorer, supports):
        PubMedArticleJudgment.objects.create(
            pmid=pmid,
            treatment_key=rj.treatment_key("MRI", "low back pain"),
            on_topic=0.9,
            supports=supports,
            undermines=0.1,
            scorer=scorer,
        )

    def test_they_are_read_only_when_asked_for(self):
        self._store("1", OWN_SCORER, supports=0.8)
        assert stored_judgments_for("MRI", "low back pain", ["1"]) == {}
        read = stored_judgments_for("MRI", "low back pain", ["1"], own_models=True)
        assert read["1"].scorer == OWN_SCORER

    def test_jevs_wins_over_ours_when_ours_is_newer(self):
        self._store("1", rj.SCORER, supports=0.3)
        self._store("1", OWN_SCORER, supports=0.9)
        with override_settings(**ENABLED):
            read = stored_judgments_for("MRI", "low back pain", ["1"], own_models=True)
        assert read["1"].scorer == rj.SCORER

    def test_jevs_wins_over_ours_when_jevs_is_newer(self):
        self._store("1", OWN_SCORER, supports=0.9)
        self._store("1", rj.SCORER, supports=0.3)
        with override_settings(**ENABLED):
            read = stored_judgments_for("MRI", "low back pain", ["1"], own_models=True)
        assert read["1"].scorer == rj.SCORER

    def test_jevs_switch_off_hides_jevs_but_not_ours(self):
        self._store("1", rj.SCORER, supports=0.3)
        self._store("2", OWN_SCORER, supports=0.9)
        read = stored_judgments_for("MRI", "low back pain", ["1", "2"], own_models=True)
        assert list(read) == ["2"]

    def test_judge_missing_with_our_model_stores_it_and_leaves_jevs_health_alone(self):
        model = _own_model()
        article = _article("7")
        judgments = async_to_sync(judge_missing)(
            [article], "MRI", "low back pain", {}, own_model=model
        )
        assert judgments["7"].scorer == OWN_SCORER
        stored = PubMedArticleJudgment.objects.get(pmid="7")
        assert stored.scorer == OWN_SCORER
        assert stored.supports == pytest.approx(0.8)
        assert not ExternalServiceHealth.objects.filter(service=rj.SERVICE).exists()

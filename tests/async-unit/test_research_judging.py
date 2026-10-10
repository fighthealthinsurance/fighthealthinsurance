"""ml/research_judging.py and its use in pubmed_tools: Jev judging PubMed
articles against a denial's treatment.

Every request is stubbed at research_judging._post; nothing here reaches
TypeSafe. What matters most: only the article and the treatment are sent,
only with consent, a stored judgment is reused without a request, and an
article nobody judged keeps its place.
"""

import asyncio
from unittest.mock import AsyncMock, patch

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
from fighthealthinsurance.pubmed_tools import PubMedTools

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

    def test_without_consent_nothing_is_sent(self):
        denial = _denial(use_external=False)
        article = _article("222")
        with override_settings(**ENABLED), patch.object(rj, "_post") as post:
            judgments = async_to_sync(PubMedTools().judge_articles)(denial, [article])
        post.assert_not_called()
        assert judgments == {}

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
            kept = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=True
            )
        assert [a.pmid for a in kept] == ["2"]
        denial.refresh_from_db()
        assert denial.pubmed_ids_json == ["2"]

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
            kept = async_to_sync(PubMedTools()._judged_for_context)(
                denial, articles, searched=False
            )
        assert [a.pmid for a in kept] == ["2", "1"]
        denial.refresh_from_db()
        assert denial.pubmed_ids_json == ["1", "2"]

    def test_judging_off_leaves_the_articles_as_they_are(self):
        denial = _denial()
        articles = [_article("1"), _article("2")]
        kept = async_to_sync(PubMedTools()._judged_for_context)(
            denial, articles, searched=True
        )
        assert kept == articles

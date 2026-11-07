"""Web pages in an Appeal Arena case's evidence: found by OpenRouter's web
search, kept only when judged relevant, shown to the voter with the
verdict that kept them, and given to the drafts without it."""

from unittest.mock import AsyncMock, patch

import pytest
from django.test import override_settings

from fighthealthinsurance import chooser_tasks
from fighthealthinsurance.ml import research_judging
from fighthealthinsurance.ml.ml_models import WebCitation

OWN_JUDGMENT = research_judging.Judgment(
    on_topic=0.9,
    supports=0.8,
    undermines=0.1,
    scorer="fhi/fhi-local/research-rubric-1",
)
JEV_JUDGMENT = research_judging.Judgment(
    on_topic=0.9, supports=0.8, undermines=0.1, scorer=research_judging.SCORER
)
PAGE = WebCitation(
    "Lumbar MRI coverage policy",
    "https://example.org/policy",
    "MRI is covered when red flags are documented.",
)


def _web_row(by_jev=False):
    judgment = JEV_JUDGMENT if by_jev else OWN_JUDGMENT
    return chooser_tasks._web_evidence_item(PAGE, judgment)


class TestWebEvidenceItem:
    def test_row_carries_title_link_excerpt_and_source(self):
        row = _web_row()
        assert (row["title"], row["url"], row["summary"], row["source"]) == (
            "Lumbar MRI coverage policy",
            "https://example.org/policy",
            "MRI is covered when red flags are documented.",
            "web",
        )

    def test_row_has_no_pmid(self):
        assert "pmid" not in _web_row()

    def test_row_records_who_judged_it(self):
        assert _web_row(by_jev=True)["web_judgment"]["by_jev"] is True
        assert _web_row(by_jev=False)["web_judgment"]["by_jev"] is False

    def test_untitled_page_is_named_by_its_link(self):
        page = WebCitation("", "https://example.org/x", "")
        row = chooser_tasks._web_evidence_item(page, OWN_JUDGMENT)
        assert row["title"] == "https://example.org/x" and "summary" not in row


class TestWebEvidenceFor:
    @pytest.mark.asyncio
    @override_settings(CHOOSER_WEB_SEARCH=True, CHOOSER_JUDGE_EVIDENCE=True)
    async def test_kept_pages_become_rows(self):
        with patch.object(
            chooser_tasks,
            "judged_web_results",
            new_callable=AsyncMock,
            return_value=[(PAGE, OWN_JUDGMENT)],
        ) as search:
            rows = await chooser_tasks._web_evidence_for("Lumbar MRI", "Back pain")
        assert [r["url"] for r in rows] == ["https://example.org/policy"]
        assert search.call_args.args == ("Lumbar MRI", "Back pain")

    @pytest.mark.asyncio
    @override_settings(CHOOSER_WEB_SEARCH=True, CHOOSER_JUDGE_EVIDENCE=True)
    async def test_jev_may_judge_a_synthetic_case(self):
        with patch.object(
            chooser_tasks, "judged_web_results", new_callable=AsyncMock, return_value=[]
        ) as search:
            await chooser_tasks._web_evidence_for("Lumbar MRI", "Back pain")
        assert await search.call_args.kwargs["may_ask_jev"]() is True

    @pytest.mark.asyncio
    @override_settings(CHOOSER_WEB_SEARCH=False, CHOOSER_JUDGE_EVIDENCE=True)
    async def test_off_searches_nothing(self):
        with patch.object(
            chooser_tasks, "judged_web_results", new_callable=AsyncMock
        ) as search:
            assert await chooser_tasks._web_evidence_for("MRI", "pain") == []
        search.assert_not_called()

    @pytest.mark.asyncio
    @override_settings(CHOOSER_WEB_SEARCH=True, CHOOSER_JUDGE_EVIDENCE=False)
    async def test_no_judging_means_no_pages(self):
        with patch.object(
            chooser_tasks, "judged_web_results", new_callable=AsyncMock
        ) as search:
            assert await chooser_tasks._web_evidence_for("MRI", "pain") == []
        search.assert_not_called()

    @pytest.mark.asyncio
    @override_settings(CHOOSER_WEB_SEARCH=True, CHOOSER_JUDGE_EVIDENCE=True)
    async def test_no_procedure_searches_nothing(self):
        with patch.object(
            chooser_tasks, "judged_web_results", new_callable=AsyncMock
        ) as search:
            assert await chooser_tasks._web_evidence_for("", "pain") == []
        search.assert_not_called()


class TestEvidenceForCombinesSources:
    @pytest.mark.asyncio
    async def test_web_rows_follow_the_articles(self):
        article = {"pmid": "1", "title": "A study", "url": "https://p/1"}
        with (
            patch.object(
                chooser_tasks,
                "_pubmed_evidence_for",
                new_callable=AsyncMock,
                return_value=[article],
            ),
            patch.object(
                chooser_tasks,
                "_web_evidence_for",
                new_callable=AsyncMock,
                return_value=[_web_row()],
            ),
        ):
            evidence = await chooser_tasks._evidence_for("MRI", "pain")
        assert [e.get("pmid") or e["source"] for e in evidence] == ["1", "web"]

    @pytest.mark.asyncio
    async def test_a_failed_web_search_keeps_the_articles(self):
        article = {"pmid": "1", "title": "A study", "url": "https://p/1"}
        with (
            patch.object(
                chooser_tasks,
                "_pubmed_evidence_for",
                new_callable=AsyncMock,
                return_value=[article],
            ),
            patch.object(
                chooser_tasks,
                "_web_evidence_for",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            assert await chooser_tasks._evidence_for("MRI", "pain") == [article]


class TestVotersSeeWebVerdicts:
    @override_settings(CHOOSER_JUDGE_EVIDENCE=True)
    def test_our_models_verdict_is_shown_while_judging_is_on(self):
        served = chooser_tasks.annotate_evidence_verdicts({"evidence": [_web_row()]})
        assert served["evidence"][0]["judgment"] == OWN_JUDGMENT.as_dict()

    @override_settings(CHOOSER_JUDGE_EVIDENCE=False)
    def test_our_models_verdict_is_hidden_while_judging_is_off(self):
        served = chooser_tasks.annotate_evidence_verdicts({"evidence": [_web_row()]})
        assert "judgment" not in served["evidence"][0]

    def test_jevs_verdict_follows_research_judgings_switch(self):
        with patch.object(research_judging, "enabled", return_value=False):
            served = chooser_tasks.annotate_evidence_verdicts(
                {"evidence": [_web_row(by_jev=True)]}
            )
        assert "judgment" not in served["evidence"][0]

    def test_stored_verdict_key_is_not_served(self):
        served = chooser_tasks.annotate_evidence_verdicts({"evidence": [_web_row()]})
        assert "web_judgment" not in served["evidence"][0]

    def test_the_stored_case_is_left_as_it_was(self):
        context = {"evidence": [_web_row()]}
        chooser_tasks.annotate_evidence_verdicts(context)
        assert "web_judgment" in context["evidence"][0]


class TestDraftPromptWithWebPages:
    def _prompt(self, evidence):
        return chooser_tasks._build_appeal_prompt(
            {"procedure": "Lumbar MRI", "diagnosis": "Back pain", "evidence": evidence}
        )

    def test_web_page_is_listed_with_its_link(self):
        prompt = self._prompt([_web_row()])
        assert (
            "- Lumbar MRI coverage policy (web page: https://example.org/policy)"
            in prompt
        )

    def test_header_names_web_pages_as_citable(self):
        assert "the only studies and web pages you may cite" in self._prompt(
            [_web_row()]
        )

    def test_drafts_are_told_how_to_cite_a_page(self):
        assert "(a web page by its title or link)" in self._prompt([_web_row()])

    def test_drafts_are_not_told_the_verdict(self):
        prompt = self._prompt([_web_row()])
        assert (
            "supports"
            not in prompt.lower().split("evidence on file")[1].split("please write")[0]
        )

    def test_articles_only_prompt_is_unchanged(self):
        prompt = self._prompt([{"pmid": "1", "title": "A study"}])
        assert "the only studies you may cite" in prompt
        assert "web page" not in prompt


ON_TOPIC = research_judging.Judgment(on_topic=0.9, supports=0.8, undermines=0.1)
MAYBE = research_judging.Judgment(on_topic=0.45, supports=0.8, undermines=0.1)


def _articles(*pmids):
    return [{"pmid": p, "title": f"Study {p}"} for p in pmids]


class TestFocusEvidence:
    """The drafts are told which evidence is probably off topic, and given
    only the plainly on-topic evidence when there is enough of it."""

    def test_beside_two_on_topic_the_rest_are_left_out(self):
        focused = chooser_tasks._focus_evidence(
            _articles("1", "2", "3"), {"1": ON_TOPIC, "2": MAYBE, "3": ON_TOPIC}
        )
        assert [e["pmid"] for e in focused] == ["1", "3"]

    def test_with_fewer_on_topic_the_probably_off_topic_ones_are_marked(self):
        focused = chooser_tasks._focus_evidence(
            _articles("1", "2"), {"1": ON_TOPIC, "2": MAYBE}
        )
        assert [e.get("relevance") for e in focused] == [
            None,
            chooser_tasks.EVIDENCE_PROBABLY_OFF_TOPIC,
        ]

    def test_without_judgments_the_evidence_is_as_it_was(self):
        evidence = _articles("1", "2")
        assert chooser_tasks._focus_evidence(evidence, {}) == evidence

    def test_a_probably_off_topic_web_page_is_marked(self):
        row = chooser_tasks._web_evidence_item(PAGE, MAYBE)
        assert row["relevance"] == chooser_tasks.EVIDENCE_PROBABLY_OFF_TOPIC

    def test_an_on_topic_web_page_is_not(self):
        assert "relevance" not in _web_row()


class TestDraftPromptRelevanceNote:
    def _prompt(self, evidence):
        return chooser_tasks._build_appeal_prompt(
            {"procedure": "Lumbar MRI", "diagnosis": "Back pain", "evidence": evidence}
        )

    def test_a_marked_article_carries_the_note(self):
        evidence = [
            {
                "pmid": "2",
                "title": "Study 2",
                "relevance": chooser_tasks.EVIDENCE_PROBABLY_OFF_TOPIC,
            }
        ]
        assert (
            f"Relevance note: judged {research_judging.PROBABLY_OFF_TOPIC_NOTE}"
            in self._prompt(evidence)
        )

    def test_an_unmarked_article_has_no_note(self):
        assert "Relevance note" not in self._prompt(_articles("1"))

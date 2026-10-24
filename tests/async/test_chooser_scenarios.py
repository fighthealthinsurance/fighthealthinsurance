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
)
from fighthealthinsurance.condition_rankings import SeedPair
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    PubMedMiniArticle,
    PubMedQueryData,
)

_LETTER = "Dear Reviewer, " + "this care is medically necessary. " * 10


def _writer(scenario):
    model = MagicMock()
    model.name = "model-a"
    model.external = False
    model._infer_no_context = AsyncMock(side_effect=[scenario] + [_LETTER] * 10)
    return model


async def _cached_search(query, pmids, age_minutes=0):
    """A cached search row; ``age_minutes`` sets its age explicitly, since
    two rows created back to back can share a millisecond timestamp."""
    row = await PubMedQueryData.objects.acreate(query=query, articles=json.dumps(pmids))
    await PubMedQueryData.objects.filter(pk=row.pk).aupdate(
        created=timezone.now() - datetime.timedelta(minutes=age_minutes)
    )


async def _article(pmid, title, url=None):
    await PubMedMiniArticle.objects.acreate(pmid=pmid, title=title, article_url=url)


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

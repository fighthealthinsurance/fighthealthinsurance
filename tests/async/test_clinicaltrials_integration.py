"""Integration tests for ClinicalTrialsTools with DB caching."""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fighthealthinsurance.clinicaltrials_tools import ClinicalTrialsTools
from fighthealthinsurance.models import (
    ClinicalTrial,
    ClinicalTrialQueryData,
    Denial,
)


def _make_studies_payload(nct_ids: List[str]) -> Dict[str, Any]:
    """Build a minimal v2 /studies response with the given NCT IDs."""
    return {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": nct_id,
                        "briefTitle": f"Study {nct_id}",
                    },
                    "statusModule": {
                        "overallStatus": "RECRUITING",
                        "startDateStruct": {"date": "2023-01-01"},
                    },
                    "descriptionModule": {
                        "briefSummary": f"A summary for {nct_id}",
                    },
                    "conditionsModule": {"conditions": ["Test Condition"]},
                    "armsInterventionsModule": {
                        "interventions": [{"type": "DRUG", "name": "Test Drug"}],
                    },
                    "designModule": {
                        "phases": ["PHASE3"],
                        "studyType": "INTERVENTIONAL",
                    },
                },
                "hasResults": False,
            }
            for nct_id in nct_ids
        ]
    }


def _mock_session_returning(payload: Dict[str, Any]):
    """Build an aiohttp.ClientSession mock whose .get() returns the payload."""
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value=payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.get = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestFindTrialsForQuery:
    """End-to-end behavior of find_trials_for_query with DB cache."""

    async def test_fetches_and_caches_trials(self):
        tools = ClinicalTrialsTools()
        payload = _make_studies_payload(["NCT11111111", "NCT22222222"])
        session = _mock_session_returning(payload)

        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            return_value=session,
        ):
            ids = await tools.find_trials_for_query("pembrolizumab")

        assert ids == ["NCT11111111", "NCT22222222"]
        # Trials are cached
        assert await ClinicalTrial.objects.filter(nct_id="NCT11111111").aexists()
        assert await ClinicalTrial.objects.filter(nct_id="NCT22222222").aexists()
        cached = await ClinicalTrial.objects.aget(nct_id="NCT11111111")
        assert cached.brief_title == "Study NCT11111111"
        assert cached.overall_status == "RECRUITING"
        assert cached.phases == "PHASE3"
        # Query is cached
        assert await ClinicalTrialQueryData.objects.filter(
            query="pembrolizumab"
        ).aexists()

    async def test_uses_cached_query_on_second_call(self):
        tools = ClinicalTrialsTools()
        payload = _make_studies_payload(["NCT33333333"])
        session = _mock_session_returning(payload)

        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            return_value=session,
        ) as session_factory:
            await tools.find_trials_for_query("treatment a")
            # Second call should hit the cache without creating a new session
            second_ids = await tools.find_trials_for_query("treatment a")

        assert second_ids == ["NCT33333333"]
        # ClientSession should only have been built once (first call)
        assert session_factory.call_count == 1

    async def test_returns_empty_list_for_blank_query(self):
        tools = ClinicalTrialsTools()
        # No HTTP should happen; no patching needed.
        ids = await tools.find_trials_for_query("")
        assert ids == []

    async def test_caches_empty_result_sets(self):
        """Queries that legitimately return zero matches must be cached, so
        we don't keep hammering ClinicalTrials.gov for the same null answer."""
        tools = ClinicalTrialsTools()
        empty_payload: Dict[str, Any] = {"studies": []}
        session = _mock_session_returning(empty_payload)

        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            return_value=session,
        ) as session_factory:
            first = await tools.find_trials_for_query("nonexistent therapy xyz")
            second = await tools.find_trials_for_query("nonexistent therapy xyz")

        assert first == []
        assert second == []
        # Second call should hit the cached "[]" row, not refetch.
        assert session_factory.call_count == 1
        cached = await ClinicalTrialQueryData.objects.aget(
            query="nonexistent therapy xyz"
        )
        assert cached.nct_ids == "[]"

    async def test_normalizes_condition_and_intervention(self):
        """Whitespace-only differences in condition/intervention must hit the
        same cache row, not create duplicates."""
        tools = ClinicalTrialsTools()
        payload = _make_studies_payload(["NCT66666666"])
        session = _mock_session_returning(payload)

        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            return_value=session,
        ) as session_factory:
            await tools.find_trials_for_query(
                "drug x", condition="melanoma", intervention="pembro"
            )
            # Same logical query with surrounding whitespace and a blank-string
            # equivalent of None must hit the cached row, not refetch.
            second = await tools.find_trials_for_query(
                "drug x", condition="  melanoma  ", intervention="pembro"
            )

        assert second == ["NCT66666666"]
        assert session_factory.call_count == 1
        # Single cache row, not two
        assert await ClinicalTrialQueryData.objects.filter(query="drug x").acount() == 1

    async def test_find_trials_for_denial_writes_audit_row_and_global_cache(self):
        """find_trials_for_denial must write a global cache row that the next
        call can hit, plus a separate denial-scoped audit row."""
        tools = ClinicalTrialsTools()
        denial = await Denial.objects.acreate(
            procedure="pembrolizumab",
            diagnosis="melanoma",
        )
        payload = _make_studies_payload(["NCT55555555"])
        session = _mock_session_returning(payload)

        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            return_value=session,
        ) as session_factory:
            trials = await tools.find_trials_for_denial(denial, max_trials=1)
            # A second call for the same denial must hit the global cache,
            # not re-fetch from the network.
            trials_again = await tools.find_trials_for_denial(denial, max_trials=1)

        assert [t.nct_id for t in trials] == ["NCT55555555"]
        assert [t.nct_id for t in trials_again] == ["NCT55555555"]
        assert session_factory.call_count == 1
        # One global cache row + two denial-scoped audit rows (one per call).
        global_rows = await ClinicalTrialQueryData.objects.filter(
            denial_id__isnull=True
        ).acount()
        denial_rows = await ClinicalTrialQueryData.objects.filter(
            denial_id=denial
        ).acount()
        assert global_rows == 1
        assert denial_rows == 2

    async def test_get_trial_uses_db_cache(self):
        tools = ClinicalTrialsTools()
        await ClinicalTrial.objects.acreate(
            nct_id="NCT44444444",
            brief_title="Cached title",
        )
        # If the cache works, no HTTP should be made; force an explosion if it is.
        with patch(
            "fighthealthinsurance.clinicaltrials_tools.aiohttp.ClientSession",
            side_effect=AssertionError("should not hit network"),
        ):
            trial = await tools.get_trial("NCT44444444")

        assert trial is not None
        assert trial.study_url == "https://clinicaltrials.gov/study/NCT44444444"
        assert trial.brief_title == "Cached title"

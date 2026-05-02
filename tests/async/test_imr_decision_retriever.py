"""DB-backed tests for the IMR / external-appeal decision retriever."""

from unittest.mock import MagicMock

import pytest

from fighthealthinsurance.ml.imr_decision_retriever import IMRDecisionRetriever
from fighthealthinsurance.models import Denial, IMRDecision


def _make_denial(procedure="", diagnosis="", state=None):
    """Create a MagicMock Denial — retriever only reads procedure/diagnosis/state."""
    denial = MagicMock(spec=Denial)
    denial.denial_id = 1
    denial.procedure = procedure
    denial.diagnosis = diagnosis
    denial.state = state
    return denial


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestIMRDecisionRetrieverDB:
    async def _seed(self):
        await IMRDecision.objects.acreate(
            source=IMRDecision.SOURCE_CA_DMHC,
            case_id="CA-1",
            state="CA",
            decision_year=2020,
            diagnosis="breast cancer",
            diagnosis_category="cancer",
            treatment="trastuzumab",
            treatment_category="pharmacy/prescription drugs",
            determination=IMRDecision.DETERMINATION_OVERTURNED,
            findings="Reviewer found trastuzumab medically necessary for HER2+ disease.",
        )
        await IMRDecision.objects.acreate(
            source=IMRDecision.SOURCE_CA_DMHC,
            case_id="CA-2",
            state="CA",
            decision_year=2018,
            diagnosis="breast cancer",
            treatment="trastuzumab",
            determination=IMRDecision.DETERMINATION_UPHELD,
            findings="Insurer denial upheld; insufficient documentation.",
        )
        await IMRDecision.objects.acreate(
            source=IMRDecision.SOURCE_NY_DFS,
            case_id="NY-1",
            state="NY",
            decision_year=2022,
            diagnosis="breast cancer",
            treatment="trastuzumab",
            determination=IMRDecision.DETERMINATION_OVERTURNED,
            findings="External reviewer overturned denial of trastuzumab.",
        )
        await IMRDecision.objects.acreate(
            source=IMRDecision.SOURCE_CA_DMHC,
            case_id="CA-3",
            state="CA",
            decision_year=2021,
            diagnosis="diabetes",
            treatment="insulin pump",
            determination=IMRDecision.DETERMINATION_OVERTURNED,
            findings="Unrelated case.",
        )

    async def test_returns_empty_when_no_query_signals(self):
        await self._seed()
        denial = _make_denial(procedure="", diagnosis="")
        results = await IMRDecisionRetriever.retrieve_for_denial(denial)
        assert results == []

    async def test_matches_by_treatment_token(self):
        await self._seed()
        denial = _make_denial(procedure="trastuzumab", diagnosis="")
        results = await IMRDecisionRetriever.retrieve_for_denial(denial)
        case_ids = {r.case_id for r in results}
        assert {"CA-1", "CA-2", "NY-1"}.issubset(case_ids)
        assert "CA-3" not in case_ids

    async def test_overturned_outranks_upheld(self):
        await self._seed()
        denial = _make_denial(procedure="trastuzumab", diagnosis="breast cancer")
        results = await IMRDecisionRetriever.retrieve_for_denial(denial)
        # First result must be an overturned decision (CA-1 or NY-1)
        assert results[0].determination == IMRDecision.DETERMINATION_OVERTURNED
        # CA-2 (upheld) should appear after the overturned ones
        ranks = {r.case_id: i for i, r in enumerate(results)}
        assert ranks["CA-2"] > ranks["CA-1"]
        assert ranks["CA-2"] > ranks["NY-1"]

    async def test_same_state_bonus_breaks_tie(self):
        await self._seed()
        # Both CA-1 and NY-1 are overturned; CA query should put CA-1 first
        denial_ca = _make_denial(
            procedure="trastuzumab", diagnosis="breast cancer", state="CA"
        )
        results = await IMRDecisionRetriever.retrieve_for_denial(denial_ca)
        assert results[0].case_id == "CA-1"

        denial_ny = _make_denial(
            procedure="trastuzumab", diagnosis="breast cancer", state="NY"
        )
        results_ny = await IMRDecisionRetriever.retrieve_for_denial(denial_ny)
        assert results_ny[0].case_id == "NY-1"

    async def test_limit_respected(self):
        await self._seed()
        denial = _make_denial(procedure="trastuzumab", diagnosis="breast cancer")
        results = await IMRDecisionRetriever.retrieve_for_denial(denial, limit=1)
        assert len(results) == 1

    async def test_get_context_for_denial_formats_or_returns_none(self):
        await self._seed()
        # No matches -> None
        denial_none = _make_denial(procedure="aspirin", diagnosis="headache")
        assert await IMRDecisionRetriever.get_context_for_denial(denial_none) is None

        # Matches -> formatted string with disclaimer
        denial = _make_denial(procedure="trastuzumab", diagnosis="breast cancer")
        ctx = await IMRDecisionRetriever.get_context_for_denial(denial, limit=2)
        assert ctx is not None
        assert "illustrative, not legal precedent" in ctx
        assert "Overturned" in ctx
        assert "trastuzumab" in ctx.lower()

    async def test_diagnosis_only_query_matches(self):
        await self._seed()
        denial = _make_denial(procedure="", diagnosis="breast cancer")
        results = await IMRDecisionRetriever.retrieve_for_denial(denial)
        case_ids = {r.case_id for r in results}
        # All breast-cancer rows should match
        assert {"CA-1", "CA-2", "NY-1"}.issubset(case_ids)

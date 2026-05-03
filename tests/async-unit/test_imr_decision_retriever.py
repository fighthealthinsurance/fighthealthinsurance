"""Unit tests for IMRDecisionRetriever's pure-Python helpers (no DB)."""

from unittest.mock import MagicMock

import pytest

from fighthealthinsurance.ml.imr_decision_retriever import (
    IMRDecisionRetriever,
    _build_text_q,
    _tokens,
)
from fighthealthinsurance.models import IMRDecision


class TestTokens:
    def test_lowercases_and_filters_short_tokens(self):
        assert _tokens("Trastuzumab Therapy") == ["trastuzumab"]

    def test_drops_stopwords(self):
        # 'patient' / 'condition' / 'medical' are stopwords
        assert "patient" not in _tokens("Patient with the condition")

    def test_handles_none_and_empty(self):
        assert _tokens(None) == []
        assert _tokens("") == []

    def test_keeps_alphanumeric_codes(self):
        assert "j9355" in _tokens("HCPCS J9355 administration")


class TestBuildTextQ:
    def test_returns_none_for_empty_tokens(self):
        assert _build_text_q("treatment", []) is None

    def test_returns_q_for_tokens(self):
        q = _build_text_q("treatment", ["foo", "bar"])
        assert q is not None
        # The Q should be an OR over icontains lookups
        assert "treatment__icontains" in str(q.children) or len(q.children) == 2


class TestFormatForAppeal:
    def _make_decision(self, **overrides):
        d = MagicMock(spec=IMRDecision)
        d.source = IMRDecision.SOURCE_CA_DMHC
        d.case_id = "EI-1"
        d.decision_year = 2020
        d.diagnosis = "breast cancer"
        d.diagnosis_category = "cancer"
        d.treatment = "trastuzumab"
        d.treatment_category = "pharmacy"
        d.findings = "Reviewer found treatment medically necessary."
        d.determination = IMRDecision.DETERMINATION_OVERTURNED
        d.get_determination_display = lambda: "Overturned"
        for k, v in overrides.items():
            setattr(d, k, v)
        return d

    def test_returns_none_for_empty_list(self):
        assert IMRDecisionRetriever.format_for_appeal([]) is None

    def test_includes_disclaimer(self):
        out = IMRDecisionRetriever.format_for_appeal([self._make_decision()])
        assert "illustrative, not legal precedent" in out

    def test_includes_source_label(self):
        ca = self._make_decision()
        ny = self._make_decision(source=IMRDecision.SOURCE_NY_DFS, case_id="NY-1")
        out = IMRDecisionRetriever.format_for_appeal([ca, ny])
        assert "CA DMHC IMR" in out
        assert "NY DFS External Appeal" in out

    def test_truncates_long_findings(self):
        long_findings = "x" * 5000
        d = self._make_decision(findings=long_findings)
        out = IMRDecisionRetriever.format_for_appeal([d])
        assert out.endswith("...")
        # Ensure it didn't dump the whole 5000 chars
        assert len(out) < 5000

    def test_falls_back_to_categories_when_treatment_missing(self):
        d = self._make_decision(
            treatment="", treatment_category="Mental Health", diagnosis=""
        )
        out = IMRDecisionRetriever.format_for_appeal([d])
        assert "Mental Health" in out


class TestRetrieveForDenialEarlyReturn:
    @pytest.mark.asyncio
    async def test_no_procedure_or_diagnosis_returns_empty(self):
        denial = MagicMock()
        denial.procedure = ""
        denial.diagnosis = ""
        denial.state = "CA"
        denial.denial_id = 1
        results = await IMRDecisionRetriever.retrieve_for_denial(denial)
        assert results == []

"""Unit tests for ClinicalTrialsTools (no DB / network)."""

from urllib.parse import parse_qs, urlparse

import pytest

from fighthealthinsurance.clinicaltrials_tools import (
    CLINICAL_TRIALS_API_BASE,
    ClinicalTrialsTools,
    _intervention_strings,
    _parse_study,
)


@pytest.fixture
def tools():
    return ClinicalTrialsTools()


class TestParseStudy:
    """Tests for _parse_study extraction from raw API responses."""

    @pytest.mark.parametrize("invalid", [None, "not a dict", 42])
    def test_returns_none_for_non_dict(self, invalid):
        assert _parse_study(invalid) is None  # type: ignore[arg-type]

    def test_returns_none_when_nct_id_missing(self):
        study = {"protocolSection": {"identificationModule": {}}}
        assert _parse_study(study) is None

    def test_extracts_full_study(self):
        study = {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT01234567",
                    "briefTitle": "Brief title",
                    "officialTitle": "Official title",
                },
                "statusModule": {
                    "overallStatus": "RECRUITING",
                    "startDateStruct": {"date": "2023-01-15"},
                    "completionDateStruct": {"date": "2026-12-31"},
                    "lastUpdatePostDateStruct": {"date": "2024-06-01"},
                },
                "descriptionModule": {
                    "briefSummary": "We test drug X for condition Y.",
                },
                "conditionsModule": {
                    "conditions": ["Melanoma", "Stage IV Cancer"],
                },
                "armsInterventionsModule": {
                    "interventions": [
                        {"type": "DRUG", "name": "Pembrolizumab"},
                        {"name": "Standard of care"},
                    ],
                },
                "designModule": {
                    "phases": ["PHASE2", "PHASE3"],
                    "studyType": "INTERVENTIONAL",
                },
            },
            "hasResults": True,
        }

        parsed = _parse_study(study)

        assert parsed is not None
        assert parsed["nct_id"] == "NCT01234567"
        assert parsed["brief_title"] == "Brief title"
        assert parsed["official_title"] == "Official title"
        assert parsed["overall_status"] == "RECRUITING"
        assert parsed["phases"] == "PHASE2,PHASE3"
        assert parsed["study_type"] == "INTERVENTIONAL"
        assert "Melanoma" in parsed["conditions"]
        assert "Stage IV Cancer" in parsed["conditions"]
        assert "DRUG: Pembrolizumab" in parsed["interventions"]
        assert "Standard of care" in parsed["interventions"]
        assert parsed["brief_summary"] == "We test drug X for condition Y."
        assert parsed["start_date"] == "2023-01-15"
        assert parsed["completion_date"] == "2026-12-31"
        assert parsed["last_update_date"] == "2024-06-01"
        assert parsed["has_results"] is True
        assert "study_url" not in parsed  # derived as a model property

    def test_handles_partial_data(self):
        study = {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT99999999"},
            }
        }
        parsed = _parse_study(study)
        assert parsed is not None
        assert parsed["nct_id"] == "NCT99999999"
        assert parsed["brief_title"] == ""
        assert parsed["overall_status"] == ""
        assert parsed["has_results"] is False


class TestInterventionStrings:
    def test_skips_non_dicts(self):
        assert _intervention_strings([None, "string"]) == []  # type: ignore[list-item]

    def test_renders_type_and_name(self):
        result = _intervention_strings(
            [{"type": "DRUG", "name": "Aspirin"}, {"name": "Surgery"}]
        )
        assert result == ["DRUG: Aspirin", "Surgery"]

    def test_skips_empty(self):
        assert _intervention_strings([{"type": "DRUG"}, {"name": ""}]) == []


class TestBuildStudiesUrl:
    def test_includes_query_terms(self, tools):
        url = tools._build_studies_url(
            query="pembrolizumab",
            condition="melanoma",
            intervention="immunotherapy",
            page_size=5,
        )
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert parsed.netloc == "clinicaltrials.gov"
        assert parsed.path == "/api/v2/studies"
        assert params["query.term"] == ["pembrolizumab"]
        assert params["query.cond"] == ["melanoma"]
        assert params["query.intr"] == ["immunotherapy"]
        assert params["pageSize"] == ["5"]
        assert params["format"] == ["json"]

    def test_clamps_page_size(self, tools):
        url = tools._build_studies_url(
            query="x", condition=None, intervention=None, page_size=999
        )
        params = parse_qs(urlparse(url).query)
        assert params["pageSize"] == ["50"]
        url_low = tools._build_studies_url(
            query="x", condition=None, intervention=None, page_size=0
        )
        params_low = parse_qs(urlparse(url_low).query)
        assert params_low["pageSize"] == ["1"]

    def test_omits_blank_filters(self, tools):
        url = tools._build_studies_url(
            query=None, condition="melanoma", intervention=None, page_size=5
        )
        params = parse_qs(urlparse(url).query)
        assert "query.term" not in params
        assert "query.intr" not in params
        assert params["query.cond"] == ["melanoma"]


class TestFormatTrialShort:
    def test_renders_all_fields(self, tools):
        class FakeTrial:
            nct_id = "NCT01234567"
            brief_title = "A Study"
            overall_status = "RECRUITING"
            phases = "PHASE3"
            study_type = "INTERVENTIONAL"
            conditions = "Cond1\nCond2"
            interventions = "DRUG: Foo"
            has_results = True
            start_date = "2023-01-01"
            completion_date = "2026-01-01"
            brief_summary = "Studies foo for cond."
            study_url = "https://clinicaltrials.gov/study/NCT01234567"

        rendered = tools.format_trial_short(FakeTrial())
        assert "NCT: NCT01234567" in rendered
        assert "Title: A Study" in rendered
        assert "Status: RECRUITING" in rendered
        assert "Phase: PHASE3" in rendered
        assert "Conditions: Cond1; Cond2" in rendered
        assert "Interventions: DRUG: Foo" in rendered
        assert "HasResults: yes" in rendered
        assert "URL: https://clinicaltrials.gov/study/NCT01234567" in rendered

    def test_skips_blank_fields(self, tools):
        class FakeTrial:
            nct_id = "NCT01234567"
            brief_title = ""
            overall_status = ""
            phases = ""
            study_type = ""
            conditions = ""
            interventions = ""
            has_results = False
            start_date = ""
            completion_date = ""
            brief_summary = ""
            study_url = "https://clinicaltrials.gov/study/NCT01234567"

        rendered = tools.format_trial_short(FakeTrial())
        assert (
            rendered
            == "NCT: NCT01234567; URL: https://clinicaltrials.gov/study/NCT01234567"
        )


class TestApiBase:
    def test_default_base(self):
        tools = ClinicalTrialsTools()
        assert tools.api_base == CLINICAL_TRIALS_API_BASE

    def test_strips_trailing_slash(self):
        tools = ClinicalTrialsTools(api_base="https://example.com/api/v2/")
        assert tools.api_base == "https://example.com/api/v2"

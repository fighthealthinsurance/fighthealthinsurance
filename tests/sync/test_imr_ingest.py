"""Tests for the IMR / external-appeal ingestion parsers and loader."""

import pytest

from fighthealthinsurance.imr_ingest import (
    iter_csv_rows,
    load_csv_text,
    load_parsed_rows,
    parse_ca_dmhc_row,
    parse_ny_dfs_row,
    _normalize_determination,
    _parse_year,
    _parse_date,
)
from fighthealthinsurance.models import IMRDecision


class TestNormalizationHelpers:
    def test_normalize_determination_overturned_variants(self):
        assert (
            _normalize_determination("Overturned")
            == IMRDecision.DETERMINATION_OVERTURNED
        )
        assert (
            _normalize_determination("OVERTURNED DECISION")
            == IMRDecision.DETERMINATION_OVERTURNED
        )

    def test_normalize_determination_in_part(self):
        assert (
            _normalize_determination("Overturned in Part")
            == IMRDecision.DETERMINATION_OVERTURNED_IN_PART
        )
        assert (
            _normalize_determination("Overturned (Modified)")
            == IMRDecision.DETERMINATION_OVERTURNED_IN_PART
        )

    def test_normalize_determination_upheld(self):
        assert _normalize_determination("Upheld") == IMRDecision.DETERMINATION_UPHELD
        assert (
            _normalize_determination("Uphold Decision")
            == IMRDecision.DETERMINATION_UPHELD
        )

    def test_normalize_determination_unknown_falls_back_to_other(self):
        assert _normalize_determination("") == IMRDecision.DETERMINATION_OTHER
        assert _normalize_determination("Pending") == IMRDecision.DETERMINATION_OTHER

    def test_parse_year_handles_iso_and_partials(self):
        assert _parse_year("2019") == 2019
        assert _parse_year("2019-04-01") == 2019
        assert _parse_year("19") is None
        assert _parse_year("") is None

    def test_parse_date_supports_common_formats(self):
        assert _parse_date("2019-04-01").isoformat() == "2019-04-01"
        assert _parse_date("4/1/2019").isoformat() == "2019-04-01"
        assert _parse_date("not a date") is None
        assert _parse_date("") is None


class TestCAParser:
    def test_parse_ca_row_basic(self):
        row = {
            "Reference ID": "EI-12345",
            "Report Year": "2020",
            "Diagnosis Category": "Cancer",
            "Diagnosis Sub Category": "Breast Cancer",
            "Treatment Category": "Pharmacy/Prescription Drugs",
            "Treatment Sub Category": "Trastuzumab",
            "Determination": "Overturned",
            "Type": "Medical Necessity",
            "Age Range": "41-50",
            "Patient Gender": "Female",
            "Findings": "Reviewer found treatment medically necessary based on records.",
        }
        parsed = parse_ca_dmhc_row(row)
        assert parsed is not None
        assert parsed.source == IMRDecision.SOURCE_CA_DMHC
        assert parsed.case_id == "EI-12345"
        assert parsed.state == "CA"
        assert parsed.decision_year == 2020
        assert parsed.diagnosis_category == "Cancer"
        assert parsed.diagnosis == "Breast Cancer"
        assert parsed.treatment_category == "Pharmacy/Prescription Drugs"
        assert parsed.treatment_subcategory == "Trastuzumab"
        assert parsed.treatment == "Trastuzumab"
        assert parsed.determination == IMRDecision.DETERMINATION_OVERTURNED
        assert parsed.decision_type == "Medical Necessity"
        assert parsed.age_range == "41-50"
        assert parsed.gender == "Female"
        assert "medically necessary" in parsed.findings
        assert parsed.raw_data == row

    def test_parse_ca_row_missing_case_id_returns_none(self):
        assert parse_ca_dmhc_row({"Report Year": "2020"}) is None

    def test_parse_ca_row_treatment_falls_back_to_category(self):
        # CA dataset has no "Treatment" column; if Sub Category is missing,
        # treatment should fall back to Treatment Category.
        row = {
            "Reference ID": "EI-2",
            "Treatment Category": "Mental Health",
            "Determination": "Overturned",
        }
        parsed = parse_ca_dmhc_row(row)
        assert parsed is not None
        assert parsed.treatment_subcategory == ""
        assert parsed.treatment_category == "Mental Health"
        assert parsed.treatment == "Mental Health"

    def test_parse_ca_row_is_header_case_insensitive(self):
        row = {
            "REFERENCE ID": "EI-99",
            "diagnosiscategory": "Mental Health",
            "TREATMENT CATEGORY": "Mental Health",
            "DETERMINATION": "Upheld",
        }
        parsed = parse_ca_dmhc_row(row)
        assert parsed is not None
        assert parsed.case_id == "EI-99"
        assert parsed.diagnosis_category == "Mental Health"
        assert parsed.treatment_category == "Mental Health"
        assert parsed.determination == IMRDecision.DETERMINATION_UPHELD


class TestNYParser:
    def test_parse_ny_row_basic(self):
        row = {
            "Appeal Number": "NY-2021-001",
            "Year": "2021",
            "Diagnosis": "Type 1 Diabetes",
            "Treatment": "Continuous Glucose Monitor",
            "Treatment Category": "Durable Medical Equipment",
            "Determination": "Overturned",
            "Plan Type": "Commercial",
            "Age Range": "18-29",
            "Gender": "Male",
            "Description": "Insurer denial overturned; CGM medically necessary.",
        }
        parsed = parse_ny_dfs_row(row)
        assert parsed is not None
        assert parsed.source == IMRDecision.SOURCE_NY_DFS
        assert parsed.case_id == "NY-2021-001"
        assert parsed.state == "NY"
        assert parsed.decision_year == 2021
        assert parsed.diagnosis == "Type 1 Diabetes"
        assert parsed.treatment == "Continuous Glucose Monitor"
        assert parsed.treatment_category == "Durable Medical Equipment"
        assert parsed.determination == IMRDecision.DETERMINATION_OVERTURNED
        assert parsed.insurance_type == "Commercial"
        assert parsed.age_range == "18-29"
        assert parsed.gender == "Male"
        assert "CGM medically necessary" in parsed.findings

    def test_parse_ny_row_missing_id_returns_none(self):
        assert parse_ny_dfs_row({"Year": "2021"}) is None


class TestIterCsvRows:
    def test_iter_csv_rows_handles_empty_cells(self):
        text = "A,B,C\n1,,3\n,,\n"
        rows = list(iter_csv_rows(text))
        assert rows[0] == {"A": "1", "B": "", "C": "3"}
        assert rows[1] == {"A": "", "B": "", "C": ""}


@pytest.mark.django_db
class TestLoadParsedRows:
    @staticmethod
    def _sample_row():
        return parse_ca_dmhc_row(
            {
                "Reference ID": "EI-1",
                "Report Year": "2020",
                "Diagnosis Sub Category": "Asthma",
                "Treatment Sub Category": "Inhaler",
                "Determination": "Overturned",
            }
        )

    def test_load_creates_new_row(self):
        parsed = self._sample_row()
        assert parsed is not None
        created, updated = load_parsed_rows([parsed])
        assert (created, updated) == (1, 0)

    def test_load_is_idempotent_on_repeat(self):
        parsed = self._sample_row()
        load_parsed_rows([parsed])
        parsed.findings = "Updated findings."
        created, updated = load_parsed_rows([parsed])
        assert (created, updated) == (0, 1)
        stored = IMRDecision.objects.get(
            source=IMRDecision.SOURCE_CA_DMHC, case_id="EI-1"
        )
        assert stored.findings == "Updated findings."


@pytest.mark.django_db
class TestLoadCsvText:
    def test_ca_csv_end_to_end(self):
        csv_text = (
            "Reference ID,Report Year,Diagnosis Category,Treatment Category,Determination\n"
            "EI-101,2020,Cancer,Chemotherapy,Overturned\n"
            "EI-102,2021,Cancer,Radiation,Upheld\n"
            ",2021,Cancer,X,Upheld\n"  # missing Reference ID — skipped
        )
        created, updated, skipped = load_csv_text(
            csv_text, source=IMRDecision.SOURCE_CA_DMHC
        )
        assert (created, updated, skipped) == (2, 0, 1)
        assert (
            IMRDecision.objects.filter(source=IMRDecision.SOURCE_CA_DMHC).count() == 2
        )

    def test_ny_csv_end_to_end(self):
        csv_text = (
            "Appeal Number,Year,Diagnosis,Treatment,Determination\n"
            "NY-1,2022,Crohn's Disease,Infliximab,Overturned\n"
        )
        created, updated, skipped = load_csv_text(
            csv_text,
            source=IMRDecision.SOURCE_NY_DFS,
            source_url="https://example.gov/ny.csv",
        )
        assert (created, updated, skipped) == (1, 0, 0)
        d = IMRDecision.objects.get(case_id="NY-1")
        assert d.source_url == "https://example.gov/ny.csv"
        assert d.state == "NY"

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError):
            load_csv_text("a,b\n1,2\n", source="unknown")

"""Tests for the worst-insurance rankings ingestion loader and command."""

import json
from io import StringIO
from pathlib import Path

import pytest

from django.core.management import call_command

from fighthealthinsurance.models import (
    InsuranceCompany,
    WorstInsuranceRanking,
    WorstInsuranceReport,
)
from fighthealthinsurance.worst_insurance_ingest import (
    load_report_dict,
    match_insurance_company,
    validate_report_dict,
)

SAMPLE_PATH = Path(__file__).resolve().parent / "worst_insurance_rankings_sample.json"


def sample_report() -> dict:
    with open(SAMPLE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class TestValidation:
    def test_valid_document_passes(self):
        validate_report_dict(sample_report())

    def test_rejects_wrong_schema_version(self):
        data = sample_report()
        data["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version"):
            validate_report_dict(data)

    def test_rejects_missing_states(self):
        data = sample_report()
        del data["states"]
        with pytest.raises(ValueError, match="states"):
            validate_report_dict(data)

    @pytest.mark.parametrize(
        "bad_period", ["2026-9", "2026-07-01", "July 2026", "26-07"]
    )
    def test_rejects_malformed_period(self, bad_period):
        data = sample_report()
        data["period"] = bad_period
        with pytest.raises(ValueError, match="period"):
            validate_report_dict(data)

    def test_rejects_non_dict_state_block(self):
        # A non-dict state block would AttributeError deep in load_report_dict;
        # validation must reject it up front with a clean ValueError.
        data = sample_report()
        data["states"]["TX"] = "not-an-object"
        with pytest.raises(ValueError, match="TX"):
            validate_report_dict(data)


@pytest.mark.django_db
class TestLoadReport:
    def test_load_creates_report_and_rankings(self):
        created, updated, deleted, _ = load_report_dict(
            sample_report(), source_url="https://example.com/latest.json"
        )
        assert created == 4
        assert updated == 0
        assert deleted == 0
        report = WorstInsuranceReport.objects.get(period="2026-07")
        assert report.methodology_version == "test.v1"
        assert report.source_url == "https://example.com/latest.json"
        assert report.rankings.count() == 4
        assert report.state_statuses["WY"]["reason"] == "no_behavior_data"
        worst_tx = report.rankings.get(state="TX", rank=1)
        assert worst_tx.issuer_name == "Evil Health Insurance"
        assert worst_tx.metrics["denial_rate"]["value"] == 0.31

    def test_reingest_same_period_is_idempotent(self):
        load_report_dict(sample_report())
        created, updated, deleted, _ = load_report_dict(sample_report())
        assert created == 0
        assert updated == 4
        assert deleted == 0
        assert WorstInsuranceReport.objects.count() == 1
        assert WorstInsuranceRanking.objects.count() == 4

    def test_reingest_removes_stale_issuer_rows(self):
        load_report_dict(sample_report())
        data = sample_report()
        # The pipeline renamed/merged the CA runner-up out of existence.
        data["states"]["CA"]["issuers"] = data["states"]["CA"]["issuers"][:1]
        created, updated, deleted, _ = load_report_dict(data)
        assert created == 0
        assert deleted == 1
        assert not WorstInsuranceRanking.objects.filter(
            state="CA", issuer_slug="sunshine-care"
        ).exists()

    def test_different_periods_coexist(self):
        load_report_dict(sample_report())
        data = sample_report()
        data["period"] = "2026-08"
        load_report_dict(data)
        assert WorstInsuranceReport.objects.count() == 2
        assert WorstInsuranceRanking.objects.count() == 8

    def test_malformed_row_rolls_back_and_preserves_previous_report(self):
        load_report_dict(sample_report())
        before = set(WorstInsuranceRanking.objects.values_list("state", "issuer_slug"))
        # Re-ingest the same period, but with a malformed row and a dropped
        # issuer (which would trigger a stale delete if it committed).
        data = sample_report()
        data["states"]["TX"]["issuers"] = data["states"]["TX"]["issuers"][:1]
        data["states"]["TX"]["issuers"][0]["composite_score"] = "not-a-number"
        with pytest.raises(Exception):
            load_report_dict(data)
        # The prior report and all its rows survive untouched.
        after = set(WorstInsuranceRanking.objects.values_list("state", "issuer_slug"))
        assert after == before
        assert WorstInsuranceReport.objects.count() == 1


@pytest.mark.django_db
class TestCompanyMatching:
    def test_exact_name_match(self):
        company = InsuranceCompany.objects.create(name="Evil Health Insurance")
        assert match_insurance_company("Evil Health Insurance") == company

    def test_alt_names_match(self):
        company = InsuranceCompany.objects.create(
            name="Evil Health", alt_names="Evil Health Insurance\nEHI"
        )
        assert match_insurance_company("Evil Health Insurance") == company

    def test_substring_match_prefers_more_specific(self):
        InsuranceCompany.objects.create(name="Evil")
        specific = InsuranceCompany.objects.create(name="Evil Health Insurance")
        assert match_insurance_company("Evil Health Insurance") == specific

    def test_unmatched_returns_none(self):
        assert match_insurance_company("Completely Unknown Payer") is None

    def test_unmatched_issuer_loads_with_null_fk_and_counts(self):
        created, _, _, match_failures = load_report_dict(sample_report())
        assert created == 4
        assert match_failures > 0
        assert (
            WorstInsuranceRanking.objects.filter(insurance_company__isnull=True).count()
            == created
        )

    def test_matched_issuer_gets_fk(self):
        company = InsuranceCompany.objects.create(name="Evil Health Insurance")
        load_report_dict(sample_report())
        row = WorstInsuranceRanking.objects.get(state="TX", rank=1)
        assert row.insurance_company == company


@pytest.mark.django_db
class TestManagementCommand:
    def test_ingests_from_file(self):
        out = StringIO()
        call_command("ingest_worst_insurance", file=str(SAMPLE_PATH), stdout=out)
        assert WorstInsuranceReport.objects.filter(period="2026-07").exists()
        assert "4 created" in out.getvalue()

    def test_rejects_unsupported_schema(self, tmp_path):
        data = sample_report()
        data["schema_version"] = 99
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(data), encoding="utf-8")
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="schema_version"):
            call_command("ingest_worst_insurance", file=str(bad), stdout=StringIO())

    def test_requires_url_or_file(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="--url or --file"):
            call_command("ingest_worst_insurance", stdout=StringIO())

    def test_rejects_both_url_and_file(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="not both"):
            call_command(
                "ingest_worst_insurance",
                url="https://example.com/rankings.json",
                file=str(SAMPLE_PATH),
                stdout=StringIO(),
            )

    def test_ingests_from_url(self):
        from unittest.mock import patch

        out = StringIO()
        with patch(
            "fighthealthinsurance.management.commands.ingest_worst_insurance.fetch_json",
            return_value=sample_report(),
        ) as mock_fetch:
            call_command(
                "ingest_worst_insurance",
                url="https://example.com/rankings.json",
                stdout=out,
            )
        mock_fetch.assert_called_once_with("https://example.com/rankings.json")
        report = WorstInsuranceReport.objects.get(period="2026-07")
        assert report.source_url == "https://example.com/rankings.json"
        assert "4 created" in out.getvalue()

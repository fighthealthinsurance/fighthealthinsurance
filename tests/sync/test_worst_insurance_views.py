"""Tests for the public worst-insurance rankings pages.

The pages must stay unpublished (404, absent from the sitemap) until a
rankings report has been ingested.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from django.test import Client
from django.urls import reverse

from fighthealthinsurance.state_help import StateHelp
from fighthealthinsurance.worst_insurance_ingest import load_report_dict

SAMPLE_PATH = Path(__file__).resolve().parent / "worst_insurance_rankings_sample.json"


def _state_help(slug: str, name: str, abbreviation: str) -> StateHelp:
    return StateHelp(
        {
            "slug": slug,
            "name": name,
            "abbreviation": abbreviation,
            "insurance_department": {
                "name": f"{name} Department of Insurance",
                "url": "https://example.gov/",
                "phone": "555-0100",
                "consumer_line": "555-0101",
            },
            "consumer_assistance": {},
            "medicaid": {},
        }
    )


STATE_HELP_FIXTURE = {
    "texas": _state_help("texas", "Texas", "TX"),
    "california": _state_help("california", "California", "CA"),
    "wyoming": _state_help("wyoming", "Wyoming", "WY"),
}


@pytest.fixture(autouse=True)
def stub_state_help():
    """state_help.json isn't available via staticfiles in tests (the existing
    state-help tests mock the same layer)."""
    with patch(
        "fighthealthinsurance.state_help.load_state_help",
        return_value=STATE_HELP_FIXTURE,
    ):
        yield


def load_sample_report(**overrides):
    with open(SAMPLE_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data.update(overrides)
    load_report_dict(data)
    return data


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
class TestDarkLaunch:
    """Without ingested data the feature must be invisible."""

    def test_index_404s_without_data(self, client):
        response = client.get(reverse("worst_insurance_index"))
        assert response.status_code == 404

    def test_state_page_404s_without_data(self, client):
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "texas"})
        )
        assert response.status_code == 404

    def test_methodology_404s_without_data(self, client):
        response = client.get(reverse("worst_insurance_methodology"))
        assert response.status_code == 404

    def test_sitemap_excludes_worst_insurance_without_data(self, client):
        response = client.get("/sitemap.xml")
        assert response.status_code == 200
        assert b"worst-insurance" not in response.content


@pytest.mark.django_db
class TestIndexPage:
    def test_index_renders_with_latest_report(self, client):
        load_sample_report()
        response = client.get(reverse("worst_insurance_index"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "Evil Health Insurance" in content
        assert "2026-07" in content

    def test_index_uses_most_recent_period(self, client):
        load_sample_report()
        newer = load_sample_report(period="2026-08")
        assert newer["period"] == "2026-08"
        response = client.get(reverse("worst_insurance_index"))
        assert "2026-08" in response.content.decode()

    def test_index_marks_states_without_data(self, client):
        load_sample_report()
        response = client.get(reverse("worst_insurance_index"))
        assert "Insufficient public data" in response.content.decode()

    def test_index_card_shows_estimated_hours_with_thousands_separators(self, client):
        load_sample_report()
        response = client.get(reverse("worst_insurance_index"))
        content = response.content.decode()
        assert "est. ~250,000 hrs/yr" in content
        assert "250000" not in content


@pytest.mark.django_db
class TestStatePage:
    def test_state_page_lists_rankings_in_rank_order(self, client):
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "texas"})
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "Evil Health Insurance" in content
        assert "Nice Health Plan" in content
        assert content.index("Evil Health Insurance") < content.index(
            "Nice Health Plan"
        )

    def test_state_page_shows_metric_details(self, client):
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "texas"})
        )
        content = response.content.decode()
        # 0.31 denial rate renders as 31.0% with its numerator/denominator.
        assert "31.0%" in content
        # Covered member-months carry thousands separators.
        assert "1,200,000" in content

    def test_state_page_shows_estimated_hours_headline(self, client):
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "texas"})
        )
        content = response.content.decode()
        # Estimated hours (250,000) surfaced with the measured-floor breakdown.
        assert "250,000 hours" in content
        assert "10,000 from overturned appeals" in content
        # Framed as an estimate, not a fact.
        assert "estimate" in content.lower()

    def test_state_hero_omits_hours_when_top_issuer_lacks_time_burden(self, client):
        """California's worst issuer is ranked on enforcement actions only;
        the hero must not present that metric as "hours"."""
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "california"})
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "Evil Health Insurance ranks worst in California" in content
        assert "4.4 hours" not in content
        assert "denials cost California" not in content
        # The enforcement metric still renders in the table itself.
        assert "4 actions, $250,000 in penalties" in content

    def test_known_state_without_rankings_shows_insufficient_data(self, client):
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "wyoming"})
        )
        assert response.status_code == 200
        assert "public data" in response.content.decode()

    def test_unknown_state_slug_returns_404(self, client):
        load_sample_report()
        response = client.get(
            reverse("worst_insurance_state", kwargs={"slug": "atlantis"})
        )
        assert response.status_code == 404


@pytest.mark.django_db
class TestMethodologyPage:
    def test_methodology_renders_with_report_parameters(self, client):
        load_sample_report()
        response = client.get(reverse("worst_insurance_methodology"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "test.v1" in content
        assert "CMS Transparency in Coverage PUF" in content

    def test_methodology_shows_time_burden_citations(self, client):
        load_sample_report()
        response = client.get(reverse("worst_insurance_methodology"))
        content = response.content.decode()
        # The cited assumptions and their sources render as clickable links.
        assert "AMA 2024 Prior Authorization" in content
        assert "KFF" in content
        assert 'href="https://www.kff.org/example"' in content


@pytest.mark.django_db
class TestSitemap:
    def test_sitemap_includes_worst_insurance_urls_with_data(self, client):
        load_sample_report()
        response = client.get("/sitemap.xml")
        assert response.status_code == 200
        content = response.content.decode()
        assert "/worst-insurance/" in content
        assert "/worst-insurance/methodology/" in content
        assert "/worst-insurance/texas/" in content
        # Wyoming has no rankings, so its thin page stays out of the sitemap.
        assert "/worst-insurance/wyoming/" not in content


class TestCaching:
    def test_pages_are_cached_like_the_site_s_other_content_pages(self):
        """On StaticIshView, so they follow the site's caching rules (and its
        per-visitor context scrubbing) rather than a second copy of them."""
        from fighthealthinsurance.views import (
            StaticIshView,
            WorstInsuranceIndexView,
            WorstInsuranceMethodologyView,
            WorstInsuranceStateView,
        )

        for view in (
            WorstInsuranceIndexView,
            WorstInsuranceStateView,
            WorstInsuranceMethodologyView,
        ):
            assert issubclass(view, StaticIshView)

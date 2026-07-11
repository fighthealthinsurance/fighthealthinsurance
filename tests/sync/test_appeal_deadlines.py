"""Tests for the Appeal Deadline Calculator.

Covers the pure deadline computation (every coverage type, expedited vs
standard, pre/post/concurrent service timing, and concrete date math) plus a
smoke test of the public view and its route.
"""

import datetime

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import appeal_deadlines
from fighthealthinsurance.appeal_deadlines import compute_deadlines

# A fixed denial-received date so date math is deterministic.
DENIAL = datetime.date(2026, 1, 1)


# ---------------------------------------------------------------------------
# Coverage-type coverage: every type resolves and produces the three windows.
# ---------------------------------------------------------------------------


def test_every_coverage_type_computes_all_three_windows():
    for coverage_key, _ in appeal_deadlines.COVERAGE_CHOICES:
        result = compute_deadlines(coverage_key, "pre_service", False, DENIAL)
        assert result.insurer_decision.label
        assert result.internal_appeal_filing.label
        assert result.external_review_filing.label
        assert result.last_reviewed == appeal_deadlines.LAST_REVIEWED


def test_unknown_coverage_type_raises():
    try:
        compute_deadlines("definitely_not_real", "pre_service", False, DENIAL)
    except ValueError:
        pass
    else:  # pragma: no cover - the assert makes intent explicit
        raise AssertionError("expected ValueError for unknown coverage type")


def test_unknown_timing_raises():
    try:
        compute_deadlines("employer_erisa", "sideways", False, DENIAL)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown timing")


# ---------------------------------------------------------------------------
# ERISA / commercial: 180-day internal appeal, 120-day (4 month) external.
# ---------------------------------------------------------------------------


def test_erisa_internal_appeal_is_180_days_from_denial():
    result = compute_deadlines("employer_erisa", "post_service", False, DENIAL)
    assert result.internal_appeal_filing.deadline == DENIAL + datetime.timedelta(
        days=180
    )


def test_erisa_external_review_is_120_days_from_denial():
    result = compute_deadlines("employer_erisa", "post_service", False, DENIAL)
    assert result.external_review_filing.deadline == DENIAL + datetime.timedelta(
        days=120
    )


def test_commercial_aca_internal_appeal_is_180_days():
    result = compute_deadlines("commercial_aca", "pre_service", False, DENIAL)
    assert result.internal_appeal_filing.deadline == DENIAL + datetime.timedelta(
        days=180
    )


# ---------------------------------------------------------------------------
# pre/post-service change the insurer decision window (30 vs 60 days for ACA).
# ---------------------------------------------------------------------------


def test_aca_pre_service_decision_window_differs_from_post_service():
    pre = compute_deadlines("commercial_aca", "pre_service", False, DENIAL)
    post = compute_deadlines("commercial_aca", "post_service", False, DENIAL)
    assert "30 days" in pre.insurer_decision.label
    assert "60 days" in post.insurer_decision.label


# ---------------------------------------------------------------------------
# Expedited overrides timing and yields the urgent (72-hour) decision window.
# ---------------------------------------------------------------------------


def test_expedited_yields_urgent_decision_window_regardless_of_timing():
    for timing in ("pre_service", "post_service", "concurrent"):
        result = compute_deadlines("commercial_aca", timing, True, DENIAL)
        assert "72 hours" in result.insurer_decision.label
        assert result.expedited is True
        assert "expedited" in result.situation_label.lower()


def test_expedited_urgent_window_has_no_calendar_deadline():
    # Hours-based urgent windows are labels, not dates.
    result = compute_deadlines("employer_erisa", "pre_service", True, DENIAL)
    assert result.insurer_decision.deadline is None


# ---------------------------------------------------------------------------
# Medicare Advantage / Part D: tighter 60-day internal filing window.
# ---------------------------------------------------------------------------


def test_medicare_internal_appeal_is_60_days():
    result = compute_deadlines(
        "medicare_advantage_partd", "pre_service", False, DENIAL
    )
    assert result.internal_appeal_filing.deadline == DENIAL + datetime.timedelta(
        days=60
    )


def test_medicare_external_review_shows_no_concrete_date():
    # The Part D 60-day IRE clock runs from the plan's redetermination decision,
    # not from the denial-received date, so we must not emit a concrete date
    # even though a denial date was supplied.
    result = compute_deadlines(
        "medicare_advantage_partd", "pre_service", False, DENIAL
    )
    assert result.external_review_filing.deadline is None
    assert result.external_review_filing.varies is True
    # The plain-language label is still shown so the row is informative.
    assert result.external_review_filing.label


# ---------------------------------------------------------------------------
# Medicaid: figures are flagged as varying by state.
# ---------------------------------------------------------------------------


def test_medicaid_windows_marked_as_varying():
    result = compute_deadlines("medicaid", "post_service", False, DENIAL)
    assert result.internal_appeal_filing.varies is True
    assert result.external_review_filing.varies is True


# ---------------------------------------------------------------------------
# No denial date -> plain-language windows, but no concrete calendar deadlines.
# ---------------------------------------------------------------------------


def test_missing_denial_date_yields_no_deadline_dates():
    result = compute_deadlines("commercial_aca", "pre_service", False, None)
    assert result.internal_appeal_filing.deadline is None
    assert result.external_review_filing.deadline is None
    # Labels are still populated so the page is informative without a date.
    assert result.internal_appeal_filing.label


# ---------------------------------------------------------------------------
# View smoke tests.
# ---------------------------------------------------------------------------


class AppealDeadlineCalculatorViewTest(TestCase):
    def setUp(self) -> None:
        self.client = Client()
        self.url = reverse("appeal_deadline_calculator")

    def test_route_resolves(self) -> None:
        assert self.url == "/tools/appeal-deadline-calculator"

    def test_get_blank_page_renders_form_without_results(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Appeal Deadline Calculator")
        self.assertContains(response, "deadline_calculator_form")
        # No coverage submitted -> no results block.
        self.assertNotContains(response, 'id="results"')

    def test_get_with_valid_inputs_renders_results_and_cta(self) -> None:
        response = self.client.get(
            self.url,
            {
                "coverage_type": "employer_erisa",
                "service_timing": "post_service",
                "denial_received": "2026-01-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results"')
        # 180 days after 2026-01-01 is 2026-06-30.
        self.assertContains(response, "June 30, 2026")
        # CTA into the appeal-generation flow.
        self.assertContains(response, reverse("scan"))

    def test_json_ld_and_disclaimer_present(self) -> None:
        response = self.client.get(self.url)
        self.assertContains(response, "application/ld+json")
        self.assertContains(response, "WebApplication")
        self.assertContains(response, "General information only")

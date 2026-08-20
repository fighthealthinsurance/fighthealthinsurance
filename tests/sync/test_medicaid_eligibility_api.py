"""Tests for the approximate Medicaid/Medicare eligibility checker.

These cover the stepwise-questioning flow the chat tool drives: the checker
must either produce a verdict or ask for more information -- never dead-end
with "not eligible" plus an empty question list just because a question was
never going to be asked (the pre-2026-08 stall bugs).
"""

from django.test import TestCase

from fighthealthinsurance.medicaid_api import get_medicaid_info, is_eligible


def _answers(**overrides):
    """Fully-answered baseline for a 30-year-old single adult in California."""
    base = dict(
        state="ca",
        age=30,
        married=False,
        household_size=1,
        monthly_income=1200.0,
        pregnant=False,
        children_in_household=0,
        receiving_ssdi=False,
        esrd=False,
        als=False,
    )
    base.update(overrides)
    return base


class TestExpansionAdultFlow(TestCase):
    """MAGI adults in expansion states."""

    def test_low_income_expansion_adult_is_eligible_2025(self):
        eligible_2025, _, _, _, missing = is_eligible(**_answers())
        self.assertTrue(eligible_2025)

    def test_flow_never_dead_ends_without_questions(self):
        # Regression: on_medicare/pregnant were required internally but never
        # asked for under-65 adults, so a fully-cooperative user got
        # "not eligible" with zero remaining questions.
        eligible_2025, eligible_2026, _, _, missing = is_eligible(**_answers())
        self.assertTrue(eligible_2025 or len(missing) > 0)

    def test_2026_requires_work_hours_question(self):
        _, eligible_2026, _, _, missing = is_eligible(**_answers())
        self.assertFalse(eligible_2026)
        self.assertTrue(any("qualifying hours" in q for q in missing))

    def test_2026_eligible_with_sufficient_hours(self):
        _, eligible_2026, _, _, missing = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=25.0)
        )
        self.assertTrue(eligible_2026)
        self.assertEqual(missing, [])

    def test_2026_not_eligible_with_insufficient_hours(self):
        _, eligible_2026, _, alts, _ = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=10.0)
        )
        self.assertFalse(eligible_2026)
        self.assertTrue(any("80" in a for a in alts))

    def test_massachusetts_is_an_expansion_state(self):
        # Regression: MA was missing from the expansion list.
        eligible_2025, _, _, _, _ = is_eligible(**_answers(state="ma"))
        self.assertTrue(eligible_2025)

    def test_north_dakota_is_an_expansion_state(self):
        # Regression: ND was missing from the expansion list.
        eligible_2025, _, _, _, _ = is_eligible(**_answers(state="nd"))
        self.assertTrue(eligible_2025)

    def test_alabama_is_not_an_expansion_state(self):
        # Regression: AL was wrongly listed as expanded.
        eligible_2025, _, _, _, _ = is_eligible(**_answers(state="al"))
        self.assertFalse(eligible_2025)

    def test_wisconsin_waiver_covers_adults_under_poverty_line(self):
        # WI has no ACA expansion but covers adults to 100% FPL via waiver.
        eligible_2025, _, _, _, _ = is_eligible(**_answers(state="wi"))
        self.assertTrue(eligible_2025)

    def test_coverage_gap_alternative_mentions_health_centers(self):
        # Below 100% FPL in a non-expansion state there are no marketplace
        # subsidies; the guidance must not pretend there are.
        _, _, _, alts, _ = is_eligible(**_answers(state="tx"))
        self.assertTrue(any("coverage gap" in a for a in alts))


class TestQuestionFlow(TestCase):
    """Stepwise questioning behavior."""

    def test_answering_no_to_ssdi_is_not_reasked(self):
        # Regression: `get_bool("receiving_ssdi") or get_bool("disabled")`
        # coerced an explicit False back to None, re-asking forever.
        _, _, _, _, missing = is_eligible(state="ca", age=30, receiving_ssdi=False)
        self.assertFalse(any("SSDI" in q for q in missing))

    def test_young_child_not_asked_about_pregnancy(self):
        _, _, _, _, missing = is_eligible(state="wa", age=2)
        self.assertFalse(any("pregnant" in q.lower() for q in missing))

    def test_unrecognized_state_becomes_a_reask_not_an_exception(self):
        # A garbled state from the LLM used to raise ValueError and kill the
        # whole check; now it just re-asks for the state.
        _, _, _, _, missing = is_eligible(state="medi-cal er california", age=30)
        self.assertTrue(any("state" in q.lower() for q in missing))

    def test_young_child_verdict_does_not_stall_on_pregnancy(self):
        eligible_2025, _, _, _, missing = is_eligible(
            state="wa",
            age=2,
            household_size=3,
            monthly_income=2500.0,
            children_in_household=1,
            receiving_ssdi=False,
            esrd=False,
            als=False,
        )
        self.assertTrue(eligible_2025)

    def test_sixty_five_year_old_is_asked_about_medicare(self):
        # Regression: the medicare question used `age > 65`, skipping
        # people who are exactly 65.
        _, _, _, _, missing = is_eligible(
            **_answers(age=65, esrd=None, als=None, receiving_ssdi=False)
        )
        self.assertTrue(any("Medicare" in q for q in missing))

    def test_missing_questions_are_deduplicated(self):
        # The assets question is raised by both the stepwise 65+ ask and the
        # ABD evaluation; the caller should only ever see it once.
        _, _, _, _, missing = is_eligible(
            **_answers(age=70, on_medicare=True, years_worked=15)
        )
        self.assertEqual(len(missing), len(set(missing)))
        self.assertEqual(
            len([q for q in missing if "countable financial assets" in q]), 1
        )


class TestMedicarePathways(TestCase):
    """Medicare eligibility determinations."""

    def test_ssdi_24_months_confers_medicare(self):
        _, _, medicare, _, _ = is_eligible(
            **_answers(
                age=50,
                receiving_ssdi=True,
                ssdi_length=30,
                on_medicare=False,
                assets_total=1000.0,
            )
        )
        self.assertTrue(medicare)

    def test_ten_years_worked_at_65_confers_medicare(self):
        # Regression: the check was `years_worked > 10`, and was only
        # evaluated while the on_medicare question was still unanswered.
        _, _, medicare, _, _ = is_eligible(
            **_answers(
                age=67,
                on_medicare=False,
                years_worked=10,
                assets_total=1000.0,
            )
        )
        self.assertTrue(medicare)

    def test_als_confers_medicare(self):
        _, _, medicare, _, _ = is_eligible(
            **_answers(als=True, on_medicare=False, assets_total=500.0)
        )
        self.assertTrue(medicare)

    def test_under_ten_years_worked_suggests_medicare_savings_programs(self):
        _, _, medicare, alts, _ = is_eligible(
            **_answers(
                age=70,
                on_medicare=False,
                years_worked=5,
                assets_total=1000.0,
            )
        )
        self.assertFalse(medicare)
        self.assertTrue(any("Part-A" in a or "Medicare Savings" in a for a in alts))


class TestLongTermCareFlow(TestCase):
    """LTC pathway checks."""

    def _ltc_answers(self, **overrides):
        base = _answers(
            age=80,
            on_medicare=True,
            applying_reason="ltc_nursing_home",
            living_situation="nursing_home_perm",
            assets_total=0.0,
            home_owner=False,
            monthly_income=1500.0,
        )
        base.update(overrides)
        return base

    def test_zero_assets_pass_the_ltc_asset_test(self):
        # Regression: `if not assets_total` treated $0 (the most-eligible
        # case) as missing info and failed the asset test.
        eligible_2025, eligible_2026, _, _, missing = is_eligible(
            **self._ltc_answers()
        )
        self.assertTrue(eligible_2025)
        self.assertTrue(eligible_2026)
        self.assertEqual(missing, [])

    def test_elderly_ltc_applicant_uses_ltc_income_cap_not_abd(self):
        # Regression: 65+ applicants matched the ABD branch first and never
        # reached the LTC rules. $2,500/month fails the ABD 100%-FPL test but
        # passes the ~$3,000 LTC cap.
        eligible_2025, _, _, _, _ = is_eligible(
            **self._ltc_answers(state="wy", monthly_income=2500.0)
        )
        self.assertTrue(eligible_2025)

    def test_income_over_ltc_cap_suggests_miller_trust(self):
        _, _, _, alts, _ = is_eligible(
            **self._ltc_answers(monthly_income=3500.0)
        )
        self.assertTrue(any("Miller trust" in a for a in alts))


class TestGetMedicaidInfo(TestCase):
    """State info lookups keep working with the shared state map."""

    def test_lookup_by_abbreviation(self):
        result = get_medicaid_info({"state": "CA", "topic": "", "limit": 5})
        self.assertIn("California", result)

    def test_lookup_dc_alias_resolves_to_district_of_columbia_data(self):
        # "washington, dc" -> "dc" -> "District of Columbia" display name,
        # which is what the CSV rows are keyed on.
        result = get_medicaid_info({"state": "washington, dc", "topic": "", "limit": 5})
        self.assertIn("Health Care Finance", result)

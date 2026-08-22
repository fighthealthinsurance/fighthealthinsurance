"""Tests for the approximate Medicaid/Medicare eligibility checker.

These cover the stepwise-questioning flow the chat tool drives: the checker
must either produce a verdict or ask for more information -- never dead-end
with "not eligible" plus an empty question list just because a question was
never going to be asked (the pre-2026-08 stall bugs).
"""

# SimpleTestCase: is_eligible is pure computation and get_medicaid_info reads
# a CSV -- no ORM, so skip TestCase's per-test transaction machinery.
from django.test import SimpleTestCase

from fighthealthinsurance.medicaid_api import (
    get_medicaid_info,
    is_eligible,
    summarize_eligibility_inputs,
)


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


class TestExpansionAdultFlow(SimpleTestCase):
    """MAGI adults in expansion states."""

    def test_low_income_expansion_adult_is_eligible_2025(self):
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers())
        self.assertTrue(eligible_2025)

    def test_flow_never_dead_ends_without_questions(self):
        # Regression: on_medicare/pregnant were required internally but never
        # asked for under-65 adults, so a fully-cooperative user got
        # "not eligible" with zero remaining questions.
        eligible_2025, _, _, _, missing, _ = is_eligible(**_answers())
        self.assertTrue(eligible_2025 or len(missing) > 0)

    def test_2026_requires_work_hours_question(self):
        _, eligible_2026, _, _, missing, _ = is_eligible(**_answers())
        self.assertFalse(eligible_2026)
        self.assertTrue(any("qualifying hours" in q for q in missing))

    def test_2026_eligible_with_sufficient_hours(self):
        _, eligible_2026, _, _, missing, _ = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=25.0)
        )
        self.assertTrue(eligible_2026)
        self.assertEqual(missing, [])

    def test_2026_not_eligible_with_insufficient_hours(self):
        _, eligible_2026, _, alts, _, _ = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=10.0)
        )
        self.assertFalse(eligible_2026)
        self.assertTrue(any("80" in a for a in alts))

    def test_massachusetts_is_an_expansion_state(self):
        # Regression: MA was missing from the expansion list.
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="ma"))
        self.assertTrue(eligible_2025)

    def test_north_dakota_is_an_expansion_state(self):
        # Regression: ND was missing from the expansion list.
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="nd"))
        self.assertTrue(eligible_2025)

    def test_alabama_is_not_an_expansion_state(self):
        # Regression: AL was wrongly listed as expanded.
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="al"))
        self.assertFalse(eligible_2025)

    def test_wisconsin_waiver_covers_adults_under_poverty_line(self):
        # WI has no ACA expansion but covers adults to 100% FPL via waiver.
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="wi"))
        self.assertTrue(eligible_2025)

    def test_coverage_gap_alternative_mentions_health_centers(self):
        # Below 100% FPL in a non-expansion state there are no marketplace
        # subsidies; the guidance must not pretend there are.
        _, _, _, alts, _, _ = is_eligible(**_answers(state="tx"))
        self.assertTrue(any("coverage gap" in a for a in alts))

    def test_wisconsin_over_waiver_limit_gets_marketplace_alternative(self):
        # Review regression: the WI waiver branch had no not-eligible arm, so
        # WI adults over 100% FPL got no subsidy pointer at all.
        eligible_2025, _, _, alts, _, _ = is_eligible(
            **_answers(state="wi", monthly_income=1600.0)
        )
        self.assertFalse(eligible_2025)
        self.assertTrue(any("marketplace subsidies" in a for a in alts))

    def test_ssdi_adult_in_expansion_state_keeps_magi_pathway(self):
        # Review regression: answering yes to SSDI routed 19-64 adults into
        # the asset-tested ABD branch only; SSDI receipt does not bar the
        # MAGI expansion pathway (income-only, up to 138% FPL).
        eligible_2025, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(
                state="co",
                monthly_income=1500.0,
                receiving_ssdi=True,
                ssdi_length=12,
                on_medicare=False,
                assets_total=5000.0,
            )
        )
        self.assertTrue(eligible_2025)
        self.assertTrue(eligible_2026)

    def test_monthly_hours_below_threshold_is_not_eligible_2026(self):
        # Review regression: the tool asks for hours per MONTH but only a
        # per-week kwarg existed, inviting a 4x unit error.
        _, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(avg_monthly_qualifying_hours_last_3mo=60.0)
        )
        self.assertFalse(eligible_2026)

    def test_monthly_hours_above_threshold_is_eligible_2026(self):
        _, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(avg_monthly_qualifying_hours_last_3mo=90.0)
        )
        self.assertTrue(eligible_2026)

    def test_weekly_hours_list_agrees_with_weekly_average(self):
        # Review regression: the scalar path used a 13-week quarter while the
        # weekly-list path bucketed 4-week months, so the same 19.5 hrs/week
        # got opposite verdicts depending on which kwarg the model filled --
        # and the user with detailed records got the harsher answer.
        _, from_average, _, _, _, _ = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=19.5)
        )
        _, from_list, _, _, _, _ = is_eligible(
            **_answers(qualifying_hours_weekly_last_12=[19.5] * 12)
        )
        self.assertEqual(from_average, from_list)

    def test_weekly_hours_keep_month_to_month_variation(self):
        # Review finding: averaging the whole window and repeating it let
        # [60]*4 + [0]*8 -- two genuinely empty months -- pass a rule that
        # requires each month to reach 80 hours.
        _, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(qualifying_hours_weekly_last_12=[60.0] * 4 + [0.0] * 8)
        )
        self.assertFalse(eligible_2026)

    def test_weekly_hours_list_does_not_drop_extra_weeks(self):
        # Bucketing by index silently discarded anything past the 12th entry.
        _, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(qualifying_hours_weekly_last_12=[0.0] * 4 + [10.0] * 12)
        )
        self.assertFalse(eligible_2026)

    def test_weekly_hours_use_thirteen_week_quarter(self):
        # Review regression: 4-weeks-per-month undercounted real months by
        # ~8%. 19 hrs/week is ~82.3 hrs per calendar month, which meets 80.
        _, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(avg_weekly_qualifying_hours_last_3mo=19.0)
        )
        self.assertTrue(eligible_2026)

    def test_esrd_patient_exempt_from_work_requirement(self):
        # Review regression: ESRD/ALS (medically frail) were subjected to the
        # 2026 work-hours demand.
        _, eligible_2026, _, _, missing, _ = is_eligible(
            **_answers(esrd=True, on_medicare=False, assets_total=500.0)
        )
        self.assertTrue(eligible_2026)
        self.assertFalse(any("hours" in q for q in missing))


class TestQuestionFlow(SimpleTestCase):
    """Stepwise questioning behavior."""

    def test_answering_no_to_ssdi_is_not_reasked(self):
        # Regression: `get_bool("receiving_ssdi") or get_bool("disabled")`
        # coerced an explicit False back to None, re-asking forever.
        _, _, _, _, missing, _ = is_eligible(state="ca", age=30, receiving_ssdi=False)
        self.assertFalse(any("SSDI" in q for q in missing))

    def test_young_child_not_asked_about_pregnancy(self):
        _, _, _, _, missing, _ = is_eligible(state="wa", age=2)
        self.assertFalse(any("pregnant" in q.lower() for q in missing))

    def test_unrecognized_state_becomes_a_reask_not_an_exception(self):
        # A garbled state from the LLM used to raise ValueError and kill the
        # whole check; now it just re-asks for the state.
        _, _, _, _, missing, _ = is_eligible(state="medi-cal er california", age=30)
        self.assertTrue(any("state" in q.lower() for q in missing))

    def test_young_child_verdict_does_not_stall_on_pregnancy(self):
        eligible_2025, _, _, _, missing, _ = is_eligible(
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
        # The stall this name refers to: pregnancy was required by the
        # completeness gate but never asked for a 2-year-old, so the flow
        # dead-ended. Assert it is neither asked nor outstanding.
        self.assertEqual(missing, [])

    def test_sixty_five_year_old_is_asked_about_medicare(self):
        # Regression: the medicare question used `age > 65`, skipping
        # people who are exactly 65.
        _, _, _, _, missing, _ = is_eligible(
            **_answers(age=65, esrd=None, als=None, receiving_ssdi=False)
        )
        self.assertTrue(any("Medicare" in q for q in missing))

    def test_missing_questions_are_deduplicated(self):
        # The assets question is raised by both the stepwise 65+ ask and the
        # ABD evaluation; the caller should only ever see it once.
        _, _, _, _, missing, _ = is_eligible(
            **_answers(age=70, on_medicare=True, years_worked=15)
        )
        self.assertEqual(len(missing), len(set(missing)))
        self.assertEqual(
            len([q for q in missing if "countable financial assets" in q]), 1
        )

    def test_string_no_is_treated_as_no(self):
        # Review regression: bool("no") is True, so LLM string booleans
        # flipped answers. A string "no" must behave like False.
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="tx", pregnant="no"))
        self.assertFalse(eligible_2025)

    def test_string_false_work_exemption_is_not_treated_as_exempt(self):
        # Review regression: work_req_exempt_2026="false" was truthy and
        # skipped the work-hours questions entirely.
        _, eligible_2026, _, _, missing, _ = is_eligible(
            **_answers(work_req_exempt_2026="false")
        )
        self.assertFalse(eligible_2026)
        self.assertTrue(any("hours" in q for q in missing))

    def test_home_equity_question_asked_only_once(self):
        # Review regression: two divergent phrasings of the home-equity
        # question survived dedup and got asked twice in one message.
        _, _, _, _, missing, _ = is_eligible(
            **_answers(
                age=80,
                on_medicare=True,
                applying_reason="ltc_nursing_home",
                living_situation="nursing_home_perm",
                assets_total=0.0,
                home_owner=True,
                monthly_income=1500.0,
            )
        )
        self.assertEqual(len([q for q in missing if "equity" in q]), 1)


class TestLlmPayloadParsing(SimpleTestCase):
    """Answers arrive as raw LLM JSON, so loose values must not stall the flow."""

    def test_word_answer_to_married_question_is_understood(self):
        # The question is literally "Are you married or single?", so "single"
        # is the expected answer -- and an unrecognized string reads as
        # unanswered, which re-asked the same question every turn forever.
        _, _, _, _, missing, _ = is_eligible(**_answers(married="single"))
        self.assertFalse(any("married" in q for q in missing))

    def test_empty_string_is_unanswered_not_no(self):
        # "" is the likeliest "I don't have this value" placeholder; recording
        # it as a definitive no silently routed a pregnant applicant down the
        # able-bodied pathway with no question left to correct it.
        _, _, _, _, missing, _ = is_eligible(**_answers(pregnant=""))
        self.assertTrue(any("pregnant" in q.lower() for q in missing))

    def test_currency_formatted_income_is_parsed(self):
        # "$1,200" raised in float(), became None, and re-asked forever.
        _, _, _, _, missing, _ = is_eligible(**_answers(monthly_income="$1,200"))
        self.assertFalse(any("monthly income" in q for q in missing))

    def test_abbreviated_thousands_income_is_parsed(self):
        summary = summarize_eligibility_inputs({"monthly_income": "1.2k"})
        self.assertEqual(summary["recorded"]["monthly_income"], 1200.0)

    def test_spelled_out_household_size_is_parsed(self):
        _, _, _, _, missing, _ = is_eligible(**_answers(household_size="three"))
        self.assertFalse(any("household size" in q for q in missing))

    def test_territory_resident_is_not_asked_for_a_state_forever(self):
        # Puerto Rico has Medicaid but never parsed as a state, so the flow
        # re-asked a question the resident had already answered correctly.
        _, _, _, alts, missing, _ = is_eligible(state="Puerto Rico", age=30)
        self.assertFalse(any("state do you live" in q for q in missing))
        self.assertTrue(any("territor" in a.lower() for a in alts))

    def test_territory_result_is_not_a_determination(self):
        # Review finding: the territory exit returned False/False with no
        # questions left, which the chat tool rendered as a confident "may
        # not be eligible" -- contradicting the alternative it returned.
        _, _, _, _, _, determination_made = is_eligible(
            state="Puerto Rico", age=30
        )
        self.assertFalse(determination_made)

    def test_territory_resident_still_gets_a_medicare_answer(self):
        # Medicare is federal and does operate in PR/GU/VI/AS/MP, so the
        # territory exit must not skip the Medicare pathway.
        _, _, medicare, _, _, _ = is_eligible(
            **_answers(state="Puerto Rico", age=67, on_medicare=True)
        )
        self.assertTrue(medicare)


class TestUnknownAnswerChannel(SimpleTestCase):
    """The model needs a way to say "asked, and the user can't answer"."""

    def test_declined_optional_field_is_not_reasked(self):
        # Without this channel the model has nothing valid to send for "I
        # don't know", so the question returns every turn forever.
        _, _, _, _, missing, _ = is_eligible(**_answers(married="unknown"))
        self.assertFalse(any("married" in q for q in missing))

    def test_declined_optional_field_still_yields_a_verdict(self):
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(married="unknown"))
        self.assertTrue(eligible_2025)

    def test_declined_optional_field_discloses_the_assumption(self):
        _, _, _, alts, _, _ = is_eligible(**_answers(married="prefer not to say"))
        self.assertTrue(any("assumed the most conservative" in a for a in alts))

    def test_declined_required_field_is_not_reasked(self):
        _, _, _, _, missing, _ = is_eligible(
            **_answers(household_size="don't know", monthly_income="idk")
        )
        self.assertFalse(any("household" in q for q in missing))

    def test_declined_required_field_explains_the_blocked_estimate(self):
        # Not a silent "not eligible" dead end -- say what's missing.
        _, _, _, alts, _, _ = is_eligible(
            **_answers(household_size="don't know", monthly_income="idk")
        )
        self.assertTrue(any("can't estimate eligibility without" in a for a in alts))

    def test_declined_assets_question_is_not_reasked(self):
        _, _, _, _, missing, _ = is_eligible(
            **_answers(
                age=70,
                on_medicare=True,
                years_worked=20,
                assets_total="prefer not to say",
            )
        )
        self.assertFalse(any("assets" in q for q in missing))


class TestIndeterminateResults(SimpleTestCase):
    """"Couldn't score them" must never be rendered as "not eligible"."""

    def test_scored_ineligible_is_a_determination(self):
        _, _, _, _, _, determination_made = is_eligible(
            **_answers(state="tx", monthly_income=1100.0)
        )
        self.assertTrue(determination_made)

    def test_declined_required_field_is_not_a_determination(self):
        _, _, _, _, _, determination_made = is_eligible(
            **_answers(household_size="don't know", monthly_income="idk")
        )
        self.assertFalse(determination_made)

    def test_declined_home_ownership_does_not_assume_no_home(self):
        # Review finding: defaulting home_owner to False skipped the LTC
        # home-equity test, so someone with equity over the cap could get a
        # false "probably eligible". It must stay indeterminate instead.
        _, _, _, _, _, determination_made = is_eligible(
            **_answers(
                age=80,
                on_medicare=True,
                applying_reason="ltc_nursing_home",
                assets_total=0.0,
                home_owner="unknown",
                monthly_income=1500.0,
            )
        )
        self.assertFalse(determination_made)


class TestParsedInputFeedback(SimpleTestCase):
    """The model parses user text, so it needs to see what actually landed."""

    def test_normalized_values_are_reported_back(self):
        summary = summarize_eligibility_inputs(
            {"state": "Medi-Cal", "monthly_income": "$1,200", "married": "single"}
        )
        self.assertEqual(summary["recorded"]["state"], "ca")
        self.assertEqual(summary["recorded"]["monthly_income"], 1200.0)
        self.assertFalse(summary["recorded"]["married"])

    def test_unrecognized_parameter_names_are_reported(self):
        # Silently dropping these looks to the model exactly like acceptance,
        # so it believes it answered and the question comes back.
        summary = summarize_eligibility_inputs({"income": 999, "state": "ca"})
        self.assertEqual(summary["unrecognized"], ["income"])

    def test_unreadable_values_are_reported(self):
        summary = summarize_eligibility_inputs({"age": "thirtyish"})
        self.assertEqual(summary["unreadable"], ["age"])

    def test_declined_fields_are_reported(self):
        summary = summarize_eligibility_inputs({"assets_total": "unknown"})
        self.assertEqual(summary["declined"], ["assets_total"])


class TestStateProgramNames(SimpleTestCase):
    """Program names are what users actually say; resolve them as a backstop."""

    def test_medi_cal_resolves_to_california(self):
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="Medi-Cal"))
        self.assertTrue(eligible_2025)

    def test_masshealth_resolves_to_massachusetts(self):
        eligible_2025, _, _, _, _, _ = is_eligible(**_answers(state="MassHealth"))
        self.assertTrue(eligible_2025)


class TestQuestionsThatCannotChangeTheAnswer(SimpleTestCase):
    """Questions are turns of a real conversation; don't spend them for nothing."""

    def test_disabled_non_ssdi_user_is_not_asked_for_ssdi_months(self):
        # Answering "disabled, but not on SSDI" made receiving_ssdi True,
        # which asked for SSDI months -- unanswerable, and the prompt forbids
        # the model inventing one, so the conversation never terminated.
        _, _, _, _, missing, _ = is_eligible(
            **_answers(receiving_ssdi=False, disabled=True, assets_total=0.0)
        )
        self.assertFalse(any("SSDI" in q for q in missing))

    def test_disability_duration_without_ssdi_does_not_confer_medicare(self):
        # ssdi_length means SSDI months; a non-SSDI disability duration must
        # not satisfy the 24-month Medicare pathway.
        _, _, medicare, _, _, _ = is_eligible(
            **_answers(
                receiving_ssdi=False, disabled=True, ssdi_length=24, assets_total=0.0
            )
        )
        self.assertFalse(medicare)

    def test_healthy_young_adult_is_not_asked_about_kidney_failure(self):
        _, _, _, _, missing, _ = is_eligible(**_answers(esrd=None, als=None))
        self.assertFalse(any("renal" in q for q in missing))

    def test_ninety_five_year_old_is_not_asked_about_pregnancy(self):
        _, _, _, _, missing, _ = is_eligible(
            **_answers(age=95, pregnant=None, on_medicare=True, assets_total=0.0)
        )
        self.assertFalse(any("pregnant" in q.lower() for q in missing))

    def test_child_is_not_asked_for_countable_assets(self):
        # Only the ABD branch reads assets; children are scored on income.
        _, _, _, _, missing, _ = is_eligible(
            **_answers(age=10, receiving_ssdi=True, ssdi_length=6)
        )
        self.assertFalse(any("countable financial assets" in q for q in missing))


class TestMedicarePathways(SimpleTestCase):
    """Medicare eligibility determinations."""

    def test_ssdi_24_months_confers_medicare(self):
        _, _, medicare, _, _, _ = is_eligible(
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
        _, _, medicare, _, _, _ = is_eligible(
            **_answers(
                age=67,
                on_medicare=False,
                years_worked=10,
                assets_total=1000.0,
            )
        )
        self.assertTrue(medicare)

    def test_als_confers_medicare(self):
        _, _, medicare, _, _, _ = is_eligible(
            **_answers(als=True, on_medicare=False, assets_total=500.0)
        )
        self.assertTrue(medicare)

    def test_under_ten_years_worked_suggests_medicare_savings_programs(self):
        _, _, medicare, alts, _, _ = is_eligible(
            **_answers(
                age=70,
                on_medicare=False,
                years_worked=5,
                assets_total=1000.0,
            )
        )
        self.assertFalse(medicare)
        self.assertTrue(any("Part-A" in a or "Medicare Savings" in a for a in alts))

    def test_under_65_already_on_medicare_is_acknowledged(self):
        # Review regression: an under-65 user who said they are ON Medicare
        # was reported not Medicare-eligible (the enrolled branch was only
        # reachable through the other pathways).
        _, _, medicare, _, _, _ = is_eligible(
            **_answers(age=40, monthly_income=800.0, on_medicare=True, assets_total=500.0)
        )
        self.assertTrue(medicare)

    def test_ssdi_recipient_at_67_is_asked_ssdi_months(self):
        # Review regression: the ssdi_length question was only asked under
        # 65, so the 24-month pathway was unreachable for 65+ SSDI
        # recipients and they got a definitive wrong "not eligible".
        _, _, medicare, _, missing, _ = is_eligible(
            **_answers(
                age=67,
                receiving_ssdi=True,
                on_medicare=False,
                years_worked=5,
                assets_total=500.0,
            )
        )
        self.assertTrue(any("months have you been receiving SSDI" in q for q in missing))
        # No definitive verdict while that question is outstanding.
        self.assertFalse(medicare)

    def test_ssdi_recipient_at_67_with_24_months_gets_medicare(self):
        _, _, medicare, _, missing, _ = is_eligible(
            **_answers(
                age=67,
                receiving_ssdi=True,
                ssdi_length=36,
                on_medicare=False,
                years_worked=5,
                assets_total=500.0,
            )
        )
        self.assertTrue(medicare)
        self.assertEqual(missing, [])

    def test_explicit_ssdi_no_does_not_mask_disabled_yes(self):
        # Review regression: receiving_ssdi=False silently discarded
        # disabled=True, dropping the disability pathway and its work
        # exemption.
        eligible_2025, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(
                state="tx",
                monthly_income=800.0,
                receiving_ssdi=False,
                disabled=True,
                ssdi_length=0,
                on_medicare=False,
                assets_total=500.0,
            )
        )
        self.assertTrue(eligible_2025)
        self.assertTrue(eligible_2026)


class TestLongTermCareFlow(SimpleTestCase):
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
        eligible_2025, _, _, _, missing, _ = is_eligible(**self._ltc_answers())
        self.assertTrue(eligible_2025)
        self.assertEqual(missing, [])

    def test_eligible_ltc_applicant_is_exempt_from_2026_work_overlay(self):
        # Split from the asset test: 2026 exemption is a distinct rule
        # (LTC applicants are medically frail, never asked for work hours).
        _, eligible_2026, _, _, missing, _ = is_eligible(**self._ltc_answers())
        self.assertTrue(eligible_2026)
        self.assertFalse(any("hours" in q for q in missing))

    def test_under_65_ltc_applicant_not_asked_for_work_hours(self):
        # Review regression: a 55-year-old permanent nursing-home resident
        # was subjected to the 80-hours-per-month work requirement.
        _, eligible_2026, _, _, missing, _ = is_eligible(
            **self._ltc_answers(age=55, on_medicare=False)
        )
        self.assertTrue(eligible_2026)
        self.assertFalse(any("hours" in q for q in missing))

    def test_ltc_income_between_year_caps_keeps_2026_eligibility(self):
        # Review regression: the work overlay's not-eligible-2025 clamp
        # discarded the LTC branch's own 2026 result. $3,050/month is over
        # the $3,000 2025 cap but under the inflated 2026 cap.
        eligible_2025, eligible_2026, _, _, _, _ = is_eligible(
            **self._ltc_answers(monthly_income=3050.0)
        )
        self.assertFalse(eligible_2025)
        self.assertTrue(eligible_2026)

    def test_ltc_missing_info_return_preserves_medicare_verdict(self):
        # Review regression: the LTC ask-for-more-info early return clobbered
        # an already-computed Medicare verdict back to False.
        _, _, medicare, _, missing, _ = is_eligible(
            **self._ltc_answers(assets_total=None, home_owner=None, living_situation=None)
        )
        self.assertTrue(medicare)
        self.assertTrue(len(missing) > 0)

    def test_child_ltc_applicant_keeps_2026_exemption(self):
        # Review regression: the overlay's LTC bypass keyed on applying_reason,
        # but children are matched earlier in the category chain, so the LTC
        # branch never ran and eligible_2026 kept its False initializer --
        # telling a 10-year-old in a nursing home they may lose coverage in
        # 2026 for not working 80 hours a month, with no question left.
        _, eligible_2026, _, _, missing, _ = is_eligible(
            **self._ltc_answers(age=10, on_medicare=False, children_in_household=1)
        )
        self.assertTrue(eligible_2026)
        self.assertEqual(missing, [])

    def test_pregnant_ltc_applicant_keeps_2026_exemption(self):
        _, eligible_2026, _, _, _, _ = is_eligible(
            **self._ltc_answers(age=30, pregnant=True, on_medicare=False)
        )
        self.assertTrue(eligible_2026)

    def test_living_situation_does_not_block_the_determination(self):
        # It gated the whole LTC verdict but nothing ever read it, costing
        # every LTC applicant a round trip for an inert answer.
        answers = self._ltc_answers()
        answers.pop("living_situation", None)
        eligible_2025, _, _, _, missing, _ = is_eligible(**answers)
        self.assertTrue(eligible_2025)
        self.assertFalse(any("living" in q.lower() for q in missing))

    def test_elderly_ltc_applicant_uses_ltc_income_cap_not_abd(self):
        # Regression: 65+ applicants matched the ABD branch first and never
        # reached the LTC rules. $2,500/month fails the ABD 100%-FPL test but
        # passes the ~$3,000 LTC cap.
        eligible_2025, _, _, _, _, _ = is_eligible(
            **self._ltc_answers(state="wy", monthly_income=2500.0)
        )
        self.assertTrue(eligible_2025)

    def test_income_over_ltc_cap_suggests_miller_trust(self):
        _, _, _, alts, _, _ = is_eligible(
            **self._ltc_answers(monthly_income=3500.0)
        )
        self.assertTrue(any("Miller trust" in a for a in alts))


class TestAbdPathway(SimpleTestCase):
    """Aged/blind/disabled income and asset rules."""

    def test_medically_needy_state_does_not_waive_the_income_test(self):
        # Review finding: medically-needy is a spend-down PATHWAY, not an
        # income waiver -- treating it as one reported a $120k/yr earner in
        # New York as "probably eligible" in both years.
        eligible_2025, eligible_2026, _, _, _, _ = is_eligible(
            **_answers(
                state="ny",
                age=70,
                monthly_income=10000.0,
                on_medicare=True,
                assets_total=1500.0,
            )
        )
        self.assertFalse(eligible_2025)
        self.assertFalse(eligible_2026)

    def test_medically_needy_state_still_suggests_spend_down(self):
        _, _, _, alts, _, _ = is_eligible(
            **_answers(
                state="ny",
                age=70,
                monthly_income=10000.0,
                on_medicare=True,
                assets_total=1500.0,
            )
        )
        self.assertTrue(any("spend-down" in a for a in alts))


class TestGetMedicaidInfo(SimpleTestCase):
    """State info lookups keep working with the shared state map."""

    def test_lookup_by_abbreviation(self):
        result = get_medicaid_info({"state": "CA", "topic": "", "limit": 5})
        self.assertIn("California", result)

    def test_lookup_dc_alias_resolves_to_district_of_columbia_data(self):
        # "washington, dc" -> "dc" -> "District of Columbia" display name,
        # which is what the CSV rows are keyed on.
        result = get_medicaid_info({"state": "washington, dc", "topic": "", "limit": 5})
        self.assertIn("Health Care Finance", result)

    def test_garbled_state_returns_none_instead_of_raising(self):
        # Review regression: is_eligible got the garbled-state hardening but
        # this sibling entry point still raised ValueError, killing the whole
        # chat tool call. None (not prose) so the caller can tell this apart
        # from real data -- it wraps any string in "Here's the official
        # Medicaid information for X".
        self.assertIsNone(get_medicaid_info({"state": "medi-cal er california"}))

    def test_placeholder_state_returns_none(self):
        # Review regression: the "unknown"/"StateName" placeholders the model
        # copies out of the prompt skipped the CSV filter and returned an
        # 11k-character all-states dump.
        self.assertIsNone(get_medicaid_info({"state": "unknown"}))

    def test_absent_state_returns_none(self):
        # Review regression: an absent key became "", and _normalize_state("")
        # raises, so this landed in the garbled-state arm and told the model
        # to "confirm" a state the user never gave.
        self.assertIsNone(get_medicaid_info({}))

    def test_non_numeric_limit_does_not_raise(self):
        # Review regression: int(query["limit"]) blew up the whole tool call
        # on an LLM payload like {"limit": "five"}.
        self.assertIn("California", get_medicaid_info({"state": "ca", "limit": "five"}))

    def test_no_dangling_misc_header(self):
        # The MISC section looped over columns already projected away, so it
        # emitted a header promising data it structurally could not deliver.
        self.assertNotIn("MISC", get_medicaid_info({"state": "ca"}))

    def test_territory_with_no_csv_row_returns_none(self):
        # PR/GU/VI/AS/MP normalize to a display name but have no row in
        # medicaid_resources.csv. Returning "No Medicaid data found for X."
        # here handed the caller prose it wraps as "Here's the official
        # Medicaid information for Puerto Rico:" -- presenting a miss as data.
        self.assertIsNone(get_medicaid_info({"state": "Puerto Rico"}))


class TestDeclinedAnswersDoNotBecomeVerdicts(SimpleTestCase):
    """A question the user declined leaves the category unscored.

    ``ask()`` silently skips declined fields, so every caller that would
    otherwise fall through to a verdict has to notice the difference between
    "the answer is no" and "we never got to run this test".
    """

    def setUp(self):
        # Three scenarios, each shared by the tests that split its
        # assertions. Age 66 keeps the assets case on the ABD pathway rather
        # than the MAGI expansion one, which applies no asset test.
        self.declined_assets = is_eligible(
            **_answers(
                age=66,
                monthly_income=900,
                on_medicare=False,
                years_worked=40,
                assets_total="unknown",
            )
        )
        self.declined_work_hours = is_eligible(
            **_answers(
                avg_monthly_qualifying_hours_last_3mo="unknown",
                total_qualifying_hours_last_3mo="unknown",
            )
        )
        self.declined_years_worked = is_eligible(
            **_answers(
                age=67,
                on_medicare=False,
                years_worked="unknown",
                assets_total=500,
            )
        )

    def test_declined_assets_leaves_abd_pathway_unscored(self):
        *_, missing, determination_made = self.declined_assets
        self.assertFalse(determination_made)
        self.assertEqual(missing, [])

    def test_declined_assets_still_offers_spend_down_in_a_medically_needy_state(self):
        *_, alts, _, _ = self.declined_assets
        self.assertIn(
            "Medically-needy/spend-down Medicaid may help if medical bills are high.",
            alts,
        )

    def test_declined_work_hours_leaves_2026_unscored(self):
        *_, missing, determination_made = self.declined_work_hours
        self.assertFalse(determination_made)
        self.assertEqual(missing, [])

    def test_declined_work_hours_keeps_the_settled_2025_verdict(self):
        eligible_2025, *_ = self.declined_work_hours
        self.assertTrue(eligible_2025)

    def test_declined_years_worked_still_offers_the_part_a_buy_in(self):
        *_, alts, _, _ = self.declined_years_worked
        self.assertIn(
            "You may be eligible to buy Medicare Part A even if you don't "
            "qualify for premium-free Part A.",
            alts,
        )

    def test_declined_years_worked_leaves_medicare_unscored(self):
        *_, determination_made = self.declined_years_worked
        self.assertFalse(determination_made)

    def test_indeterminate_result_names_the_field_it_could_not_check(self):
        *_, alts, _, _ = self.declined_assets
        self.assertTrue(
            any("could not check assets total" in a for a in alts),
            f"no disclosure of the unchecked field in {alts}",
        )


class TestTerritoryShortCircuit(SimpleTestCase):
    """Territories are not modeled, so don't interrogate them about Medicaid."""

    def test_medicaid_only_questions_are_dropped(self):
        *_, missing, _ = is_eligible(state="pr")
        for question in missing:
            self.assertNotIn("household", question.lower())
            self.assertNotIn("income", question.lower())

    def test_medicare_relevant_questions_survive(self):
        *_, missing, _ = is_eligible(state="pr")
        self.assertIn("How old are you?", missing)

    def test_territory_medicare_verdict_is_still_produced(self):
        _, _, medicare, _, missing, determination_made = is_eligible(
            **_answers(state="pr", age=67, on_medicare=False, years_worked=40)
        )
        self.assertTrue(medicare)
        self.assertEqual(missing, [])
        self.assertFalse(determination_made)


class TestAmbiguousStateInput(SimpleTestCase):
    """Program names resolve exactly or not at all -- never fuzzily."""

    def test_misspelled_medicaid_does_not_resolve_to_california(self):
        *_, missing, _ = is_eligible(state="medicad")
        self.assertIn("What state do you live in?", missing)

    def test_exact_program_name_still_resolves(self):
        eligible_2025, *_ = is_eligible(**_answers(state="medi-cal"))
        self.assertTrue(eligible_2025)

    def test_a_program_name_shared_by_two_states_re_asks(self):
        *_, missing, _ = is_eligible(state="healthy connections")
        self.assertIn("What state do you live in?", missing)

    def test_a_genuine_state_typo_still_fuzzy_matches(self):
        eligible_2025, *_ = is_eligible(**_answers(state="californa"))
        self.assertTrue(eligible_2025)


class TestAnswerVocabulary(SimpleTestCase):
    """Natural phrasings of common answers must not read as unanswered."""

    def test_no_income_is_recorded_as_zero(self):
        summary = summarize_eligibility_inputs({"monthly_income": "no income"})
        self.assertEqual(summary["recorded"]["monthly_income"], 0.0)

    def test_no_income_does_not_re_ask_for_income(self):
        *_, missing, _ = is_eligible(**_answers(monthly_income="no income"))
        for question in missing:
            self.assertNotIn("monthly income", question.lower())

    def test_unemployed_is_not_treated_as_zero_income(self):
        # A job status is not an amount, and unemployment benefits are income.
        summary = summarize_eligibility_inputs({"monthly_income": "unemployed"})
        self.assertIn("monthly_income", summary["unreadable"])

    def test_widowed_answers_the_married_question(self):
        summary = summarize_eligibility_inputs({"married": "widowed"})
        self.assertIs(summary["recorded"]["married"], False)


class TestSpendDownSuggestion(SimpleTestCase):
    """Only suggest medically-needy Medicaid where the program exists."""

    def test_not_suggested_in_a_state_without_the_program(self):
        *_, alts, _, _ = is_eligible(
            **_answers(
                state="tx", age=70, monthly_income=3000, on_medicare=True,
                assets_total=50000, years_worked=40,
            )
        )
        for alt in alts:
            self.assertNotIn("medically-needy", alt.lower())

    def test_suggested_in_a_state_with_the_program(self):
        *_, alts, _, _ = is_eligible(
            **_answers(
                state="ca", age=70, monthly_income=3000, on_medicare=True,
                assets_total=50000, years_worked=40,
            )
        )
        self.assertTrue(
            any("medically-needy" in a.lower() for a in alts),
            f"expected a spend-down pointer in {alts}",
        )


class TestDisabledButNotOnSsdi(SimpleTestCase):
    """`receiving_ssdi` is ORed with `disabled`; the SSDI question is not.

    Regression: the "don't rule Medicare out yet" branch waited on
    ``receiving_ssdi``, which is true when only ``disabled`` was supplied,
    but the ssdi_length question it waits for is gated on the SSDI answer
    alone. Someone who said "disabled, not on SSDI" therefore waited on a
    question that never got asked.
    """

    def setUp(self):
        self.result = is_eligible(
            **_answers(
                age=66,
                receiving_ssdi=False,
                disabled=True,
                on_medicare=False,
                years_worked=5,
                assets_total=500,
            )
        )

    def test_no_silent_medicare_denial(self):
        *_, alts, missing, _ = self.result
        self.assertTrue(
            missing or alts,
            "a 66-year-old got no Medicare verdict, no question, and no next step",
        )

    def test_part_a_buy_in_is_offered(self):
        *_, alts, _, _ = self.result
        self.assertTrue(
            any("buy Medicare Part A" in a for a in alts),
            f"expected the Part-A buy-in pointer in {alts}",
        )

    def test_actual_ssdi_recipient_still_defers(self):
        # The deferral is still correct when they really are on SSDI: the
        # ssdi_length question is asked, so don't conclude anything yet.
        *_, missing, _ = is_eligible(
            **_answers(
                age=66, receiving_ssdi=True, on_medicare=False,
                years_worked=5, assets_total=500,
            )
        )
        self.assertIn("How many months have you been receiving SSDI?", missing)


class TestBareMedicalIsAmbiguous(SimpleTestCase):
    """Pennsylvania, Minnesota and Maryland all call their program "Medical Assistance"."""

    def test_bare_medical_re_asks_instead_of_guessing_california(self):
        *_, missing, _ = is_eligible(state="medical", age=30)
        self.assertTrue(any("state" in q.lower() for q in missing))

    def test_hyphenated_medi_cal_still_resolves(self):
        eligible_2025, *_ = is_eligible(**_answers(state="medi-cal"))
        self.assertTrue(eligible_2025)


class TestAssetTestIsSkippedWhenIrrelevant(SimpleTestCase):
    """ACA expansion applies no asset test, so a missing asset figure can't matter.

    Regression: marking a declined ``assets_total`` indeterminate was applied
    before checking the expansion pathway, so a disabled 19-64 adult in an
    expansion state under 138% FPL got "we could not produce an estimate" --
    while the same person reporting assets far OVER the ABD limit came back
    eligible through that very pathway. Declining was worse than answering
    badly.
    """

    def _disabled_expansion_adult(self, **overrides):
        return is_eligible(
            **_answers(
                age=40,
                receiving_ssdi=True,
                ssdi_length=6,
                on_medicare=False,
                avg_monthly_qualifying_hours_last_3mo=100,
                **overrides,
            )
        )

    def test_declined_assets_still_scores_via_expansion(self):
        eligible_2025, _, _, _, _, determination_made = self._disabled_expansion_adult(
            assets_total="unknown"
        )
        self.assertTrue(eligible_2025)
        self.assertTrue(determination_made)

    def test_declining_matches_answering_when_assets_are_irrelevant(self):
        declined, *_ = self._disabled_expansion_adult(assets_total="unknown")
        over_limit, *_ = self._disabled_expansion_adult(assets_total=99999)
        self.assertEqual(declined, over_limit)

    def test_the_asset_question_is_not_even_asked(self):
        *_, missing, _ = self._disabled_expansion_adult()
        for question in missing:
            self.assertNotIn("assets", question.lower())

    def test_non_expansion_state_still_needs_the_asset_answer(self):
        # Texas has no expansion pathway, so the asset test genuinely applies.
        *_, missing, _ = is_eligible(
            **_answers(
                state="tx", age=40, receiving_ssdi=True, ssdi_length=6,
                on_medicare=False, avg_monthly_qualifying_hours_last_3mo=100,
            )
        )
        self.assertTrue(any("assets" in q.lower() for q in missing))

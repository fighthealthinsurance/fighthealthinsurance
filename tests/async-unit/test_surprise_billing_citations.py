"""Tests for the emergency and surprise-billing citations: which denials
trigger them, which plans they reach, and how they join the regulatory block
the appeal prompt already carries."""

import unittest
from types import SimpleNamespace

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.regulatory_citations import (
    AIR_AMBULANCE,
    EMERGENCY_CARE,
    OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY,
    SURPRISE_BILLING_HEADER,
    SURPRISE_BILLING_RULES,
    applicable_surprise_billing_rules,
    get_surprise_billing_citation_context,
    surprise_billing_facts,
)


class TestSurpriseBillingFacts(unittest.TestCase):
    def test_emergency_room_wording_is_emergency_care(self):
        facts = surprise_billing_facts(
            denial_text="Your emergency room visit on 3/2 was denied."
        )
        self.assertEqual(facts, {EMERGENCY_CARE})

    def test_emergency_department_code_is_emergency_care(self):
        self.assertEqual(surprise_billing_facts(codes=["99284"]), {EMERGENCY_CARE})

    def test_non_emergency_wording_is_not_emergency_care(self):
        facts = surprise_billing_facts(
            denial_text=(
                "This was a non-emergency transport and not an emergency "
                "service under the plan."
            )
        )
        self.assertNotIn(EMERGENCY_CARE, facts)

    def test_air_ambulance_wording_is_air_ambulance(self):
        facts = surprise_billing_facts(procedure="Air ambulance transport")
        self.assertIn(AIR_AMBULANCE, facts)

    def test_air_ambulance_code_is_air_ambulance(self):
        self.assertEqual(surprise_billing_facts(codes=["a0431"]), {AIR_AMBULANCE})

    def test_ground_ambulance_is_not_air_ambulance(self):
        facts = surprise_billing_facts(
            denial_text="Ground ambulance transport", codes=["A0427"]
        )
        self.assertNotIn(AIR_AMBULANCE, facts)

    def test_out_of_network_clinician_at_in_network_hospital(self):
        facts = surprise_billing_facts(
            denial_text=(
                "Your claim from the anesthesiologist was denied because the "
                "anesthesiologist was out-of-network, although the surgery was "
                "at an in-network hospital."
            )
        )
        self.assertEqual(facts, {OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY})

    def test_out_of_network_alone_is_not_the_facility_case(self):
        facts = surprise_billing_facts(
            denial_text="The provider is out-of-network for your plan."
        )
        self.assertNotIn(OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY, facts)

    def test_policy_boilerplate_on_another_denial_is_not_emergency_care(self):
        facts = surprise_billing_facts(
            denial_text=(
                "Your MRI is not medically necessary. Emergency services do not "
                "require prior authorization."
            ),
            procedure="MRI lumbar spine",
        )
        self.assertNotIn(EMERGENCY_CARE, facts)

    def test_negated_mentions_do_not_count(self):
        for text in (
            "No ER visit occurred on your claim.",
            "No air ambulance was used for your claim.",
            "Your claim was never an emergency room visit.",
        ):
            with self.subTest(text=text):
                self.assertEqual(surprise_billing_facts(denial_text=text), frozenset())

    def test_non_emergency_air_ambulance_is_still_air_ambulance(self):
        facts = surprise_billing_facts(procedure="Non-emergency air ambulance transport")
        self.assertIn(AIR_AMBULANCE, facts)
        self.assertNotIn(EMERGENCY_CARE, facts)

    def test_emergency_service_as_the_procedure_counts(self):
        self.assertEqual(
            surprise_billing_facts(procedure="Emergency department visit"),
            {EMERGENCY_CARE},
        )

    def test_prior_authorization_wording_on_an_er_claim_still_counts(self):
        facts = surprise_billing_facts(
            denial_text=(
                "Your emergency room visit on 3/2 was denied because prior "
                "authorization was required and not obtained."
            )
        )
        self.assertIn(EMERGENCY_CARE, facts)

    def test_terms_broken_across_lines_still_match(self):
        self.assertIn(
            EMERGENCY_CARE,
            surprise_billing_facts(denial_text="Your emergency\nroom visit was denied."),
        )
        self.assertIn(
            AIR_AMBULANCE,
            surprise_billing_facts(denial_text="Your claim for the air\nambulance was denied."),
        )

    def test_non_participating_hospital_alone_is_not_the_facility_case(self):
        facts = surprise_billing_facts(
            denial_text="Care at a non-participating hospital is not covered."
        )
        self.assertNotIn(OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY, facts)

    def test_out_of_network_boilerplate_on_a_drug_denial_does_not_fire(self):
        facts = surprise_billing_facts(
            denial_text=(
                "Your prescription for the drug is denied. Out-of-network "
                "benefits are not available under your plan. To find a "
                "participating hospital, visit our website."
            ),
            procedure="Adalimumab",
        )
        self.assertNotIn(OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY, facts)

    def test_unrelated_denial_has_no_facts(self):
        facts = surprise_billing_facts(
            denial_text="Physical therapy beyond 20 visits is not medically necessary.",
            procedure="Physical therapy",
            codes=["97110"],
        )
        self.assertEqual(facts, frozenset())


class TestApplicableSurpriseBillingRules(unittest.TestCase):
    def names(self, rules):
        return " ".join(r.name for r in rules)

    def test_private_emergency_gets_the_no_surprises_act_rule(self):
        names = self.names(applicable_surprise_billing_rules({EMERGENCY_CARE}))
        self.assertIn("149.110", names)
        self.assertNotIn("422.113", names)
        self.assertNotIn("438.114", names)

    def test_self_funded_erisa_plan_still_gets_it(self):
        names = self.names(
            applicable_surprise_billing_rules({EMERGENCY_CARE}, programs=("erisa",))
        )
        self.assertIn("149.110", names)

    def test_medicare_advantage_gets_its_own_emergency_rule_only(self):
        names = self.names(
            applicable_surprise_billing_rules(
                {EMERGENCY_CARE, AIR_AMBULANCE}, programs=("medicare_advantage",)
            )
        )
        self.assertIn("422.113", names)
        self.assertNotIn("149.110", names)
        self.assertNotIn("149.130", names)

    def test_medicaid_gets_its_own_emergency_rule_only(self):
        names = self.names(
            applicable_surprise_billing_rules({EMERGENCY_CARE}, programs=("medicaid",))
        )
        self.assertIn("438.114", names)
        self.assertNotIn("149.110", names)

    def test_original_medicare_and_va_get_nothing(self):
        for program in ("medicare", "va"):
            with self.subTest(program=program):
                self.assertEqual(
                    applicable_surprise_billing_rules(
                        {EMERGENCY_CARE, AIR_AMBULANCE}, programs=(program,)
                    ),
                    [],
                )

    def test_fehb_gets_the_no_surprises_act_rules(self):
        names = self.names(
            applicable_surprise_billing_rules({AIR_AMBULANCE}, programs=("fehb",))
        )
        self.assertIn("149.130", names)

    def test_public_and_private_coverage_together_keeps_both(self):
        names = self.names(
            applicable_surprise_billing_rules(
                {EMERGENCY_CARE}, programs=("medicare_advantage", "erisa")
            )
        )
        self.assertIn("149.110", names)
        self.assertIn("422.113", names)

    def test_no_rule_cites_the_pre_2022_emergency_section(self):
        for rule in SURPRISE_BILLING_RULES:
            with self.subTest(rule=rule.name):
                self.assertNotIn("147.138", rule.name)
                self.assertNotIn("147.138", rule.summary)


class TestLetterTextAndCitingLimits(unittest.TestCase):
    """The summary is what a letter may use, so it never offers the plan a
    way out; the limits live in the model-only "Cite only if" line."""

    def rule(self, section):
        return next(r for r in SURPRISE_BILLING_RULES if section in r.name)

    def test_no_summary_offers_an_exit(self):
        for rule in SURPRISE_BILLING_RULES:
            with self.subTest(rule=rule.name):
                for phrase in (
                    "excludes",
                    "does not make a plan cover",
                    "medically necessary",
                    "When a plan covers",
                    "A plan that covers",
                ):
                    self.assertNotIn(phrase, rule.summary)

    def test_no_surprises_act_summaries_say_no_out_of_network_benefits_is_not_an_exclusion(
        self,
    ):
        for section in ("149.110", "149.120", "149.130"):
            with self.subTest(section=section):
                self.assertIn(
                    "lack of out-of-network benefits is not an exclusion",
                    self.rule(section).summary,
                )

    def test_benefit_conditions_are_in_the_citing_limits(self):
        self.assertIn(
            "covers any emergency department services", self.rule("149.110").cite_when
        )
        self.assertIn("covers the service itself", self.rule("149.120").cite_when)
        self.assertIn("covers air ambulance services at all", self.rule("149.130").cite_when)
        self.assertIn("ground ambulances", self.rule("149.130").cite_when)

    def test_emergency_limits_name_the_terms_the_rule_keeps(self):
        cite_when = self.rule("149.110").cite_when
        for phrase in ("coordination of benefits", "waiting period", "cost sharing"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, cite_when)
        self.assertIn("defined term", cite_when)

    def test_cost_sharing_is_a_ceiling_except_for_air_ambulance(self):
        self.assertIn("no higher than in network", self.rule("149.110").summary)
        self.assertIn("no higher than in network", self.rule("149.120").summary)
        self.assertIn("the same cost sharing", self.rule("149.130").summary)

    def test_facility_rule_names_the_participating_facility_types(self):
        summary = self.rule("149.120").summary
        for phrase in (
            "hospital outpatient department",
            "critical access hospital",
            "ambulatory surgical center",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, summary)

    def test_149_420_appears_only_as_the_consent_criteria(self):
        rule = self.rule("149.120")
        self.assertNotIn("149.420", rule.name)
        self.assertIn("notice-and-consent criteria in 45 C.F.R. § 149.420(c)", rule.summary)
        self.assertIn("never as a rule the plan broke", rule.cite_when)
        for other in SURPRISE_BILLING_RULES:
            if other is not rule:
                self.assertNotIn("149.420", other.summary + other.name)

    def test_consent_exceptions_cover_the_regulation_list(self):
        summary = self.rule("149.120").summary
        for phrase in (
            "emergency medicine",
            "anesthesiology",
            "pathology",
            "radiology",
            "neonatology",
            "assistant surgeons",
            "hospitalists",
            "intensivists",
            "laboratory",
            "no participating provider",
            "unforeseen, urgent",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, summary)
        self.assertNotIn("signed", summary)

    def test_emergency_rule_gives_the_full_prudent_layperson_standard(self):
        summary = self.rule("149.110").summary
        for phrase in (
            "average knowledge of health and medicine",
            "serious jeopardy",
            "seriously impair bodily functions",
            "serious dysfunction of a bodily organ or part",
            "unborn child",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, summary)

    def test_medicare_advantage_rule_leaves_out_urgently_needed_services(self):
        self.assertNotIn("urgently needed", self.rule("422.113").summary)

    def test_reach_names_plans_the_act_does_not_reach(self):
        reaches = self.rule("149.110").reaches
        self.assertIn("not short-term limited-duration insurance", reaches)
        self.assertIn("excepted benefits", reaches)
        self.assertIn("HRAs", reaches)
        self.assertNotIn("TRICARE", reaches)


class TestEmployerPlanSections(unittest.TestCase):
    PARALLELS = (
        "29 C.F.R. § 2590.716-4",
        "26 U.S.C. § 9816(a)",
    )

    def test_erisa_tpa_and_unknown_plans_get_the_labor_and_tax_code_sections(self):
        for programs in (("erisa",), ("tpa",), ("erisa_letter",), ()):
            with self.subTest(programs=programs):
                block = get_surprise_billing_citation_context(
                    codes=["99285"], programs=programs
                )
                assert block is not None
                for section in self.PARALLELS:
                    self.assertIn(section, block)

    def test_every_no_surprises_act_rule_carries_its_parallels(self):
        block = get_surprise_billing_citation_context(
            denial_text=(
                "Your claim for the air ambulance was denied. Your claim from the "
                "out-of-network anesthesiologist at the in-network hospital was "
                "denied."
            ),
            codes=["99285"],
            programs=("erisa",),
        )
        assert block is not None
        for section in (
            "2590.716-4",
            "26 U.S.C. § 9816(a)",
            "2590.716-5",
            "26 U.S.C. § 9816(b)",
            "2590.717-1",
            "26 U.S.C. § 9817",
        ):
            with self.subTest(section=section):
                self.assertIn(section, block)

    def test_no_treasury_temporary_regulation_is_cited(self):
        for rule in SURPRISE_BILLING_RULES:
            with self.subTest(rule=rule.name):
                self.assertNotIn("54.981", rule.employer_plan_sections)
                self.assertNotIn("26 C.F.R.", rule.employer_plan_sections)

    def test_marketplace_plan_gets_only_the_hhs_section(self):
        block = get_surprise_billing_citation_context(
            codes=["99285"], programs=("marketplace",)
        )
        assert block is not None
        self.assertIn("149.110", block)
        self.assertNotIn("2590.716-4", block)


class TestSurpriseBillingContext(unittest.TestCase):
    def test_air_ambulance_block_keeps_the_limits_out_of_the_letter_text(self):
        block = get_surprise_billing_citation_context(codes=["A0430"])
        assert block is not None
        self.assertTrue(block.startswith(SURPRISE_BILLING_HEADER))
        self.assertIn("149.130", block)
        self.assertIn("Cite only if the plan covers air ambulance services", block)
        self.assertIn("is for you, not for the letter", block)
        self.assertNotIn("medically necessary", block)

    def test_medicaid_with_a_tpa_flag_gets_only_the_medicaid_rule(self):
        block = get_surprise_billing_citation_context(
            codes=["99285"], programs=("medicaid", "tpa")
        )
        assert block is not None
        self.assertIn("438.114", block)
        self.assertNotIn("149.110", block)
        self.assertNotIn("No Surprises Act", block)

    def test_public_and_private_sources_render_the_also_private_caveat(self):
        block = get_surprise_billing_citation_context(
            codes=["99285"], programs=("medicaid", "erisa")
        )
        assert block is not None
        self.assertIn("149.110", block)
        self.assertIn("438.114", block)
        self.assertIn("if you also have private coverage", block)

    def test_fehb_alone_gets_no_also_private_caveat(self):
        block = get_surprise_billing_citation_context(
            codes=["99285"], programs=("fehb",)
        )
        assert block is not None
        self.assertIn("149.110", block)
        self.assertNotIn("if you also have private coverage", block)
        self.assertNotIn("the plan type is not known", block)

    def test_no_block_for_an_unrelated_denial(self):
        self.assertIsNone(
            get_surprise_billing_citation_context(
                denial_text="Knee MRI is not medically necessary."
            )
        )

    def test_no_block_when_no_rule_reaches_the_plan(self):
        self.assertIsNone(
            get_surprise_billing_citation_context(
                denial_text="Emergency room visit denied.", programs=("va",)
            )
        )

    def test_unknown_plan_gets_the_plan_limits_and_a_caveat(self):
        block = get_surprise_billing_citation_context(codes=["99285"])
        assert block is not None
        self.assertIn("reaches employer plans", block)
        self.assertIn("the plan type is not known", block)

    def test_known_private_plan_gets_no_unknown_plan_caveat(self):
        block = get_surprise_billing_citation_context(
            codes=["99285"], programs=("erisa",)
        )
        assert block is not None
        self.assertNotIn("the plan type is not known", block)

    def test_known_original_medicare_or_va_gets_no_emergency_rule(self):
        for program in ("medicare", "va"):
            with self.subTest(program=program):
                self.assertIsNone(
                    get_surprise_billing_citation_context(
                        codes=["99285"], programs=(program,)
                    )
                )

    def test_the_source_url_is_not_rendered(self):
        block = get_surprise_billing_citation_context(codes=["99285"])
        assert block is not None
        self.assertNotIn("ecfr.gov", block)


class TestCollectorWiring(unittest.TestCase):
    def denial(self, **fields):
        base = dict(
            your_state=None,
            denial_text=None,
            procedure=None,
            diagnosis=None,
        )
        base.update(fields)
        return SimpleNamespace(**base)

    def test_stateless_emergency_denial_now_gets_a_block(self):
        result = AppealGenerator._collect_regulatory_context(
            self.denial(denial_text="Claim for emergency department services denied.")
        )
        assert result is not None
        self.assertIn(SURPRISE_BILLING_HEADER, result)
        self.assertIn("149.110", result)

    def test_code_in_the_procedure_field_triggers_it(self):
        result = AppealGenerator._collect_regulatory_context(
            self.denial(procedure="A0431 rotary wing transport")
        )
        assert result is not None
        self.assertIn("149.130", result)

    def test_state_block_and_surprise_block_are_joined(self):
        result = AppealGenerator._collect_regulatory_context(
            self.denial(your_state="MA", denial_text="Emergency room visit denied.")
        )
        assert result is not None
        self.assertIn("Massachusetts", result)
        self.assertIn(SURPRISE_BILLING_HEADER, result)
        self.assertLess(result.index("Massachusetts"), result.index(SURPRISE_BILLING_HEADER))

    def test_lowercase_air_ambulance_code_triggers_it(self):
        result = AppealGenerator._collect_regulatory_context(
            self.denial(procedure="a0431")
        )
        assert result is not None
        self.assertIn("149.130", result)

    def test_unrelated_stateless_denial_still_gets_nothing(self):
        self.assertIsNone(
            AppealGenerator._collect_regulatory_context(
                self.denial(denial_text="Knee MRI is not medically necessary.")
            )
        )

    def test_a_failing_surprise_lookup_leaves_the_state_block(self):
        from unittest import mock

        with mock.patch(
            "fighthealthinsurance.regulatory_citations."
            "get_surprise_billing_citation_context",
            side_effect=RuntimeError("boom"),
        ):
            result = AppealGenerator._collect_regulatory_context(
                self.denial(your_state="MA", denial_text="Emergency room visit denied.")
            )
        assert result is not None
        self.assertIn("Massachusetts", result)
        self.assertNotIn(SURPRISE_BILLING_HEADER, result)

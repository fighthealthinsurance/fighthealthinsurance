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
                "The anesthesiologist was out-of-network although the surgery "
                "was at an in-network hospital."
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


class TestSummariesKeepTheBenefitLimits(unittest.TestCase):
    def rule(self, section):
        return next(r for r in SURPRISE_BILLING_RULES if section in r.name)

    def test_emergency_rule_is_conditioned_on_emergency_benefits(self):
        self.assertIn(
            "A plan that covers any emergency department services",
            self.rule("149.110").summary,
        )

    def test_facility_rule_is_conditioned_on_a_covered_service(self):
        summary = self.rule("149.120").summary
        self.assertIn("When a plan covers a service", summary)
        self.assertIn("does not make a plan cover a service it excludes", summary)

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


class TestSurpriseBillingContext(unittest.TestCase):
    def test_air_ambulance_block_says_what_the_rule_does_not_do(self):
        block = get_surprise_billing_citation_context(codes=["A0430"])
        assert block is not None
        self.assertTrue(block.startswith(SURPRISE_BILLING_HEADER))
        self.assertIn("149.130", block)
        self.assertIn("does not reach ground ambulances", block)
        self.assertIn("not medically necessary", block)

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

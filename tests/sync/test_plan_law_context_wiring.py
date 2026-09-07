"""The APPLICABLE APPEAL LAW collector against real rows: the plan-source
many-to-many (through PlanSourceRelation) and the carrier's TPA flag."""

from django.test import TestCase

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    PlanSource,
    PlanSourceRelation,
)


class PlanLawContextWiringTest(TestCase):
    def _denial(self, **fields):
        return Denial.objects.create(
            denial_text="Your claim was denied.", hashed_email="law@example.com", **fields
        )

    def test_the_intake_plan_source_reaches_the_block(self):
        denial = self._denial()
        source = PlanSource.objects.create(name="State Marketplace / Affordable Care Act")
        PlanSourceRelation.objects.create(denial=denial, plan_source=source)
        block = AppealGenerator._collect_plan_law_context(denial)
        self.assertIsNotNone(block)
        self.assertIn("marketplace (Affordable Care Act) plan", block)
        self.assertIn("45 C.F.R. § 147.136", block)

    def test_a_tpa_carrier_reaches_the_block(self):
        company = InsuranceCompany.objects.create(
            name="Meritain Health", regex=r"meritain", is_tpa=True
        )
        denial = self._denial(insurance_company="Meritain", insurance_company_obj=company)
        block = AppealGenerator._collect_plan_law_context(denial)
        self.assertIsNotNone(block)
        self.assertIn("ERISA governs the appeal", block)

    def test_a_denial_with_nothing_known_gets_the_hedged_block(self):
        block = AppealGenerator._collect_plan_law_context(self._denial())
        self.assertIsNotNone(block)
        self.assertIn("We do not know how the patient gets this coverage", block)

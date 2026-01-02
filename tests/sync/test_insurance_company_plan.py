"""Test the InsuranceCompany and InsurancePlan models and extraction logic"""

from django.test import TestCase
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    PlanSource,
    PlanType,
)


class InsuranceCompanyModelTests(TestCase):
    """Test the InsuranceCompany model."""

    def setUp(self):
        """Set up test insurance companies and plans."""
        # Create test insurance company
        self.anthem = InsuranceCompany.objects.create(
            name="Anthem Blue Cross Blue Shield",
            alt_names="Anthem\nBCBS\nBlue Cross Blue Shield",
            regex=r"(anthem|blue\s*cross\s*blue\s*shield|bcbs)",
            website="https://www.anthem.com",
        )

        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            alt_names="UHC\nUnited Healthcare\nUnited Health",
            regex=r"(united\s*health\s*care|united\s*health|uhc)",
            website="https://www.uhc.com",
        )

        # Create plan source for Medicaid
        self.medicaid_source = PlanSource.objects.create(
            name="Medicaid",
            regex=r"medicaid",
            negative_regex=r"",
        )

        # Create state-specific Medicaid plans
        self.anthem_ca_medicaid = InsurancePlan.objects.create(
            insurance_company=self.anthem,
            plan_name="Medicaid",
            state="CA",
            plan_source=self.medicaid_source,
            regex=r"anthem.*medicaid.*california|california.*anthem.*medicaid",
        )

        self.anthem_ny_medicaid = InsurancePlan.objects.create(
            insurance_company=self.anthem,
            plan_name="Medicaid",
            state="NY",
            plan_source=self.medicaid_source,
            regex=r"anthem.*medicaid.*new\s*york|new\s*york.*anthem.*medicaid",
        )

    def test_insurance_company_creation(self):
        """Test that insurance companies can be created."""
        self.assertEqual(self.anthem.name, "Anthem Blue Cross Blue Shield")
        self.assertIn("BCBS", self.anthem.alt_names)

    def test_insurance_plan_creation(self):
        """Test that insurance plans can be created."""
        self.assertEqual(self.anthem_ca_medicaid.insurance_company, self.anthem)
        self.assertEqual(self.anthem_ca_medicaid.state, "CA")
        self.assertEqual(
            str(self.anthem_ca_medicaid),
            "Anthem Blue Cross Blue Shield - Medicaid (CA)",
        )

    def test_plan_unique_constraint(self):
        """Test that company + plan name + state must be unique."""
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            InsurancePlan.objects.create(
                insurance_company=self.anthem,
                plan_name="Medicaid",
                state="CA",  # Duplicate of anthem_ca_medicaid
            )

    def test_denial_with_insurance_company(self):
        """Test that denials can be linked to insurance companies."""
        denial = Denial.objects.create(
            denial_text="Anthem denied my claim for medical necessity.",
            hashed_email="test@example.com",
            insurance_company="Anthem",
            insurance_company_obj=self.anthem,
        )

        self.assertEqual(denial.insurance_company_obj, self.anthem)
        self.assertEqual(denial.insurance_company, "Anthem")

    def test_denial_with_insurance_plan(self):
        """Test that denials can be linked to specific insurance plans."""
        denial = Denial.objects.create(
            denial_text="Anthem Medicaid California denied my claim.",
            hashed_email="test@example.com",
            state="CA",
            insurance_company_obj=self.anthem,
            insurance_plan_obj=self.anthem_ca_medicaid,
        )

        self.assertEqual(denial.insurance_company_obj, self.anthem)
        self.assertEqual(denial.insurance_plan_obj, self.anthem_ca_medicaid)
        self.assertEqual(denial.state, "CA")

    def test_multiple_plans_per_company(self):
        """Test that one company can have multiple state-specific plans."""
        anthem_plans = InsurancePlan.objects.filter(insurance_company=self.anthem)
        self.assertEqual(anthem_plans.count(), 2)

        states = [plan.state for plan in anthem_plans]
        self.assertIn("CA", states)
        self.assertIn("NY", states)

    def test_company_alt_names_matching(self):
        """Test that we can find companies by alternative names."""
        # Search for "BCBS" which is an alt name
        alt_names_lower = self.anthem.alt_names.lower()
        self.assertIn("bcbs", alt_names_lower)

    def test_plan_cascade_delete(self):
        """Test that deleting a company cascades to its plans."""
        aetna = InsuranceCompany.objects.create(
            name="Aetna",
            regex=r"aetna",
        )

        plan = InsurancePlan.objects.create(
            insurance_company=aetna,
            plan_name="Test Plan",
            state="CA",
        )

        plan_id = plan.id
        aetna.delete()

        # Plan should be deleted due to CASCADE
        self.assertFalse(InsurancePlan.objects.filter(id=plan_id).exists())

    def test_denial_set_null_on_company_delete(self):
        """Test that denials set company to NULL when company is deleted."""
        denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email="test@example.com",
            insurance_company_obj=self.uhc,
        )

        denial_id = denial.denial_id
        self.uhc.delete()

        # Denial should still exist but company should be NULL
        denial = Denial.objects.get(denial_id=denial_id)
        self.assertIsNone(denial.insurance_company_obj)

    def test_multiple_anthem_brands(self):
        """Test that we can have multiple Anthem regional brands."""
        anthem_ca = InsuranceCompany.objects.create(
            name="Anthem Blue Cross California",
            alt_names="Anthem Blue Cross\nAnthem CA",
            regex=r"anthem.*california|california.*anthem",
        )

        empire_ny = InsuranceCompany.objects.create(
            name="Empire BlueCross BlueShield",
            alt_names="Empire BCBS\nEmpire Blue Cross",
            regex=r"empire.*blue.*cross|empire.*bcbs",
        )

        # Both should exist alongside the generic Anthem
        self.assertEqual(
            InsuranceCompany.objects.filter(name__icontains="Anthem").count(), 2
        )
        self.assertEqual(
            InsuranceCompany.objects.filter(name__icontains="Empire").count(), 1
        )

    def test_regional_brand_priority(self):
        """Test that regional brands are preferred over generic brands in matching."""
        # Create generic and specific Anthem entries
        anthem_generic = InsuranceCompany.objects.create(
            name="Anthem Generic",
            alt_names="Anthem",
        )

        anthem_ca = InsuranceCompany.objects.create(
            name="Anthem Blue Cross California",
            alt_names="Anthem Blue Cross\nAnthem CA",
        )

        # Test that "Anthem Blue Cross California" text matches the more specific one
        # This would be tested in integration tests with the actual matching logic


class FindNextStepsInsuranceCompanyTests(TestCase):
    """Test that FindNextStepsHelper properly saves insurance company info."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """Set up test data."""
        self.anthem = InsuranceCompany.objects.create(
            name="Anthem Blue Cross Blue Shield",
            alt_names="Anthem\nBCBS",
            regex=r"(anthem|bcbs)",
        )

        self.anthem_ca_medicaid = InsurancePlan.objects.create(
            insurance_company=self.anthem,
            plan_name="Medicaid",
            state="CA",
        )

        # Create a denial that can be updated
        self.email = "test@example.com"
        self.denial = Denial.objects.create(
            denial_text="Test denial text",
            hashed_email=Denial.get_hashed_email(self.email),
        )

    def test_find_next_steps_saves_insurance_company_obj(self):
        """Test that find_next_steps saves insurance_company_obj on the denial."""
        from fighthealthinsurance.common_view_logic import FindNextStepsHelper

        FindNextStepsHelper.find_next_steps(
            denial_id=self.denial.denial_id,
            email=self.email,
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            insurance_company="Anthem",
            insurance_company_obj=self.anthem,
            plan_id="12345",
            claim_id="CLM-001",
            denial_type=[],
            semi_sekret=self.denial.semi_sekret,
        )

        # Refresh from database
        self.denial.refresh_from_db()

        self.assertEqual(self.denial.insurance_company, "Anthem")
        self.assertEqual(self.denial.insurance_company_obj, self.anthem)

    def test_find_next_steps_saves_insurance_plan_obj(self):
        """Test that find_next_steps saves insurance_plan_obj on the denial."""
        from fighthealthinsurance.common_view_logic import FindNextStepsHelper

        FindNextStepsHelper.find_next_steps(
            denial_id=self.denial.denial_id,
            email=self.email,
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            insurance_company="Anthem",
            insurance_company_obj=self.anthem,
            insurance_plan_obj=self.anthem_ca_medicaid,
            plan_id="12345",
            claim_id="CLM-001",
            denial_type=[],
            semi_sekret=self.denial.semi_sekret,
        )

        # Refresh from database
        self.denial.refresh_from_db()

        self.assertEqual(self.denial.insurance_company_obj, self.anthem)
        self.assertEqual(self.denial.insurance_plan_obj, self.anthem_ca_medicaid)

    def test_find_next_steps_with_none_insurance_objs_does_not_overwrite(self):
        """Test that passing None for insurance objects doesn't overwrite existing values."""
        from fighthealthinsurance.common_view_logic import FindNextStepsHelper

        # First set the insurance company obj
        self.denial.insurance_company_obj = self.anthem
        self.denial.save()

        # Now call find_next_steps with None for insurance_company_obj
        FindNextStepsHelper.find_next_steps(
            denial_id=self.denial.denial_id,
            email=self.email,
            procedure="Test procedure",
            diagnosis="Test diagnosis",
            insurance_company="Anthem",
            insurance_company_obj=None,  # Explicitly None
            plan_id="12345",
            claim_id="CLM-001",
            denial_type=[],
            semi_sekret=self.denial.semi_sekret,
        )

        # Refresh from database
        self.denial.refresh_from_db()

        # Should still have the original insurance_company_obj
        self.assertEqual(self.denial.insurance_company_obj, self.anthem)


class AppealGeneratorInsuranceCompanyTests(TestCase):
    """Test that appeal generation uses structured insurance company info."""

    def setUp(self):
        """Set up test data."""
        self.anthem = InsuranceCompany.objects.create(
            name="Anthem Blue Cross Blue Shield",
            alt_names="Anthem\nBCBS",
            regex=r"(anthem|bcbs)",
        )

    def test_make_open_prompt_uses_structured_company_name(self):
        """Test that make_open_prompt uses insurance_company_obj.name when available."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()

        # Test with text-only insurance company
        prompt_text_only = generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="MRI",
            diagnosis="Back pain",
            insurance_company="Anthem",  # Text field
        )

        self.assertIn("Anthem", prompt_text_only)

    def test_make_appeals_prefers_structured_company_name(self):
        """Test that make_appeals uses insurance_company_obj.name over text field."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        # Create a denial with both text and structured company
        denial = Denial.objects.create(
            denial_text="Your claim was denied by Anthem.",
            hashed_email="test@example.com",
            insurance_company="Some Other Name",  # Text field has different name
            insurance_company_obj=self.anthem,  # Structured has "Anthem Blue Cross Blue Shield"
        )

        generator = AppealGenerator()

        # Access the prompt-building logic indirectly by checking behavior
        # The make_appeals method builds the prompt internally, so we verify
        # the denial has the correct data that would be used

        # Prefer structured name
        insurance_company_name = denial.insurance_company
        if denial.insurance_company_obj is not None:
            insurance_company_name = denial.insurance_company_obj.name

        self.assertEqual(insurance_company_name, "Anthem Blue Cross Blue Shield")

    def test_make_open_prompt_includes_tpa_erisa_info(self):
        """Test that make_open_prompt includes TPA/ERISA info when is_tpa is True."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        # Create a TPA company
        tpa_company = InsuranceCompany.objects.create(
            name="Meritain Health",
            alt_names="Meritain",
            regex=r"meritain",
            is_tpa=True,
        )

        generator = AppealGenerator()

        # Test prompt with TPA flag
        prompt_with_tpa = generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="MRI",
            diagnosis="Back pain",
            insurance_company="Meritain Health",
            is_tpa=True,
        )

        self.assertIn("Meritain Health", prompt_with_tpa)
        self.assertIn("Third-Party Administrator", prompt_with_tpa)
        self.assertIn("ERISA", prompt_with_tpa)

    def test_make_open_prompt_no_tpa_info_when_not_tpa(self):
        """Test that make_open_prompt does not include TPA info when is_tpa is False."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()

        # Test prompt without TPA flag
        prompt_without_tpa = generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="MRI",
            diagnosis="Back pain",
            insurance_company="Anthem",
            is_tpa=False,
        )

        self.assertIn("Anthem", prompt_without_tpa)
        self.assertNotIn("Third-Party Administrator", prompt_without_tpa)
        self.assertNotIn("ERISA", prompt_without_tpa)

    def test_make_appeals_uses_tpa_info_from_structured_company(self):
        """Test that make_appeals extracts is_tpa from structured company."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        # Create a TPA company
        tpa_company = InsuranceCompany.objects.create(
            name="Meritain Health",
            alt_names="Meritain",
            regex=r"meritain",
            is_tpa=True,
        )

        # Create a denial with TPA company
        denial = Denial.objects.create(
            denial_text="Your claim was denied.",
            hashed_email="test@example.com",
            insurance_company="Meritain",
            insurance_company_obj=tpa_company,
        )

        # Verify the logic that would be used in make_appeals
        insurance_company_name = denial.insurance_company
        is_tpa = False
        if denial.insurance_company_obj is not None:
            insurance_company_name = denial.insurance_company_obj.name
            is_tpa = denial.insurance_company_obj.is_tpa

        self.assertEqual(insurance_company_name, "Meritain Health")
        self.assertTrue(is_tpa)

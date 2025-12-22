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
        self.assertEqual(str(self.anthem_ca_medicaid), 
                        "Anthem Blue Cross Blue Shield - Medicaid (CA)")

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

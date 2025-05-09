"""Test the prior authorization text substitution utility"""

import typing
import datetime
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from fighthealthinsurance.models import (
    PriorAuthRequest,
    ProposedPriorAuth,
    ProfessionalUser,
    UserDomain,
)
from fighthealthinsurance.prior_auth_utils import PriorAuthTextSubstituter
from fhi_users.models import ProfessionalDomainRelation

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class PriorAuthTextSubstituterTest(TestCase):
    """Test the PriorAuthTextSubstituter utility class"""

    def setUp(self):
        """Set up test data for each test"""
        # Create a domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
            office_fax="555-123-4567",
        )

        # Create a user
        self.user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
            first_name="Test",
            last_name="Provider",
        )

        # Create a professional user
        self.professional = ProfessionalUser.objects.create(
            user=self.user,
            active=True,
            npi_number="1234567890",
            provider_type="Physician",
            credentials="MD",
            fax_number="555-987-6543",
            display_name="Dr. Test Provider",
        )

        # Link professional to domain
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create a prior auth request
        self.prior_auth = PriorAuthRequest.objects.create(
            creator_professional_user=self.professional,
            domain=self.domain,
            diagnosis="Type 2 Diabetes",
            treatment="Continuous Glucose Monitor",
            insurance_company="Blue Cross Blue Shield",
            patient_name="John Smith",
            patient_health_history="Patient has had Type 2 Diabetes for 5 years",
            plan_id="BCBS123456",
            member_id="MEM987654",
            patient_dob=datetime.date(1975, 5, 15),
            urgent=True,
            status="questions_asked",
        )

    def test_substitute_patient_info(self):
        """Test substituting patient information in a template"""
        template_text = """
        Date: $today

        Re: Prior Authorization for $patient_name (DOB: $patient_dob)
        Insurance ID: $plan_id
        Member ID: $member_id

        To Whom It May Concern:

        I am writing to request prior authorization for $treatment for my patient,
        $patient_name, who has been diagnosed with $diagnosis.
        """

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, template_text
        )

        # Check that patient info was substituted
        self.assertIn(
            f"Re: Prior Authorization for {self.prior_auth.patient_name}", result
        )
        self.assertIn(f"DOB: {self.prior_auth.patient_dob}", result)
        self.assertIn(f"Insurance ID: {self.prior_auth.plan_id}", result)
        self.assertIn(f"Member ID: {self.prior_auth.member_id}", result)
        self.assertIn(f"diagnosed with {self.prior_auth.diagnosis}", result)
        self.assertIn(
            f"request prior authorization for {self.prior_auth.treatment}", result
        )

        # Check that today's date was substituted
        today_str = datetime.date.today().strftime("%B %d, %Y")
        self.assertIn(f"Date: {today_str}", result)

    def test_substitute_provider_info(self):
        """Test substituting provider information in a template"""
        template_text = """
        $practice_name
        $practice_address
        Phone: $practice_phone
        Fax: $practice_fax

        Provider: $provider_name, $provider_credentials
        NPI: $provider_npi
        Provider Type: $provider_type
        Provider Fax: $provider_fax
        """

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, template_text
        )

        # Check that provider info was substituted
        self.assertIn(self.domain.business_name, result)
        self.assertIn(self.domain.address1, result)
        self.assertIn(f"Phone: {self.domain.visible_phone_number}", result)
        self.assertIn(f"Fax: {self.domain.office_fax}", result)
        self.assertIn(self.professional.display_name, result)
        self.assertIn(self.professional.credentials, result)
        self.assertIn(self.professional.npi_number, result)
        self.assertIn(self.professional.provider_type, result)
        self.assertIn(self.professional.fax_number, result)

    def test_substitute_medical_info(self):
        """Test substituting medical information in a template"""
        template_text = """
        $urgent

        DIAGNOSIS: $diagnosis
        REQUESTED TREATMENT: $treatment
        INSURANCE: $insurance_company
        """

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, template_text
        )

        # Check that medical info was substituted
        self.assertIn(f"DIAGNOSIS: {self.prior_auth.diagnosis}", result)
        self.assertIn(f"REQUESTED TREATMENT: {self.prior_auth.treatment}", result)
        self.assertIn(f"INSURANCE: {self.prior_auth.insurance_company}", result)
        self.assertIn("URGENT", result)  # Since self.prior_auth.urgent is True

    def test_missing_fields_use_placeholders(self):
        """Test that missing fields use appropriate placeholders"""
        # Create a prior auth with minimal information
        minimal_prior_auth = PriorAuthRequest.objects.create(
            diagnosis="Hypothyroidism",
            treatment="Levothyroxine",
            insurance_company="Aetna",
            status="initial",
        )

        template_text = """
        Patient: $patient_name (DOB: $patient_dob)
        Insurance ID: $plan_id
        Member ID: $member_id
        Provider: $provider_name, $provider_credentials
        Practice: $practice_name
        Address: $practice_address
        Phone: $practice_phone
        Fax: $practice_fax
        """

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            minimal_prior_auth, template_text
        )

        # Check that placeholders were used for missing fields
        self.assertIn("Patient: [PATIENT NAME]", result)
        self.assertIn("DOB: [DATE OF BIRTH]", result)
        self.assertIn("Insurance ID: [PLAN ID]", result)
        self.assertIn("Member ID: [MEMBER ID]", result)
        self.assertIn("Provider: [PROVIDER NAME]", result)
        self.assertIn("Practice: [PRACTICE NAME]", result)
        self.assertIn("Address: [PRACTICE ADDRESS]", result)
        self.assertIn("Phone: [PRACTICE PHONE]", result)

    def test_empty_input_text(self):
        """Test that empty input text returns empty output"""
        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, ""
        )
        self.assertEqual(result, "")

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, None
        )
        self.assertEqual(result, None)

    def test_invalid_template_syntax(self):
        """Test handling of invalid template syntax"""
        # Template with an unclosed placeholder
        invalid_template = (
            "Patient name: $patient_name but this $placeholder is invalid"
        )

        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, invalid_template
        )

        # Should still substitute valid placeholders and leave invalid ones
        self.assertIn(f"Patient name: {self.prior_auth.patient_name}", result)
        self.assertIn("$placeholder is invalid", result)


    @patch(
        "fighthealthinsurance.prior_auth_utils.PriorAuthTextSubstituter._build_context_dict"
    )
    def test_fallback_to_basic_context(self, mock_build_context):
        """Test fallback to basic context when building context dict fails"""
        # Configure the mock to raise an exception
        mock_build_context.side_effect = Exception("Test exception")

        template_text = """
        Diagnosis: $diagnosis
        Treatment: $treatment
        Insurance: $insurance_company
        Date: $today
        """

        # This should still not raise an exception
        result = PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            self.prior_auth, template_text
        )

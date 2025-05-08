"""Test the integration of prior auth text substitution with prior auth generation"""

import typing
import asyncio
import uuid
from unittest.mock import patch, MagicMock, AsyncMock

from django.test import TestCase
from django.contrib.auth import get_user_model

from fighthealthinsurance.models import (
    PriorAuthRequest,
    ProposedPriorAuth,
    ProfessionalUser,
    UserDomain,
)
from fighthealthinsurance.generate_prior_auth import PriorAuthGenerator
from fighthealthinsurance.prior_auth_utils import PriorAuthTextSubstituter

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class MockPriorAuthModel:
    """Mock model for testing prior auth generation"""

    async def generate_prior_auth_response(self, prompt: str) -> str:
        """Return a simple template with placeholders for testing substitution"""
        return """
        Date: $today

        RE: Prior Authorization Request for $treatment

        Insurance: $insurance_company
        Patient: $patient_name (DOB: $patient_dob)
        Member ID: $member_id
        Insurance ID: $plan_id

        Provider: $provider_name, $provider_credentials (NPI: $provider_npi)
        Practice: $practice_name
        Address: $practice_address
        Phone: $practice_phone
        Fax: $practice_fax

        DIAGNOSIS: $diagnosis

        $urgent

        This letter is to request prior authorization for $treatment for my patient,
        $patient_name, who has been diagnosed with $diagnosis.

        Sincerely,

        $provider_name, $provider_credentials
        $practice_name
        """


class PriorAuthGenerationSubstitutionTest(TestCase):
    """Test the substitution of placeholders during prior auth generation"""

    @classmethod
    def setUpClass(cls):
        """Set up test environment once for all tests"""
        super().setUpClass()
        # Mock the ml_router to return our mock model
        cls.ml_router_patcher = patch("fighthealthinsurance.ml.ml_router.ml_router")
        cls.mock_ml_router = cls.ml_router_patcher.start()

        # Set up the mock ML model
        cls.mock_model = MockPriorAuthModel()
        cls.mock_ml_router.get_prior_auth_backends.return_value = [cls.mock_model]

    @classmethod
    def tearDownClass(cls):
        """Clean up test environment"""
        cls.ml_router_patcher.stop()
        super().tearDownClass()

    def setUp(self):
        """Set up test data for each test"""
        # Create a domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Medical Group",
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
        self.domain_relation = ProfessionalDomainRelation.objects.create(
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
            patient_dob="1975-05-15",
            urgent=True,
            status="questions_answered",
            answers={
                "How long has the patient had this condition?": "5 years",
                "Has the patient tried other treatments?": "Yes, diet and exercise",
            },
        )

        # Create the generator
        self.generator = PriorAuthGenerator()

    @patch("uuid.uuid4", return_value=uuid.UUID("12345678-1234-5678-1234-567812345678"))
    @patch(
        "fighthealthinsurance.generate_prior_auth.PriorAuthGenerator._create_proposal"
    )
    async def test_substitution_during_proposal_generation(
        self, mock_create_proposal, mock_uuid
    ):
        """Test that placeholders are substituted during proposal generation"""
        # Configure the mock to capture the text
        mock_create_proposal.return_value = AsyncMock()

        # Generate a single proposal
        async def run_generator():
            results = []
            async for result in self.generator.generate_prior_auth_proposals(
                self.prior_auth
            ):
                results.append(result)
                break  # We only need one result
            return results[0] if results else None

        proposal_result = await run_generator()

        # Verify a proposal was generated
        self.assertIsNotNone(proposal_result)
        self.assertIn("proposed_id", proposal_result)
        self.assertIn("text", proposal_result)

        # Get the result text
        result_text = proposal_result["text"]

        # Check that patient info was substituted
        self.assertIn(f"Patient: {self.prior_auth.patient_name}", result_text)
        self.assertIn(f"Member ID: {self.prior_auth.member_id}", result_text)
        self.assertIn(f"Insurance ID: {self.prior_auth.plan_id}", result_text)
        self.assertIn(f"DIAGNOSIS: {self.prior_auth.diagnosis}", result_text)
        self.assertIn(f"for {self.prior_auth.treatment}", result_text)

        # Check that provider info was substituted
        self.assertIn(self.professional.display_name, result_text)
        self.assertIn(self.professional.credentials, result_text)
        self.assertIn(self.professional.npi_number, result_text)

        # Check that practice info was substituted
        self.assertIn(self.domain.business_name, result_text)
        self.assertIn(self.domain.address1, result_text)
        self.assertIn(self.domain.visible_phone_number, result_text)
        self.assertIn(self.domain.office_fax, result_text)

        # Verify that mock_create_proposal was called with substituted text
        args, kwargs = mock_create_proposal.call_args
        self.assertEqual(args[0], self.prior_auth)
        self.assertEqual(args[1], uuid.UUID("12345678-1234-5678-1234-567812345678"))
        substituted_text = args[2]

        # The text passed to _create_proposal should have placeholders substituted
        self.assertIn(f"Patient: {self.prior_auth.patient_name}", substituted_text)
        self.assertIn(self.professional.display_name, substituted_text)
        self.assertIn(self.domain.business_name, substituted_text)

    def test_substitute_values_in_proposal(self):
        """Test the utility method for substituting values in an existing proposal"""
        # Create a template with placeholders
        template_text = """
        Date: $today

        Patient: $patient_name
        Provider: $provider_name
        Diagnosis: $diagnosis
        Treatment: $treatment
        """

        # Use the utility method directly
        substituted_text = self.generator.substitute_values_in_proposal(
            self.prior_auth, template_text
        )

        # Check substitutions
        self.assertIn(f"Patient: {self.prior_auth.patient_name}", substituted_text)
        self.assertIn(f"Provider: {self.professional.display_name}", substituted_text)
        self.assertIn(f"Diagnosis: {self.prior_auth.diagnosis}", substituted_text)
        self.assertIn(f"Treatment: {self.prior_auth.treatment}", substituted_text)

    @patch(
        "fighthealthinsurance.prior_auth_utils.PriorAuthTextSubstituter.substitute_patient_and_provider_info"
    )
    def test_substitute_values_on_proposal_selection(self, mock_substitute):
        """Test that values are substituted when a proposal is selected"""
        from django.urls import reverse
        from rest_framework.test import APIClient
        from rest_framework import status

        # Mock the substitution function
        mock_substitute.return_value = "SUBSTITUTED TEXT"

        # Create a proposal
        proposal = ProposedPriorAuth.objects.create(
            prior_auth_request=self.prior_auth, text="Original text with $placeholders"
        )

        # Log in
        client = APIClient()
        client.force_authenticate(user=self.user)

        # Select the proposal
        url = reverse("prior-auth-select-proposal", args=[str(self.prior_auth.id)])
        data = {
            "token": str(self.prior_auth.token),
            "proposed_id": str(proposal.proposed_id),
        }

        response = client.post(url, data, format="json")

        # Check response
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify substitution was called
        mock_substitute.assert_called_once()

        # Verify the prior auth text was updated with substituted text
        self.prior_auth.refresh_from_db()
        self.assertEqual(self.prior_auth.text, "SUBSTITUTED TEXT")

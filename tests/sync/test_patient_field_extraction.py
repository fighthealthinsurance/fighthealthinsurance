"""Test the patient field extraction functionality."""

import json
from datetime import datetime
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import ProfessionalUser, UserDomain
from fhi_users.models import ProfessionalDomainRelation
from fighthealthinsurance.ml.ml_router import ml_router

User = get_user_model()


class PatientFieldExtractionTest(APITestCase):
    """Test the patient field extraction from PDF documents."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
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
            city="San Francisco",
            zipcode="94105",
        )

        # Create a user and professional user
        self.user = User.objects.create_user(
            username="testpro",
            password="testpassword123",
            email="test@example.com",
            first_name="Test",
            last_name="Professional",
        )

        self.professional_user = ProfessionalUser.objects.create(
            user=self.user,
            active=True,
            npi_number="1234567890",  # Correct field name
            display_name="Dr. Test Professional",  # Correct field name
            provider_type="Physician",
        )

        # Create domain relation
        self.domain_relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional_user,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Login the user
        self.client.login(username="testpro", password="testpassword123")

        # URL for the extract_patient_fields endpoint
        self.extract_fields_url = reverse("prior-auth-extract-patient-fields")

        # Sample text from PDF with patient information
        self.sample_patient_text = """
        Patient Information:
        Name: John Smith
        Date of Birth: 01/15/1980
        Member ID: ABC123456789
        Insurance: Blue Cross Blue Shield
        Plan ID: PLAN987654

        Additional Information:
        Address: 123 Main St, Anytown, CA 94105
        Phone: (555) 123-4567
        Email: john.smith@example.com
        """

    @patch("fighthealthinsurance.ml.ml_router.ml_router.entity_extract_backends")
    def test_extract_patient_fields_success(self, mock_extract_backends):
        """Test successful extraction of patient fields."""
        # Mock the ML router to return predefined model
        mock_model = MagicMock()

        # Configure the mock model to return specific values for different entity types
        async def mock_get_entity(text, entity_type):
            entity_values = {
                "patient_name": "John Smith",
                "member_id": "ABC123456789",
                "date_of_birth": "01/15/1980",
                "plan_id": "PLAN987654",
                "insurance_company": "Blue Cross Blue Shield",
            }
            return entity_values.get(entity_type)

        mock_model.get_entity.side_effect = mock_get_entity
        mock_extract_backends.return_value = [mock_model]

        # Make request to extract patient fields
        response = self.client.post(
            self.extract_fields_url, {"text": self.sample_patient_text}, format="json"
        )

        # Assert the response status and structure
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        patient_fields = response.data
        self.assertEqual(patient_fields["patient_name"], "John Smith")
        self.assertEqual(patient_fields["member_id"], "ABC123456789")
        self.assertEqual(patient_fields["insurance_company"], "Blue Cross Blue Shield")
        self.assertEqual(patient_fields["plan_id"], "PLAN987654")

        # Test that dob field is properly parsed as a date
        self.assertIn("dob", patient_fields)
        # If using Django's built-in JSON serialization, dates are converted to strings
        # Check for ISO format or the format defined in Django's settings
        self.assertTrue(isinstance(patient_fields["dob"], str))

        # Verify the model was called with expected parameters
        mock_extract_backends.assert_called_once_with(use_external=False)

    def test_extract_patient_fields_unauthenticated(self):
        """Test that unauthenticated users cannot extract patient fields."""
        # Logout the user
        self.client.logout()

        # Make request to extract patient fields
        response = self.client.post(
            self.extract_fields_url, {"text": self.sample_patient_text}, format="json"
        )

        # Assert unauthorized response
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_extract_patient_fields_invalid_request(self):
        """Test handling of invalid request data."""
        # Make request with missing text field
        response = self.client.post(
            self.extract_fields_url, {}, format="json"  # Empty data
        )

        # Assert bad request response
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("fighthealthinsurance.ml.ml_router.ml_router.entity_extract_backends")
    def test_extract_patient_fields_date_conversion(self, mock_extract_backends):
        """Test date of birth is properly converted to a date object."""
        # Mock the ML router to return predefined model
        mock_model = MagicMock()

        # Configure the mock to return a date string for date_of_birth
        async def mock_get_entity(text, entity_type):
            entity_values = {
                "patient_name": "Jane Doe",
                "date_of_birth": "1990-05-15",  # ISO format date
            }
            return entity_values.get(entity_type)

        mock_model.get_entity.side_effect = mock_get_entity
        mock_extract_backends.return_value = [mock_model]

        # Make request to extract patient fields
        response = self.client.post(
            self.extract_fields_url, {"text": self.sample_patient_text}, format="json"
        )

        # Assert the response status and date format
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("dob", response.data)
        self.assertIn("patient_name", response.data)

        # Verify the date is properly formatted
        try:
            # Check if the date can be parsed
            datetime.fromisoformat(response.data["dob"].replace("Z", "+00:00"))
            is_valid_date = True
        except (ValueError, AttributeError):
            is_valid_date = False

        self.assertTrue(is_valid_date)

    @patch("fighthealthinsurance.ml.ml_router.ml_router.entity_extract_backends")
    def test_extract_patient_fields_no_models_available(self, mock_extract_backends):
        """Test handling when no ML models are available for extraction."""
        # Mock the ML router to return no models
        mock_extract_backends.return_value = []

        # Make request to extract patient fields
        response = self.client.post(
            self.extract_fields_url, {"text": self.sample_patient_text}, format="json"
        )

        # Assert service unavailable response
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("error", response.data)
        self.assertEqual(
            response.data["error"], "No entity extraction models available"
        )

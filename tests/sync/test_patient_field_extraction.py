"""Test the patient field extraction functionality."""

import asyncio
import json
from datetime import datetime
from unittest.mock import patch, MagicMock
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from loguru import logger

from fighthealthinsurance.models import ProfessionalUser, UserDomain
from fhi_users.models import ProfessionalDomainRelation
from fighthealthinsurance.ml.ml_models import ProviderUnavailable
from fighthealthinsurance.ml.ml_router import ml_router

User = get_user_model()


class _ExtractionTestCase(APITestCase):
    """A logged-in professional and a sample of PDF text."""

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


class PatientFieldExtractionTest(_ExtractionTestCase):
    """Test the patient field extraction from PDF documents."""

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

        # Each extracted field must carry a confidence note so the UI can
        # surface uncertainty rather than silently prefilling values.
        self.assertIn("confidence_notes", patient_fields)
        for field in (
            "patient_name",
            "member_id",
            "dob",
            "plan_id",
            "insurance_company",
        ):
            self.assertIn(field, patient_fields["confidence_notes"])
            self.assertIn(
                patient_fields["confidence_notes"][field], {"high", "medium", "low"}
            )

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
    def test_extract_patient_fields_rejects_english_words(self, mock_extract_backends):
        """Test that common English words returned by ML are rejected for identifier fields."""
        mock_model = MagicMock()

        async def mock_get_entity(text, entity_type):
            # Simulate ML model returning common words instead of real IDs
            entity_values = {
                "patient_name": "John Smith",
                "member_id": "covers",  # Bad: common English word
                "date_of_birth": "01/15/1980",
                "plan_id": "amount",  # Bad: common English word
                "insurance_company": "Blue Cross Blue Shield",
            }
            return entity_values.get(entity_type)

        mock_model.get_entity.side_effect = mock_get_entity
        mock_extract_backends.return_value = [mock_model]

        response = self.client.post(
            self.extract_fields_url, {"text": self.sample_patient_text}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # plan_id and member_id should be rejected (they're English words with no digits)
        patient_fields = response.data
        self.assertNotEqual(patient_fields.get("plan_id"), "amount")
        self.assertNotEqual(patient_fields.get("member_id"), "covers")
        # Other fields should still be present
        self.assertEqual(patient_fields["patient_name"], "John Smith")
        self.assertEqual(patient_fields["insurance_company"], "Blue Cross Blue Shield")

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


_PATIENT_ENTITIES = {
    "patient_name": "John Smith",
    "member_id": "ABC123456789",
    "date_of_birth": "01/15/1980",
    "plan_id": "PLAN987654",
    "insurance_company": "Blue Cross Blue Shield",
}


def _reader(error=None, entities=None):
    """An entity backend that raises ``error`` for every field, or answers
    from ``entities`` (None for a field it finds nothing for)."""
    model = MagicMock()
    answers = entities or {}

    async def get_entity(text, entity_type):
        if error is not None:
            raise error
        return answers.get(entity_type)

    model.get_entity.side_effect = get_entity
    return model


class PatientFieldExtractionFallthroughTest(_ExtractionTestCase):
    """One entity model that cannot be reached no longer empties the form:
    each field goes on to the next model, and when none can read the
    document the person is told so instead of getting an empty form."""

    def _post(self, *backends):
        with patch(
            "fighthealthinsurance.ml.ml_router.ml_router.entity_extract_backends",
            return_value=list(backends),
        ):
            return self.client.post(
                self.extract_fields_url,
                {"text": self.sample_patient_text},
                format="json",
            )

    def test_an_unavailable_first_model_falls_through_to_the_next(self):
        response = self._post(
            _reader(error=ProviderUnavailable("HTTP 503")),
            _reader(entities=_PATIENT_ENTITIES),
        )
        self.assertEqual(
            (response.status_code, response.data.get("member_id")),
            (status.HTTP_200_OK, "ABC123456789"),
        )

    def test_a_transport_error_on_the_first_model_falls_through_to_the_next(self):
        response = self._post(
            _reader(error=asyncio.TimeoutError()),
            _reader(entities=_PATIENT_ENTITIES),
        )
        self.assertEqual(response.data.get("patient_name"), "John Smith")

    def test_no_model_able_to_read_is_a_503_not_an_empty_form(self):
        response = self._post(
            _reader(error=ProviderUnavailable("HTTP 503")),
            _reader(error=ProviderUnavailable("in transport-failure cooldown")),
        )
        self.assertEqual(
            (response.status_code, "error" in response.data),
            (status.HTTP_503_SERVICE_UNAVAILABLE, True),
        )

    def test_no_model_able_to_read_logs_one_warning_and_no_errors(self):
        records = []
        sink = logger.add(lambda m: records.append(m.record), level="DEBUG")
        try:
            self._post(_reader(error=ProviderUnavailable("HTTP 503")))
        finally:
            logger.remove(sink)
        levels = [
            r["level"].name
            for r in records
            if r["level"].name in ("WARNING", "ERROR") and "rest_views" in r["name"]
        ]
        self.assertEqual(levels, ["WARNING"])

    def test_a_model_that_finds_nothing_is_still_an_answer(self):
        response = self._post(_reader(entities={}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model, authenticate
from rest_framework.test import APIClient
from rest_framework import status

from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
    ExtraUserProperties,
    UserContactInfo,
)

User = get_user_model()


class UserDomainManagementTests(TestCase):
    """Additional tests for UserDomainViewSet functionality."""

    def setUp(self) -> None:
        self.client = APIClient()
        # Create a domain
        self.domain = UserDomain.objects.create(
            name="medicalclinic",
            visible_phone_number="5552223333",
            internal_phone_number="5552224444",
            active=True,
            display_name="Medical Clinic",
            country="USA",
            state="NY",
            city="New York",
            address1="789 Medical Ave",
            zipcode="10001",
            beta=True,
            office_fax="5552225555",
        )

        # Create an admin user
        self.admin_password = "AdminPass123!"
        self.admin_user = User.objects.create_user(
            username=f"admin_doctor🐼{self.domain.id}",
            password=self.admin_password,
            email="admin_doctor@example.com",
            first_name="Admin",
            last_name="Doctor",
            is_active=True,
        )
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user,
            active=True,
            npi_number="1112223333",
            provider_type="Physician",
            most_common_denial="Medical necessity",
        )
        ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create a regular professional user
        self.pro_password = "DocPass123!"
        self.pro_user = User.objects.create_user(
            username=f"doctor🐼{self.domain.id}",
            password=self.pro_password,
            email="doctor@example.com",
            first_name="Regular",
            last_name="Doctor",
            is_active=True,
        )
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user,
            active=True,
            npi_number="4445556666",
            provider_type="Specialist",
        )
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

    def test_update_address_with_partial_data(self) -> None:
        """Test updating only some domain fields works correctly."""
        url = reverse("user_domain-update-address")

        # Login as admin
        self.client.login(
            username=self.admin_user.username, password=self.admin_password
        )
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Update only office_fax and zipcode
        partial_update_data = {
            "office_fax": "9998887777",
            "zipcode": "10002",
        }

        response = self.client.post(url, partial_update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify only the specified fields were updated
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.office_fax, "9998887777")
        self.assertEqual(self.domain.zipcode, "10002")
        # Other fields should remain unchanged
        self.assertEqual(self.domain.display_name, "Medical Clinic")
        self.assertEqual(self.domain.address1, "789 Medical Ave")
        self.assertEqual(self.domain.city, "New York")

    def test_list_domain_returns_all_fields(self) -> None:
        """Test that domain list endpoint returns all expected fields."""
        url = reverse("user_domain-list")

        # Login as regular professional
        self.client.login(username=self.pro_user.username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check if all expected fields are in the response
        data = response.data
        expected_fields = [
            "name",
            "display_name",
            "visible_phone_number",
            "internal_phone_number",
            "country",
            "state",
            "city",
            "address1",
            "address2",
            "zipcode",
            "office_fax",
        ]

        for field in expected_fields:
            self.assertIn(field, data)


class ProfessionalProfileManagementTests(TestCase):
    """Tests for ProfessionalUserUpdateViewSet functionality."""

    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="specialists",
            visible_phone_number="7778889999",
            active=True,
            display_name="Specialists Clinic",
        )

        # Create a professional user
        self.password = "SpecPass123!"
        self.user = User.objects.create_user(
            username=f"specialist🐼{self.domain.id}",
            password=self.password,
            email="specialist@example.com",
            first_name="Special",
            last_name="Doctor",
            is_active=True,
        )
        self.professional = ProfessionalUser.objects.create(
            user=self.user,
            active=True,
            npi_number="7778889999",
            provider_type="Neurologist",
        )
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            pending_domain_relation=False,
        )

    def test_update_professional_partial_fields(self) -> None:
        """Test updating only some fields of professional profile."""
        url = reverse("professional_profile-update-profile")

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # Update only provider_type and most_common_denial
        partial_update_data = {
            "provider_type": "Neurosurgeon",
            "most_common_denial": "Experimental procedure",
        }

        response = self.client.post(url, partial_update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify only the specified fields were updated
        self.professional.refresh_from_db()
        self.assertEqual(self.professional.provider_type, "Neurosurgeon")
        self.assertEqual(self.professional.most_common_denial, "Experimental procedure")
        # NPI number should remain unchanged
        self.assertEqual(self.professional.npi_number, "7778889999")

    def test_update_professional_with_invalid_npi(self) -> None:
        """Test updating professional with invalid NPI number fails validation."""
        url = reverse("professional_profile-update-profile")

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # Try to update with invalid NPI format
        invalid_data = {
            "npi_number": "12345",  # Too short, should be 10 digits
        }

        response = self.client.post(url, invalid_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Verify NPI was not updated
        self.professional.refresh_from_db()
        self.assertEqual(self.professional.npi_number, "7778889999")


class PasswordManagementTests(TestCase):
    """Additional tests for password management functionality."""

    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="hospital",
            visible_phone_number="1112223333",
            active=True,
        )

        # Regular user
        self.password = "InitialPass123!"
        self.user = User.objects.create_user(
            username=f"doctor🐼{self.domain.id}",
            password=self.password,
            email="doctor@example.com",
            is_active=True,
        )

        # Professional user for this regular user
        ProfessionalUser.objects.create(
            user=self.user,
            active=True,
        )

        # Patient user
        self.patient_password = "PatientPass123!"
        self.patient_user = User.objects.create_user(
            username=f"patient🐼{self.domain.id}",
            password=self.patient_password,
            email="patient@example.com",
            is_active=True,
        )
        PatientUser.objects.create(
            user=self.patient_user,
            active=True,
        )

    def test_password_too_short(self) -> None:
        """Test changing to a password that's too short."""
        url = reverse("password-change-password")

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # Try to change to a short password
        change_data = {
            "current_password": self.password,
            "new_password": "short1",  # Too short, should be at least 8 characters
        }

        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify original password still works
        self.client.logout()
        self.assertTrue(
            self.client.login(username=self.user.username, password=self.password)
        )

    def test_password_no_digits(self) -> None:
        """Test changing to a password without digits fails validation."""
        url = reverse("password-change-password")

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # Try to change to a password without digits
        change_data = {
            "current_password": self.password,
            "new_password": "NoDigitsPassword",  # No digits
        }

        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify original password still works
        self.client.logout()
        self.assertTrue(
            self.client.login(username=self.user.username, password=self.password)
        )

    def test_password_all_digits(self) -> None:
        """Test changing to a password with only digits fails validation."""
        url = reverse("password-change-password")

        # Login
        self.client.login(username=self.user.username, password=self.password)

        # Try to change to a password with only digits
        change_data = {
            "current_password": self.password,
            "new_password": "12345678",  # All digits
        }

        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify original password still works
        self.client.logout()
        self.assertTrue(
            self.client.login(username=self.user.username, password=self.password)
        )

    def test_patient_password_change(self) -> None:
        """Test password change works for patient users too."""
        url = reverse("password-change-password")
        new_password = "NewPatientPass456!"

        # Login as patient
        self.client.login(
            username=self.patient_user.username, password=self.patient_password
        )

        # Change password
        change_data = {
            "current_password": self.patient_password,
            "new_password": new_password,
        }

        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify password change
        self.client.logout()
        self.assertFalse(
            self.client.login(
                username=self.patient_user.username, password=self.patient_password
            )
        )
        self.assertTrue(
            self.client.login(
                username=self.patient_user.username, password=new_password
            )
        )

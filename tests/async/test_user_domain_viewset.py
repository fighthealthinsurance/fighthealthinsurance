from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from fhi_users.models import (
    UserDomain,
    ProfessionalUser,
    ProfessionalDomainRelation,
    ExtraUserProperties,
)

User = get_user_model()


class UserDomainViewSetTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
            beta=True,
        )

        # Admin user
        self.admin_password = "adminpass123"
        self.admin_user = User.objects.create_user(
            username=f"adminuser🐼{self.domain.id}",
            password=self.admin_password,
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
            is_active=True,
        )
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user,
            active=True,
        )
        # Admin relationship
        self.admin_relation = ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Regular user
        self.user_password = "userpass123"
        self.regular_user = User.objects.create_user(
            username=f"regularuser🐼{self.domain.id}",
            password=self.user_password,
            email="regular@example.com",
            first_name="Regular",
            last_name="User",
            is_active=True,
        )
        self.regular_professional = ProfessionalUser.objects.create(
            user=self.regular_user,
            active=True,
        )
        # Regular user relationship
        self.regular_relation = ProfessionalDomainRelation.objects.create(
            professional=self.regular_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

    def test_user_domain_list(self) -> None:
        """Test listing domain info works for authenticated users."""
        url = reverse("user_domain-list")

        # Unauthenticated request should fail
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Admin user should see domain info
        self.client.login(
            username=self.admin_user.username, password=self.admin_password
        )
        session = self.client.session
        session["domain_id"] = self.domain.id
        session.save()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "testdomain")
        self.assertEqual(response.data["display_name"], "Test Domain")

        # Non-admin user should also see domain info
        self.client.logout()
        self.client.login(
            username=self.regular_user.username, password=self.user_password
        )
        session = self.client.session
        session["domain_id"] = self.domain.id
        session.save()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "testdomain")

    def test_user_domain_update_address(self) -> None:
        """Test updating domain info works for admin users only."""
        url = reverse("user_domain-update-address")
        update_data = {
            "display_name": "Updated Domain Name",
            "office_fax": "5551234567",
            "address1": "456 New St",
            "city": "New City",
        }

        # Unauthenticated request should fail
        response = self.client.post(url, update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Non-admin user should not be able to update
        self.client.login(
            username=self.regular_user.username, password=self.user_password
        )
        session = self.client.session
        session["domain_id"] = self.domain.id
        session.save()

        response = self.client.post(url, update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Admin user should be able to update
        self.client.logout()
        self.client.login(
            username=self.admin_user.username, password=self.admin_password
        )
        session = self.client.session
        session["domain_id"] = self.domain.id
        session.save()

        response = self.client.post(url, update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")

        # Verify changes were saved
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.display_name, "Updated Domain Name")
        self.assertEqual(self.domain.office_fax, "5551234567")
        self.assertEqual(self.domain.address1, "456 New St")
        self.assertEqual(self.domain.city, "New City")


class ProfessionalUserUpdateViewSetTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            active=True,
            display_name="Test Domain",
            zipcode="12345",
        )

        # Professional user
        self.password = "testpass123"
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password=self.password,
            email="test@example.com",
            first_name="Test",
            last_name="User",
            is_active=True,
        )
        self.professional = ProfessionalUser.objects.create(
            user=self.user,
            active=True,
            npi_number="1234567890",
            provider_type="Doctor",
        )
        self.relation = ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            pending_domain_relation=False,
        )

    def test_update_professional_profile(self) -> None:
        """Test updating professional profile works."""
        url = reverse("professional_profile-update-profile")
        update_data = {
            "npi_number": "9876543210",
            "provider_type": "Specialist",
            "most_common_denial": "Experimental treatment",
            "fax_number": "5551234567",
            "display_name": "Dr. Test User",
        }

        # Unauthenticated request should fail
        response = self.client.post(url, update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Authenticated user should be able to update their profile
        self.client.login(username=self.user.username, password=self.password)
        response = self.client.post(url, update_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")

        # Verify changes were saved
        self.professional.refresh_from_db()
        self.assertEqual(self.professional.npi_number, "9876543210")
        self.assertEqual(self.professional.provider_type, "Specialist")
        self.assertEqual(self.professional.most_common_denial, "Experimental treatment")
        self.assertEqual(self.professional.fax_number, "5551234567")
        self.assertEqual(self.professional.display_name, "Dr. Test User")


class PasswordViewSetTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            active=True,
            zipcode="12345",
        )

        # User
        self.password = "oldPassword123"
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password=self.password,
            email="test@example.com",
            is_active=True,
        )

    def test_change_password(self) -> None:
        """Test changing password works."""
        url = reverse("password-change-password")
        new_password = "newPassword456"
        change_data = {
            "current_password": self.password,
            "new_password": new_password,
        }

        # Unauthenticated request should fail
        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Authenticated user should be able to change their password
        self.client.login(username=self.user.username, password=self.password)
        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")

        # Verify old password no longer works
        self.client.logout()
        self.assertFalse(
            self.client.login(username=self.user.username, password=self.password)
        )

        # Verify new password works
        self.assertTrue(
            self.client.login(username=self.user.username, password=new_password)
        )

    def test_change_password_with_incorrect_current_password(self) -> None:
        """Test changing password fails with incorrect current password."""
        url = reverse("password-change-password")
        change_data = {
            "current_password": "wrongPassword",
            "new_password": "newPassword456",
        }

        self.client.login(username=self.user.username, password=self.password)
        response = self.client.post(url, change_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify original password still works
        self.client.logout()
        self.assertTrue(
            self.client.login(username=self.user.username, password=self.password)
        )

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


class ProfessionalUserManagementTests(TestCase):
    def setUp(self) -> None:
        """
        Set up test environment with a domain, admin professional, and non-admin professional
        """
        self.client = APIClient()

        # Create a test domain
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
        )

        # Create admin user
        self.admin_user = User.objects.create_user(
            username=f"adminuser🐼{self.domain.id}",
            password="adminpass",
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )
        self.admin_user.is_active = True
        self.admin_user.save()
        ExtraUserProperties.objects.create(user=self.admin_user, email_verified=True)

        # Create admin professional
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user, active=True, npi_number="1111111111"
        )

        # Create admin relation to domain
        self.admin_relation = ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            pending=False,
            active=True,
            admin=True,
        )

        # Create regular professional user (active, not admin)
        self.regular_user = User.objects.create_user(
            username=f"reguser🐼{self.domain.id}",
            password="regpass",
            email="reg@example.com",
            first_name="Regular",
            last_name="User",
        )
        self.regular_user.is_active = True
        self.regular_user.save()
        ExtraUserProperties.objects.create(user=self.regular_user, email_verified=True)

        self.regular_professional = ProfessionalUser.objects.create(
            user=self.regular_user, active=True, npi_number="2222222222"
        )

        self.regular_relation = ProfessionalDomainRelation.objects.create(
            professional=self.regular_professional,
            domain=self.domain,
            pending=False,
            active=True,
            admin=False,
        )

        # Create pending professional user
        self.pending_user = User.objects.create_user(
            username=f"pendinguser🐼{self.domain.id}",
            password="pendingpass",
            email="pending@example.com",
            first_name="Pending",
            last_name="User",
        )
        self.pending_user.is_active = True
        self.pending_user.save()
        ExtraUserProperties.objects.create(user=self.pending_user, email_verified=True)

        self.pending_professional = ProfessionalUser.objects.create(
            user=self.pending_user, active=False, npi_number="3333333333"
        )

        self.pending_relation = ProfessionalDomainRelation.objects.create(
            professional=self.pending_professional,
            domain=self.domain,
            pending=True,
            active=False,
            admin=False,
        )

    def test_list_active_professionals(self):
        """Test listing active professionals in a domain"""
        # Login as admin user
        self.client.login(username=self.admin_user.username, password="adminpass")

        # Set session domain
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # List active professionals
        url = reverse("professional_user-list-active-in-domain")
        response = self.client.post(url, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should include both active professionals (admin and regular)
        data = response.json()
        self.assertEqual(len(data), 2)

        # Check that both active professionals are in the response
        professional_ids = [p["professional_user_id"] for p in data]
        self.assertIn(self.admin_professional.id, professional_ids)
        self.assertIn(self.regular_professional.id, professional_ids)

        # Pending professional should not be included
        self.assertNotIn(self.pending_professional.id, professional_ids)

    def test_list_pending_professionals(self):
        """Test listing pending professionals in a domain"""
        # Login as admin user
        self.client.login(username=self.admin_user.username, password="adminpass")

        # Set session domain
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # List pending professionals
        url = reverse("professional_user-list-pending-in-domain")
        response = self.client.post(url, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should include only the pending professional
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["professional_user_id"], self.pending_professional.id)
        self.assertEqual(data[0]["name"], "Pending User")
        self.assertEqual(data[0]["npi"], "3333333333")

    def test_accept_pending_professional(self):
        """Test accepting a pending professional user"""
        # Login as admin user
        self.client.login(username=self.admin_user.username, password="adminpass")

        # Set session domain
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Accept the pending professional
        url = reverse("professional_user-accept")
        data = {
            "professional_user_id": self.pending_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "accepted")

        # Verify relationship status has been updated
        self.pending_relation.refresh_from_db()
        self.assertFalse(self.pending_relation.pending)
        self.assertTrue(self.pending_relation.active)

        # Verify professional user is now active
        self.pending_professional.refresh_from_db()
        self.assertTrue(self.pending_professional.active)

        # Check that listing pending users no longer includes this professional
        url = reverse("professional_user-list-pending-in-domain")
        response = self.client.post(url, format="json")
        pending_data = response.json()
        self.assertEqual(len(pending_data), 0)

        # Check that listing active users now includes this professional
        url = reverse("professional_user-list-active-in-domain")
        response = self.client.post(url, format="json")
        active_data = response.json()
        professional_ids = [p["professional_user_id"] for p in active_data]
        self.assertIn(self.pending_professional.id, professional_ids)

    def test_reject_pending_professional(self):
        """Test rejecting a pending professional user"""
        # Login as admin user
        self.client.login(username=self.admin_user.username, password="adminpass")

        # Set session domain
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Reject the pending professional
        url = reverse("professional_user-reject")
        data = {
            "professional_user_id": self.pending_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "rejected")

        # Verify relationship status has been updated
        self.pending_relation.refresh_from_db()
        self.assertFalse(self.pending_relation.pending)
        self.assertFalse(self.pending_relation.active)
        self.assertTrue(self.pending_relation.rejected)

        # Check that listing pending users no longer includes this professional
        url = reverse("professional_user-list-pending-in-domain")
        response = self.client.post(url, format="json")
        pending_data = response.json()
        self.assertEqual(len(pending_data), 0)

        # Check that listing active users still doesn't include this professional
        url = reverse("professional_user-list-active-in-domain")
        response = self.client.post(url, format="json")
        active_data = response.json()
        professional_ids = [p["professional_user_id"] for p in active_data]
        self.assertNotIn(self.pending_professional.id, professional_ids)

    def test_regular_user_cannot_manage_professionals(self):
        """Test that non-admin users cannot accept/reject professionals"""
        # Login as regular (non-admin) user
        self.client.login(username=self.regular_user.username, password="regpass")

        # Set session domain
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Try to accept the pending professional
        url = reverse("professional_user-accept")
        data = {
            "professional_user_id": self.pending_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(url, data, format="json")

        # Should be forbidden for non-admin users
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["status"], "error")

        # Try to reject the pending professional
        url = reverse("professional_user-reject")
        response = self.client.post(url, data, format="json")

        # Should be forbidden for non-admin users
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Make sure the relationship wasn't changed
        self.pending_relation.refresh_from_db()
        self.assertTrue(self.pending_relation.pending)
        self.assertFalse(self.pending_relation.active)
        self.assertFalse(self.pending_relation.rejected)

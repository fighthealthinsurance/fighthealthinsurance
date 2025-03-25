import uuid

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone

from rest_framework.test import APIClient
from rest_framework import status
from fhi_users.models import (
    UserDomain,
    ExtraUserProperties,
    ProfessionalUser,
    ResetToken,
)

User = get_user_model()


class PaymentViewsTests(TestCase):
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
            beta=True,  # Set beta flag to True
        )
        self.user_password = "testpass"
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password=self.user_password,
            email="test@example.com",
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_finish_payment_view_get(self) -> None:
        url = reverse("professionaluser-finish_payment")
        params = {
            "domain_id": self.domain.id,
            "professional_user_id": self.user.id,
            "continue_url": "http://example.com/continue",
        }
        response = self.client.get(url, params, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertIn("next_url", response.json())

    def test_complete_payment_view_post(self) -> None:
        url = reverse("complete_payment")
        data = {
            "domain_id": self.domain.id,
            "professional_user_id": self.user.id,
            "continue_url": "http://example.com/continue",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertIn("next_url", response.json())

    def test_complete_payment_view_missing_parameters(self) -> None:
        url = reverse("complete_payment")
        data = {
            "domain_id": self.domain.id,
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_payment_view_invalid_domain(self) -> None:
        url = reverse("complete_payment")
        data = {
            "domain_id": 99999,
            "professional_user_id": self.user.id,
            "continue_url": "http://example.com/continue",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_payment_view_invalid_professional_user(self) -> None:
        url = reverse("complete_payment")
        data = {
            "domain_id": self.domain.id,
            "professional_user_id": 99999,
            "continue_url": "http://example.com/continue",
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

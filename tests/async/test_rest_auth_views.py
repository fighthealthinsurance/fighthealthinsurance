from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from django.contrib.auth import get_user_model
from fhi_users.models import (
    UserDomain,
    ExtraUserProperties,
    UserContactInfo,
    PatientUser,
    VerificationToken,
)
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.utils import timezone


User = get_user_model()


class RestAuthViewsTests(TestCase):
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
        )
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_rest_login_view_with_domain(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "success")

    def test_rest_login_view_with_phone(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "",
            "phone": "1234567890",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "success")

    def test_rest_login_view_with_domain_and_phone(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "testdomain",
            "phone": "1234567890",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "success")

    def test_rest_login_view_without_domain_and_phone(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rest_login_view_with_incorrect_credentials(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "wrongpass",
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_patient_user_view(self) -> None:
        url = reverse("create_patient_user-list")
        data = {
            "username": "newuser",
            "password": "newpass",
            "email": "newuser1289@example.com",
            "provider_phone_number": "1234567890",
            "country": "USA",
            "state": "CA",
            "city": "Test City",
            "address1": "123 Test St",
            "address2": "",
            "zipcode": "12345",
            "domain_name": "testdomain",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "pending")
        new_user = User.objects.get(email="newuser1289@example.com")
        token = VerificationToken.objects.get(user=new_user)
        new_user_user_extra_properties = ExtraUserProperties.objects.get(user=new_user)
        self.assertFalse(new_user_user_extra_properties.email_verified)
        self.assertIsNotNone(UserContactInfo.objects.get(user=new_user))
        self.assertIsNotNone(PatientUser.objects.get(user=new_user))

    def test_verify_email_view(self) -> None:
        url = reverse("rest_verify_email-verify")
        VerificationToken.objects.create(
            user=self.user, token=default_token_generator.make_token(self.user)
        )
        print(f"Making verification for {self.user} w/pk {self.user.pk}")
        data = {
            "token": VerificationToken.objects.get(user=self.user).token,
            "user_id": self.user.pk,
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        new_user_user_extra_properties = ExtraUserProperties.objects.get(user=self.user)
        self.assertTrue(new_user_user_extra_properties.email_verified)

    def test_send_verification_email_after_user_creation(self) -> None:
        url = reverse("create_patient_user-list")
        data = {
            "username": "newuser",
            "password": "newpass",
            "email": "newuser1289@example.com",
            "provider_phone_number": "1234567890",
            "country": "USA",
            "state": "CA",
            "city": "Test City",
            "address1": "123 Test St",
            "address2": "",
            "zipcode": "12345",
            "domain_name": "testdomain",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        new_user = User.objects.get(email="newuser1289@example.com")
        token = VerificationToken.objects.get(user=new_user)
        self.assertIsNotNone(token)
        # Check that one message has been sent.
        self.assertEqual(len(mail.outbox), 1)
        # Verify that the subject of the first message is correct.
        self.assertEqual(mail.outbox[0].subject, "Activate your account.")

    def test_email_confirmation_with_verification_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        VerificationToken.objects.create(
            user=self.user, token=default_token_generator.make_token(self.user)
        )
        data = {
            "token": VerificationToken.objects.get(user=self.user).token,
            "user_id": self.user.pk,
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        new_user_user_extra_properties = ExtraUserProperties.objects.get(user=self.user)
        self.assertTrue(new_user_user_extra_properties.email_verified)

    def test_email_confirmation_with_invalid_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        data = {"token": "invalidtoken", "user_id": self.user.pk}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "failure")
        self.assertEqual(response.json()["message"], "Invalid activation link")

    def test_email_confirmation_with_expired_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        token = VerificationToken.objects.create(
            user=self.user,
            token=default_token_generator.make_token(self.user),
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )
        data = {"token": token.token, "user_id": self.user.pk}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json()["status"], "failure")
        self.assertEqual(response.json()["message"], "Activation link has expired")

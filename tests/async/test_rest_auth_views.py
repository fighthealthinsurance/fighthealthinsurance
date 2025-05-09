import uuid

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.utils import timezone

from rest_framework.test import APIClient
from rest_framework import status
from fhi_users.models import (
    UserDomain,
    ExtraUserProperties,
    UserContactInfo,
    PatientUser,
    VerificationToken,
    ResetToken,
    ProfessionalUser,
    ProfessionalDomainRelation,
    PendingProStripeCheckoutSession,
)

from unittest.mock import patch

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
        url = reverse("patient_user-list")
        data = {
            "username": "newuser",
            "password": "newLongerPasswordMagicCheetoCheeto123",
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
        # Make sure they can't login yet
        self.assertFalse(
            self.client.login(
                username=new_user.username,
                password="newLongerPasswordMagicCheetoCheeto123",
            )
        )
        # Then make sure they can log in post verification
        verify_url = reverse("rest_verify_email-verify")
        print(f"Making verification for {new_user} w/pk {new_user.pk}")
        verify_data = {
            "token": VerificationToken.objects.get(user=new_user).token,
            "user_id": new_user.pk,
        }
        response = self.client.post(verify_url, verify_data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        new_user.refresh_from_db()
        self.assertTrue(new_user.is_active)
        new_user_user_extra_properties = ExtraUserProperties.objects.get(user=new_user)
        self.assertTrue(new_user_user_extra_properties.email_verified)
        self.assertTrue(
            self.client.login(
                username=new_user.username,
                password="newLongerPasswordMagicCheetoCheeto123",
            )
        )

    def test_patient_user_creates_with_correct_name(self) -> None:
        """Test that patient user is created with the correct first and last name."""
        url = reverse("patient_user-list")
        # Test with first_name and last_name
        data = {
            "username": "nameuser1@example.com",
            "password": "SecurePassword123",
            "email": "nameuser1@example.com",
            "first_name": "Test",
            "last_name": "User",
            "provider_phone_number": "1234567890",
            "country": "USA",
            "state": "CA",
            "city": "Test City",
            "address1": "123 Test St",
            "zipcode": "12345",
            "domain_name": "testdomain",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "pending")

        # Verify user was created with correct name
        new_user1 = User.objects.get(email="nameuser1@example.com")
        self.assertEqual(new_user1.first_name, "Test")
        self.assertEqual(new_user1.last_name, "User")

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
        url = reverse("patient_user-list")
        data = {
            "username": "newuser",
            "password": "newLongerPasswordMagicCheetoCheeto123",
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
        # Check that two messages have been sent (one to BCC)
        self.assertEqual(len(mail.outbox), 2)
        # Verify that the subject of the first message is correct.
        self.assertEqual(mail.outbox[0].subject, "Activate your account.")

    def test_email_confirmation_with_verification_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        self.user.is_active = False
        self.user.save()
        # Verify the user can not login until verification
        self.assertFalse(
            self.client.login(username=self.user.username, password=self.user_password)
        )
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
        # Verify the user can login now
        self.assertTrue(
            self.client.login(username=self.user.username, password=self.user_password)
        )

    def test_email_confirmation_with_invalid_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        data = {"token": "invalidtoken", "user_id": self.user.pk}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")
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
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["message"], "Activation link has expired")

    def test_create_professional_user_with_new_domain(self) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newprouser@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": "newdomain",
                "visible_phone_number": "1234567891",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "newdomain",
                "visible_phone_number": "1234567891",
                "internal_phone_number": "0987654322",
                "display_name": "New Domain",
                "country": "USA",
                "state": "CA",
                "city": "New City",
                "address1": "456 New St",
                "zipcode": "67890",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertTrue(UserDomain.objects.filter(name="newdomain").exists())

    def test_create_professional_user_with_existing_domain_name_but_create_set_to_true(
        self,
    ) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser2",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newprouser2@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": "testdomain",
                "visible_phone_number": "1234567892",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "testdomain",
                "visible_phone_number": "1234567892",
                "internal_phone_number": "0987654323",
                "display_name": "Test Domain",
                "country": "USA",
                "state": "CA",
                "city": "Test City",
                "address1": "123 Test St",
                "zipcode": "12345",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_professional_user_with_existing_domain_name_and_create_set_to_false(
        self,
    ) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser2",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newprouser2@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": "testdomain",
                "visible_phone_number": "1234567892",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": False,
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))

    def test_create_professional_user_with_existing_visible_phone_number(self) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser3",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newprouser3@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": "anothernewdomain",
                "visible_phone_number": "1234567890",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "anothernewdomain",
                "visible_phone_number": "1234567890",
                "internal_phone_number": "0987654324",
                "display_name": "Another New Domain",
                "country": "USA",
                "state": "CA",
                "city": "Another City",
                "address1": "789 Another St",
                "zipcode": "54321",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_request_password_reset(self) -> None:
        url = reverse("password_reset-request-reset")
        data = {
            "username": "testuser",
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "reset_requested")
        self.assertTrue(ResetToken.objects.filter(user=self.user).exists())

    def test_request_password_reset_with_phone(self) -> None:
        url = reverse("password_reset-request-reset")
        data = {
            "username": "testuser",
            "domain": "",
            "phone": "1234567890",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "reset_requested")
        self.assertTrue(ResetToken.objects.filter(user=self.user).exists())

    def test_finish_password_reset_invalid(self) -> None:
        reset_token = ResetToken.objects.create(user=self.user, token=uuid.uuid4().hex)
        url = reverse("password_reset-finish-reset")
        data = {
            "token": reset_token.token,
            "new_password": "newtestpass",
        }
        response = self.client.post(url, data, format="json")
        self.assertNotIn(response.status_code, range(200, 300))

    def test_finish_password_reset_valid(self) -> None:
        reset_token = ResetToken.objects.create(user=self.user, token=uuid.uuid4().hex)
        url = reverse("password_reset-finish-reset")
        data = {
            "token": reset_token.token,
            "new_password": "newtestpass111",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "password_reset_complete")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("newtestpass111"))

    def test_finish_password_reset_with_invalid_token(self) -> None:
        url = reverse("password_reset-finish-reset")
        data = {
            "token": "invalidtoken",
            "new_password": "newtestpass",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["message"], "Invalid reset token")

    def test_finish_password_reset_with_expired_token(self) -> None:
        reset_token = ResetToken.objects.create(
            user=self.user,
            token=uuid.uuid4().hex,
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )
        url = reverse("password_reset-finish-reset")
        data = {
            "token": reset_token.token,
            "new_password": "newtestpass",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["message"], "Reset token has expired")
        self.assertEqual(response.json()["error"], "Reset token has expired")

    def test_rest_login_view_with_nonexistent_domain(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "nonexistentdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")

    def test_rest_login_view_with_invalid_phone(self) -> None:
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "",
            "phone": "9999999999",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")

    def test_rest_login_view_with_inactive_user(self) -> None:
        self.user.is_active = False
        self.user.save()
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": "testpass",
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_sends_verification_email_for_inactive_user(self) -> None:
        """Test that attempting to login with an inactive user sends a verification email."""
        # Set user to inactive
        self.user.is_active = False
        self.user.save()

        # Get the current email outbox count
        email_count_before = len(mail.outbox)

        # Attempt to login with inactive user
        url = reverse("rest_login-login")
        data = {
            "username": "testuser",
            "password": self.user_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")

        # Verify response is unauthorized
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("User is inactive", response.json()["error"])

        # Check that a verification email was sent (plus one for BCC)
        self.assertEqual(len(mail.outbox), email_count_before + 2)

        # Verify the email subject
        self.assertEqual(mail.outbox[-2].subject, "Activate your account.")
        self.assertIn(self.user.email, mail.outbox[-2].to)

    def test_verify_email_with_nonexistent_user(self) -> None:
        url = reverse("rest_verify_email-verify")
        data = {
            "token": "sometoken",
            "user_id": 99999,
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")

    def test_verify_email_without_token(self) -> None:
        url = reverse("rest_verify_email-verify")
        data = {"user_id": self.user.pk}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_professional_user_without_required_fields(self) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser4",
                "password": "newLongerPasswordMagicCheetoCheeto123",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "newdomain2",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_professional_user_with_invalid_email(self) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "newprouser5",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "invalid-email",
                "first_name": "New",
                "last_name": "User",
                "domain_name": "newdomain3",
                "visible_phone_number": "1234567893",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "newdomain3",
                "visible_phone_number": "1234567893",
                "internal_phone_number": "0987654325",
                "display_name": "New Domain 3",
                "country": "USA",
                "state": "CA",
                "city": "Test City",
                "address1": "123 Test St",
                "zipcode": "12345",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_request_password_reset_nonexistent_user(self) -> None:
        url = reverse("password_reset-request-reset")
        data = {
            "username": "nonexistentuser",
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["error"], "User does not exist")

    def test_whoami_view_authed(self):
        # Log in
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
        # Check whoami
        url = reverse("whoami-list")
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()[0]
        self.assertEqual(data["email"], self.user.email)
        self.assertEqual(data["domain_id"], str(self.domain.id))
        self.assertEqual(data["domain_name"], self.domain.name)
        self.assertIn("highest_role", data)

    def test_whoami_view_beta_flag(self):
        # Log in
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
        # Check whoami
        url = reverse("whoami-list")
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()[0]
        self.assertEqual(data["email"], self.user.email)
        self.assertEqual(data["domain_id"], str(self.domain.id))
        self.assertEqual(data["domain_name"], self.domain.name)
        self.assertTrue(data["beta"])  # Check beta flag

    def test_create_professional_user_with_short_password(self) -> None:
        url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "shortpass",
                "password": "short",  # Too short password
                "email": "shortpass@example.com",
                "first_name": "Short",
                "last_name": "Pass",
                "domain_name": "newshortpass",
                "visible_phone_number": "1234567893",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "newshortpass",
                "visible_phone_number": "1234567893",
                "internal_phone_number": "0987654325",
                "display_name": "Short Pass Domain",
                "country": "USA",
                "state": "CA",
                "city": "Test City",
                "address1": "123 Test St",
                "zipcode": "12345",
            },
        }
        response = self.client.post(url, data, format="json")
        self.assertNotIn(response.status_code, range(200, 300))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_duplicate_patient_user_view(self) -> None:
        """Test that creating a patient user with an email that already exists fails correctly."""
        url = reverse("patient_user-list")
        # Create first patient user
        data = {
            "username": "duplicate_patient@example.com",
            "password": "SecurePassword123",
            "email": "duplicate_patient@example.com",
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

        # Verify user was created
        self.assertTrue(
            User.objects.filter(email="duplicate_patient@example.com").exists()
        )

        # Try to create a second user with the same email and username
        data2 = dict(data)
        data2["username"] = "duplicate_patient@example.com"

        response2 = self.client.post(url, data2, format="json")

        # Verify it fails with a 400 status code
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify the error message is appropriate
        error_data = response2.json()
        self.assertIn("error", error_data)
        self.assertEqual(error_data["error"], "A user with this email already exists")

    def test_verification_email_throttling(self) -> None:
        """Test that verification emails are not sent more than once within 10 minutes."""
        # Create a user for testing
        test_user = User.objects.create_user(
            username=f"throttleuser🐼{self.domain.id}",
            password="testpass123",
            email="throttle@example.com",
        )
        test_user.is_active = False
        test_user.save()

        # Count emails before sending
        email_count_before = len(mail.outbox)

        # Send first verification email
        from fhi_users.emails import send_verification_email

        send_verification_email(self.client.request(), test_user)

        # Check that email was sent
        self.assertEqual(len(mail.outbox), email_count_before + 2)  # +2 for BCC

        # Try to send another verification email immediately
        send_verification_email(self.client.request(), test_user)

        # Check that no additional email was sent (still just the first one)
        self.assertEqual(len(mail.outbox), email_count_before + 2)

        # Verify the token exists
        token = VerificationToken.objects.get(user=test_user)
        self.assertIsNotNone(token)

        # Manually modify the token creation time to be 11 minutes ago
        token.created_at = timezone.now() - timezone.timedelta(minutes=11)
        token.save()

        # Now try sending again
        send_verification_email(self.client.request(), test_user)

        # Check that a new email was sent
        self.assertEqual(len(mail.outbox), email_count_before + 4)  # +2 more with BCC

    def test_first_only_parameter_works(self) -> None:
        """Test that first_only=True prevents sending any follow-up emails."""
        # Create a user for testing
        test_user = User.objects.create_user(
            username=f"firstonlyuser🐼{self.domain.id}",
            password="testpass123",
            email="firstonly@example.com",
        )
        test_user.is_active = False
        test_user.save()

        # Count emails before sending
        email_count_before = len(mail.outbox)

        # Send first verification email with first_only=True
        from fhi_users.emails import send_verification_email

        send_verification_email(self.client.request(), test_user, first_only=True)

        # Check that email was sent
        self.assertEqual(len(mail.outbox), email_count_before + 2)  # +2 for BCC

        # Manually modify the token creation time to be 11 minutes ago
        token = VerificationToken.objects.get(user=test_user)
        token.created_at = timezone.now() - timezone.timedelta(minutes=11)
        token.save()

        # Try to send another email with first_only=True
        send_verification_email(self.client.request(), test_user, first_only=True)

        # Check that no additional email was sent (still just the first one)
        self.assertEqual(len(mail.outbox), email_count_before + 2)


class TestE2EProfessionalUserSignupFlow(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_end_to_end_happy_path(self):
        domain_name = "new_test_domain"
        phone_number = "1234567890"
        signup_url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": "testpro@example.com",
                "password": "temp12345",
                "email": "testpro@example.com",
                "first_name": "Test",
                "last_name": "Pro",
                "domain_name": domain_name,
                "visible_phone_number": phone_number,
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": domain_name,
                "visible_phone_number": phone_number,
                "internal_phone_number": "0987654321",
                "display_name": "Test Domain Display",
                "country": "USA",
                "state": "CA",
                "city": "TestCity",
                "address1": "123 Test St",
                "zipcode": "99999",
            },
        }
        old_outbox = len(mail.outbox)
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)
        # We don't send the message right away -- we wait for stripe callback.
        self.assertEqual(len(mail.outbox), old_outbox)

        # Simulate Stripe webhook call to trigger the email
        new_user = User.objects.get(email="testpro@example.com")
        from fighthealthinsurance.common_view_logic import StripeWebhookHelper

        stripe_event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "subscription": "sub_12345",
                    "customer": "cus_12345",
                    "metadata": {
                        "payment_type": "professional_domain_subscription",
                        "professional_id": new_user.professionaluser.id,
                        "domain_id": UserDomain.objects.get(name=domain_name).id,
                    },
                }
            },
        }
        StripeWebhookHelper.handle_checkout_session_completed(
            None, stripe_event["data"]["object"]
        )

        # Now we expect it + BCC
        self.assertEqual(len(mail.outbox), old_outbox + 2)
        # User can not log in pre-verification
        self.assertFalse(
            self.client.login(username=new_user.username, password="temp12345")
        )
        # Check the verification
        verify_url = reverse("rest_verify_email-verify")
        token = VerificationToken.objects.get(user=new_user)
        response = self.client.post(
            verify_url, {"token": token.token, "user_id": new_user.pk}, format="json"
        )
        self.assertIn(response.status_code, range(200, 300))
        new_user.refresh_from_db()
        self.assertTrue(
            self.client.login(username=new_user.username, password="temp12345")
        )
        # Ok but hit the rest endpoint for the login so we have a domain id
        login_url = reverse("rest_login-login")
        data = {
            "username": "testpro@example.com",
            "password": "temp12345",
            "domain": domain_name,
        }
        response = self.client.post(login_url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "success")
        # Have the now logged in pro-user start to make an appeal
        get_pending_url = reverse("patient_user-get-or-create-pending")
        data = {
            "username": "testpro@example.com",
            "first_name": "fname",
            "last_name": "lname",
        }
        response = self.client.post(get_pending_url, data, format="json")
        print(response)
        self.assertIn(response.status_code, range(200, 300))
        patient_id = response.json()["id"]

        # Let’s pretend to create a denial record via DenialFormSerializer
        denial_create_url = reverse("denials-list")
        denial_data = {
            "insurance_company": "Test Insurer",
            "denial_text": "Sample denial text",
            "email": "testpro@example.com",
            "patient_id": patient_id,
            "pii": True,
            "tos": True,
            "privacy": True,
        }
        response = self.client.post(denial_create_url, denial_data, format="json")

        # Validate response has data we can use for the generate appeal websocket
        denial_response = response.json()
        self.assertIn("denial_id", denial_response)
        denial_id = denial_response["denial_id"]

        # Now we need to call the websocket...

    def test_retry_signup_with_same_email_and_domain(self):
        """Test that a user can press back and retry the signup from Stripe checkout."""
        # Do initial signup
        domain_name = "retry_domain"
        phone_number = "5551234567"
        email = "retry@example.com"

        signup_url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": email,
                "password": "temp12345",
                "email": email,
                "first_name": "Retry",
                "last_name": "User",
                "domain_name": domain_name,
                "visible_phone_number": phone_number,
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": domain_name,
                "visible_phone_number": phone_number,
                "internal_phone_number": "5559876543",
                "display_name": "Retry Domain",
                "country": "USA",
                "state": "CA",
                "city": "RetryCity",
                "address1": "123 Retry St",
                "zipcode": "12345",
            },
        }

        # First signup attempt
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("next_url", response.json())

        # Check that objects were created
        domain = UserDomain.objects.get(name=domain_name)
        self.assertFalse(domain.active)  # Domain should be inactive initially

        user = User.objects.get(email=email)
        professional_user = ProfessionalUser.objects.get(user=user)
        self.assertFalse(professional_user.active)

        # Store the IDs for comparison later
        domain_id = domain.id
        user_id = user.id
        pro_user_id = professional_user.id

        # Verify a checkout session was created
        checkout_session = PendingProStripeCheckoutSession.objects.filter(
            email=email
        ).first()
        self.assertIsNotNone(checkout_session)
        self.assertEqual(checkout_session.email, email)
        self.assertEqual(checkout_session.professional_user_id, professional_user.id)
        self.assertEqual(str(checkout_session.domain_id), str(domain.id))

        # Now simulate a second signup attempt (as if user pressed back from Stripe)
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("next_url", response.json())

        # Check that we don't have duplicate objects
        self.assertEqual(UserDomain.objects.filter(name=domain_name).count(), 1)
        self.assertEqual(User.objects.filter(email=email).count(), 1)

        # Get the objects again
        domain = UserDomain.objects.get(name=domain_name)
        user = User.objects.get(email=email)
        professional_user = ProfessionalUser.objects.get(user=user)

        # Verify we have two checkout sessions (original + retry)
        self.assertEqual(
            PendingProStripeCheckoutSession.objects.filter(email=email).count(), 2
        )

    def test_retry_signup_with_active_domain_fails(self):
        """Test that a domain can only be reused if it's still pending/inactive."""
        # Initial signup
        domain_name = "active_domain"
        phone_number = "5551111111"
        email = "active@example.com"

        signup_url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": email,
                "password": "temp12345",
                "email": email,
                "first_name": "Active",
                "last_name": "User",
                "domain_name": domain_name,
                "visible_phone_number": phone_number,
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": domain_name,
                "visible_phone_number": phone_number,
                "internal_phone_number": "5559998888",
                "display_name": "Active Domain",
                "country": "USA",
                "state": "CA",
                "city": "ActiveCity",
                "address1": "123 Active St",
                "zipcode": "12345",
            },
        }

        # First signup attempt
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)

        # Manually activate the domain (simulating completed Stripe checkout)
        domain = UserDomain.objects.get(name=domain_name)
        domain.active = True
        domain.stripe_subscription_id = "sub_12345"
        domain.save()

        # Attempt second signup with same domain name
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Domain is active, cannot delete", response.json()["error"])

    def test_signup_with_different_email_same_domain_fails(self):
        """Test that a different user cannot use the same domain name."""
        # Initial signup
        domain_name = "shared_domain"
        phone_number = "5552222222"
        email = "first@example.com"

        signup_url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": email,
                "password": "temp12345",
                "email": email,
                "first_name": "First",
                "last_name": "User",
                "domain_name": domain_name,
                "visible_phone_number": phone_number,
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": domain_name,
                "visible_phone_number": phone_number,
                "internal_phone_number": "5557778888",
                "display_name": "Shared Domain",
                "country": "USA",
                "state": "CA",
                "city": "SharedCity",
                "address1": "123 Shared St",
                "zipcode": "12345",
            },
        }

        # First user signup
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)

        # Second user with same domain name
        data2 = data.copy()
        data2["user_signup_info"] = data["user_signup_info"].copy()
        data2["user_signup_info"]["email"] = "second@example.com"
        data2["user_signup_info"]["username"] = "second@example.com"

        # Attempt signup with different email but same domain name
        response = self.client.post(signup_url, data2, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Domain already exists", response.json()["error"])

    def test_signup_with_different_session_id_but_same_email(self):
        """Test that signup with different session ID but same email cleans up properly."""
        # Initial signup
        domain_name = "session_domain"
        phone_number = "5553333333"
        email = "session@example.com"

        signup_url = reverse("professional_user-list")
        data = {
            "user_signup_info": {
                "username": email,
                "password": "temp12345",
                "email": email,
                "first_name": "Session",
                "last_name": "User",
                "domain_name": domain_name,
                "visible_phone_number": phone_number,
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": domain_name,
                "visible_phone_number": phone_number,
                "internal_phone_number": "5556667777",
                "display_name": "Session Domain",
                "country": "USA",
                "state": "CA",
                "city": "SessionCity",
                "address1": "123 Session St",
                "zipcode": "12345",
            },
        }

        # First signup attempt
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 201)

        # Get the created objects
        initial_domain = UserDomain.objects.get(name=domain_name)
        initial_pro_user = ProfessionalUser.objects.get(user__email=email)

        # Reset the session
        self.client.cookies.clear()

        # Verify initial checkout session was created
        initial_session = PendingProStripeCheckoutSession.objects.get(
            email=email,
            domain_id=initial_domain.id,
            professional_user_id=initial_pro_user.id,
        )
        self.assertIsNotNone(initial_session)

        # Now do another signup attempt with the same data - this should
        # not work and handle the previously created pending records
        response = self.client.post(signup_url, data, format="json")
        self.assertEqual(response.status_code, 400)


class ProfessionalInvitationTests(TestCase):
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
        self.admin_password = "adminpass123"
        self.admin_user = User.objects.create_user(
            username=f"adminuser🐼{self.domain.id}",
            password=self.admin_password,
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )
        self.admin_user.is_active = True
        self.admin_user.save()

        # Create professional user for admin
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user,
            active=True,
        )

        # Create admin relationship with domain
        ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create non-admin user
        self.regular_password = "regularpass123"
        self.regular_user = User.objects.create_user(
            username=f"regularuser🐼{self.domain.id}",
            password=self.regular_password,
            email="regular@example.com",
            first_name="Regular",
            last_name="User",
        )
        self.regular_user.is_active = True
        self.regular_user.save()

        self.regular_professional = ProfessionalUser.objects.create(
            user=self.regular_user,
            active=True,
        )

        # Create non-admin relationship with domain
        self.regular_relation = ProfessionalDomainRelation.objects.create(
            professional=self.regular_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

        ExtraUserProperties.objects.create(user=self.admin_user, email_verified=True)
        ExtraUserProperties.objects.create(user=self.regular_user, email_verified=True)

    def test_invite_professional_as_admin(self):
        # Login as admin user
        url = reverse("rest_login-login")
        data = {
            "username": "adminuser",
            "password": self.admin_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "success")

        # Send invitation
        invite_url = reverse("professional_user-invite")
        invite_data = {
            "user_email": "newinvite@example.com",
            "name": "New Professional",
        }

        # Check email count before invitation
        mail_count_before = len(mail.outbox)

        response = self.client.post(invite_url, invite_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "invitation_sent")

        # Verify email was sent + BCC
        self.assertEqual(len(mail.outbox), mail_count_before + 2)
        self.assertEqual(mail.outbox[-2].subject, "Invitation to Join Practice")
        self.assertIn("newinvite@example.com", mail.outbox[-2].to)

        # Check email content
        email_content = mail.outbox[-2].body
        self.assertIn("testdomain", email_content)
        self.assertIn("1234567890", email_content)  # Practice phone number
        self.assertIn("Admin User", email_content)  # Inviter name

    def test_invite_professional_as_non_admin(self):
        # Login as regular user
        url = reverse("rest_login-login")
        data = {
            "username": "regularuser",
            "password": self.regular_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Try to send invitation
        invite_url = reverse("professional_user-invite")
        invite_data = {
            "user_email": "newinvite@example.com",
            "name": "New Professional",
        }

        response = self.client.post(invite_url, invite_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.json()["error"], "User does not have admin privileges"
        )

    def test_invite_professional_without_authentication(self):
        # Try to send invitation without logging in
        invite_url = reverse("professional_user-invite")
        invite_data = {
            "user_email": "newinvite@example.com",
            "name": "New Professional",
        }

        response = self.client.post(invite_url, invite_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_professional_as_admin(self):
        """Test that an admin can delete (mark as rejected) a professional in the domain"""
        # Login as admin user
        url = reverse("rest_login-login")
        data = {
            "username": "adminuser",
            "password": self.admin_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "success")

        # Delete the regular professional
        delete_url = reverse("professional_user-delete")
        delete_data = {
            "professional_user_id": self.regular_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(delete_url, delete_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "deleted")

        # Verify relationship status has been updated
        self.regular_relation.refresh_from_db()
        self.assertFalse(self.regular_relation.pending_domain_relation)
        self.assertFalse(self.regular_relation.active_domain_relation)
        self.assertTrue(self.regular_relation.rejected)

        # Check that listing active users no longer includes this professional
        url = reverse("professional_user-list-active-in-domain")
        response = self.client.post(url, format="json")
        active_data = response.json()
        professional_ids = [p["professional_user_id"] for p in active_data]
        self.assertNotIn(self.regular_professional.id, professional_ids)

    def test_delete_professional_as_non_admin(self):
        """Test that a non-admin cannot delete a professional in the domain"""
        # Login as regular user
        url = reverse("rest_login-login")
        data = {
            "username": "regularuser",
            "password": self.regular_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Try to delete another professional
        delete_url = reverse("professional_user-delete")
        delete_data = {
            "professional_user_id": self.admin_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(delete_url, delete_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.json()["error"], "User does not have admin privileges"
        )

        # Verify admin's relationship status has NOT been updated
        admin_relation = ProfessionalDomainRelation.objects.get(
            professional=self.admin_professional, domain=self.domain
        )
        self.assertTrue(admin_relation.active_domain_relation)
        self.assertFalse(admin_relation.rejected)

    def test_delete_professional_without_authentication(self):
        """Test that unauthenticated user cannot delete a professional in the domain"""
        # Try to delete without logging in
        delete_url = reverse("professional_user-delete")
        delete_data = {
            "professional_user_id": self.admin_professional.id,
            "domain_id": str(self.domain.id),
        }
        response = self.client.post(delete_url, delete_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CreateProfessionalInDomainTests(TestCase):
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
        self.admin_password = "adminpass123"
        self.admin_user = User.objects.create_user(
            username=f"adminuser🐼{self.domain.id}",
            password=self.admin_password,
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
        )
        self.admin_user.is_active = True
        self.admin_user.save()

        # Create professional user for admin
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user,
            active=True,
        )

        # Create admin relationship with domain
        ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create non-admin user
        self.regular_password = "regularpass123"
        self.regular_user = User.objects.create_user(
            username=f"regularuser🐼{self.domain.id}",
            password=self.regular_password,
            email="regular@example.com",
            first_name="Regular",
            last_name="User",
        )
        self.regular_user.is_active = True
        self.regular_user.save()

        self.regular_professional = ProfessionalUser.objects.create(
            user=self.regular_user,
            active=True,
        )

        # Create non-admin relationship with domain
        ProfessionalDomainRelation.objects.create(
            professional=self.regular_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

        ExtraUserProperties.objects.create(user=self.admin_user, email_verified=True)
        ExtraUserProperties.objects.create(user=self.regular_user, email_verified=True)

    def test_create_pro_as_admin(self):
        # Login as admin user
        url = reverse("rest_login-login")
        data = {
            "username": "adminuser",
            "password": self.admin_password,
            "domain": "testdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "success")

        # Create provider
        create_url = reverse("professional_user-create-professional-in-current-domain")
        provider_data = {
            "email": "newprovider@example.com",
            "first_name": "New",
            "last_name": "Provider",
            "npi_number": "1234567890",
            "provider_type": "Physician",
        }

        # Check email count before creation
        mail_count_before = len(mail.outbox)

        response = self.client.post(create_url, provider_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "professional_created")

        # Check that user was created
        new_user = User.objects.get(email="newprovider@example.com")
        self.assertEqual(new_user.first_name, "New")
        self.assertEqual(new_user.last_name, "Provider")
        # User object should not be active so they can not log in yet
        self.assertFalse(new_user.is_active)

        # Check that professional user was created
        professional = ProfessionalUser.objects.get(user=new_user)
        self.assertEqual(professional.npi_number, "1234567890")
        self.assertEqual(professional.provider_type, "Physician")
        # Professional user should be active
        self.assertTrue(professional.active)

        # Check that relationship was created
        relation = ProfessionalDomainRelation.objects.get(
            professional=professional, domain=self.domain
        )
        self.assertTrue(relation.active_domain_relation)
        self.assertFalse(relation.pending_domain_relation)
        self.assertFalse(relation.admin)

        # Verify the newly created provider appears in the listing of active providers
        list_url = reverse("professional_user-list-active-in-domain")
        response = self.client.post(list_url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        providers_data = response.json()
        self.assertTrue(isinstance(providers_data, list))

        # Find the newly created provider in the list
        new_provider_in_list = False
        for provider in providers_data:
            if "New" in provider.get("name") and "Provider" in provider.get("name"):
                new_provider_in_list = True
        self.assertTrue(
            new_provider_in_list, f"Should find new provider in list: {providers_data}"
        )


class UserDomainExistsTests(TestCase):
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

    def test_domain_exists_by_name(self) -> None:
        url = reverse("domain_exists-check")
        data = {
            "domain_name": "testdomain",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["exists"])

    def test_domain_exists_by_phone(self) -> None:
        url = reverse("domain_exists-check")
        data = {
            "phone_number": "1234567890",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["exists"])

    def test_domain_does_not_exist(self) -> None:
        url = reverse("domain_exists-check")
        data = {
            "domain_name": "nonexistentdomain",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["exists"])

    def test_domain_does_not_exist_by_phone(self) -> None:
        url = reverse("domain_exists-check")
        data = {
            "phone_number": "9999999999",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["exists"])

    def test_missing_both_parameters(self) -> None:
        url = reverse("domain_exists-check")
        data = {}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GetBillingUrlTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="billingtestdomain",
            visible_phone_number="5550000000",
            internal_phone_number="5550000001",
            active=True,
            display_name="Billing Test Domain",
            country="USA",
            state="CA",
            city="Billing City",
            address1="123 Billing St",
            zipcode="11111",
            beta=True,
            stripe_customer_id="cus_test12345",  # Required for billing portal
        )
        self.admin_password = "adminbillingpass"
        self.admin_user = User.objects.create_user(
            username=f"adminbillinguser🐼{self.domain.id}",
            password=self.admin_password,
            email="adminbilling@example.com",
            first_name="Admin",
            last_name="Billing",
        )
        self.admin_user.is_active = True
        self.admin_user.save()
        self.admin_professional = ProfessionalUser.objects.create(
            user=self.admin_user,
            active=True,
        )
        ProfessionalDomainRelation.objects.create(
            professional=self.admin_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )
        self.regular_password = "regularbillingpass"
        self.regular_user = User.objects.create_user(
            username=f"regularbillinguser🐼{self.domain.id}",
            password=self.regular_password,
            email="regularbilling@example.com",
            first_name="Regular",
            last_name="Billing",
        )
        self.regular_user.is_active = True
        self.regular_user.save()
        self.regular_professional = ProfessionalUser.objects.create(
            user=self.regular_user,
            active=True,
        )
        ProfessionalDomainRelation.objects.create(
            professional=self.regular_professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )
        ExtraUserProperties.objects.create(user=self.admin_user, email_verified=True)
        ExtraUserProperties.objects.create(user=self.regular_user, email_verified=True)

    @patch("stripe.billing_portal.Session.create")
    def test_billing_url_for_admin(self, mock_stripe_portal):
        mock_stripe_portal.return_value.url = "https://billing.stripe.com/test_portal"
        # Login as admin
        url = reverse("rest_login-login")
        data = {
            "username": "adminbillinguser",
            "password": self.admin_password,
            "domain": "billingtestdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Call get_billing_url
        billing_url = reverse("professional_user-get-billing-url")
        response = self.client.post(billing_url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("next_url", response.json())
        self.assertEqual(
            response.json()["next_url"], "https://billing.stripe.com/test_portal"
        )

    @patch("stripe.billing_portal.Session.create")
    def test_billing_url_for_non_admin(self, mock_stripe_portal):
        # Login as regular user
        url = reverse("rest_login-login")
        data = {
            "username": "regularbillinguser",
            "password": self.regular_password,
            "domain": "billingtestdomain",
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Call get_billing_url
        billing_url = reverse("professional_user-get-billing-url")
        response = self.client.post(billing_url, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("error", response.json())
        self.assertIn("not an admin", response.json()["error"])

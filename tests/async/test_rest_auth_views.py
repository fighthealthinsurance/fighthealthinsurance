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
)


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
        # Check that one message has been sent.
        self.assertEqual(len(mail.outbox), 1)
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

    def test_finish_password_reset(self) -> None:
        reset_token = ResetToken.objects.create(user=self.user, token=uuid.uuid4().hex)
        url = reverse("password_reset-finish-reset")
        data = {
            "token": reset_token.token,
            "new_password": "newtestpass",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        self.assertEqual(response.json()["status"], "password_reset_complete")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("newtestpass"))

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

        # Now we expect it
        self.assertEqual(len(mail.outbox), old_outbox + 1)
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
            active=True,
            admin=True,
            pending=False,
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
            active=True,
            admin=False,
            pending=False,
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

        # Verify email was sent
        self.assertEqual(len(mail.outbox), mail_count_before + 1)
        self.assertEqual(mail.outbox[-1].subject, "Invitation to Join Practice")
        self.assertIn("newinvite@example.com", mail.outbox[-1].to)

        # Check email content
        email_content = mail.outbox[-1].body
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
            active=True,
            admin=True,
            pending=False,
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
            active=True,
            admin=False,
            pending=False,
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
        self.assertTrue(relation.active)
        self.assertFalse(relation.pending)
        self.assertFalse(relation.admin)

        # Verify email was sent
        self.assertEqual(len(mail.outbox), mail_count_before + 1)
        self.assertEqual(
            mail.outbox[-1].subject, "Your Professional Account Has Been Created"
        )
        self.assertIn("newprovider@example.com", mail.outbox[-1].to)

        # Check email content
        email_content = mail.outbox[-1].body
        self.assertIn("testdomain", email_content)
        self.assertIn("Admin User", email_content)  # Inviter name
        self.assertIn("manage", email_content)  # Provider name
        self.assertIn("reset-password", email_content)  # Link to reset password
        # No reset token directly in the email
        self.assertNotIn("token=", email_content)

    def test_create_pro_as_non_admin(self):
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

        # Try to create provider
        create_url = reverse("professional_user-create-professional-in-current-domain")
        provider_data = {
            "email": "newprovider@example.com",
            "first_name": "New",
            "last_name": "Provider",
        }

        response = self.client.post(create_url, provider_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.json()["error"], "User does not have admin privileges"
        )

    def test_create_pro_with_existing_email(self):
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

        # Try to create professional with existing email
        create_url = reverse("professional_user-create-professional-in-current-domain")
        provider_data = {
            "email": "regular@example.com",  # This email already exists
            "first_name": "New",
            "last_name": "Provider",
        }

        response = self.client.post(create_url, provider_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json()["error"], "A user with this email already exists"
        )

"""Regression tests for the legacy web auth views in fhi_users.auth.auth_views."""

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from fhi_users.models import ExtraUserProperties

User = get_user_model()


class LoginViewMissingDomainTest(TestCase):
    """POST to login with valid form fields but neither domain nor phone."""

    def setUp(self):
        self.client = Client()

    def test_login_without_domain_or_phone_rerenders_not_500(self):
        response = self.client.post(
            reverse("login"),
            {"username": "someone", "password": "hunter2"},
        )
        # Previously fell off the end of form_valid returning None -> 500.
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "login.html")
        self.assertTrue(response.context["need_phone_or_domain"])
        self.assertTrue(response.context["invalid"])


class VerifyEmailViewMissingExtraPropsTest(TestCase):
    """GET the legacy verify-email link for a user lacking ExtraUserProperties."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="verifyme", password="hunter2", is_active=False
        )
        # Intentionally do NOT create an ExtraUserProperties row.

    def _verify_url(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        return reverse("verify_email_legacy", kwargs={"uidb64": uidb64, "token": token})

    def test_verify_email_without_extra_props_activates_not_500(self):
        self.assertFalse(ExtraUserProperties.objects.filter(user=self.user).exists())
        response = self.client.get(self._verify_url(self.user))
        # Previously raised ExtraUserProperties.DoesNotExist -> 500.
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        props = ExtraUserProperties.objects.get(user=self.user)
        self.assertTrue(props.email_verified)

    def test_verify_email_invalid_token_returns_invalid_message(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        url = reverse(
            "verify_email_legacy",
            kwargs={"uidb64": uidb64, "token": "not-a-valid-token"},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Activation link is invalid", response.content)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

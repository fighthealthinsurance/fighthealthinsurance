"""Tests for login redirect URL validation (open redirect prevention)."""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model

from fhi_users.models import UserDomain, ExtraUserProperties

User = get_user_model()


class LoginRedirectTests(TestCase):
    """Test that the login view validates the 'next' parameter."""

    def setUp(self):
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
        self.password = "SecureP@ss123"
        self.user = User.objects.create_user(
            username=f"loginuser\U0001f43c{self.domain.id}",
            password=self.password,
            email="login@example.com",
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)
        self.login_url = reverse("login")
        self.valid_credentials = {
            "username": "loginuser",
            "password": self.password,
            "domain": "testdomain",
            "phone": "",
        }

    def _login_with_next(self, next_url, via_get=False):
        """POST valid credentials with a given 'next' value."""
        data = dict(self.valid_credentials)
        if via_get:
            # next in query string
            url = f"{self.login_url}?next={next_url}"
            return self.client.post(url, data)
        else:
            # next in POST body
            data["next"] = next_url
            return self.client.post(self.login_url, data)

    # -- safe redirects should be honoured --------------------------------

    def test_safe_relative_path_redirect(self):
        resp = self._login_with_next("/dashboard/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/dashboard/")

    def test_safe_relative_path_via_query_string(self):
        resp = self._login_with_next("/settings/profile", via_get=True)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/settings/profile")

    # -- open redirect attempts must be blocked ----------------------------

    def test_blocks_absolute_url_to_external_host(self):
        resp = self._login_with_next("https://attacker.example/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("attacker.example", resp["Location"])

    def test_blocks_protocol_relative_url(self):
        resp = self._login_with_next("//evil.com/path")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.com", resp["Location"])

    def test_blocks_javascript_scheme(self):
        resp = self._login_with_next("javascript:alert(1)")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("javascript", resp["Location"])

    def test_blocks_data_scheme(self):
        resp = self._login_with_next("data:text/html,<h1>pwned</h1>")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("data:", resp["Location"])

    def test_blocks_backslash_trick(self):
        # Some browsers treat backslash as forward slash
        resp = self._login_with_next("https:\\\\evil.com")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.com", resp["Location"])

    def test_blocks_external_via_query_string(self):
        resp = self._login_with_next("https://attacker.example", via_get=True)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("attacker.example", resp["Location"])

    # -- no next parameter = default redirect to root ----------------------

    def test_no_next_redirects_to_root(self):
        resp = self.client.post(self.login_url, self.valid_credentials)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("root"))

    # -- template passes next through hidden field -------------------------

    def test_get_passes_next_to_template(self):
        resp = self.client.get(f"{self.login_url}?next=/foo/")
        self.assertContains(resp, 'name="next"')
        self.assertContains(resp, 'value="/foo/"')

"""Tests for the Permissions-Policy header."""

from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from fighthealthinsurance.middleware import PermissionsPolicyMiddleware


def _ok(_request):
    return HttpResponse("ok")


class PermissionsPolicyHeaderTest(TestCase):
    def test_page_response_carries_the_policy(self):
        response = self.client.get("/")
        policy = response["Permissions-Policy"]
        for directive in (
            "camera=()",
            "microphone=()",
            "geolocation=()",
            "payment=()",
            "usb=()",
        ):
            self.assertIn(directive, policy)

    def test_not_found_response_carries_the_policy(self):
        response = self.client.get("/no-such-page-anywhere/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Permissions-Policy", response)

    def test_passkeys_and_clipboard_are_not_turned_off(self):
        policy = self.client.get("/")["Permissions-Policy"]
        self.assertNotIn("publickey-credentials", policy)
        self.assertNotIn("clipboard", policy)

    @override_settings(
        DOMAIN_REDIRECTS={"fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_domain_redirect_carries_the_policy(self):
        response = self.client.get("/", HTTP_HOST="fuckhealthinsurance.com")
        self.assertEqual(response.status_code, 301)
        self.assertIn("Permissions-Policy", response)

    def test_header_set_by_a_view_is_kept(self):
        def view(_request):
            response = HttpResponse("ok")
            response["Permissions-Policy"] = "camera=(self)"
            return response

        response = PermissionsPolicyMiddleware(view)(RequestFactory().get("/"))
        self.assertEqual(response["Permissions-Policy"], "camera=(self)")

    @override_settings(PERMISSIONS_POLICY="")
    def test_empty_setting_sends_no_header(self):
        response = PermissionsPolicyMiddleware(_ok)(RequestFactory().get("/"))
        self.assertNotIn("Permissions-Policy", response)

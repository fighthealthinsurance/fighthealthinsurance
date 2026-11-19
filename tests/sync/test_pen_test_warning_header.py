"""Tests for the X-Warning header sent to unauthorized pen testers."""

from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from fighthealthinsurance.middleware import PenTestWarningMiddleware
from fighthealthinsurance.middleware.PenTestWarningMiddleware import WARNING_TEXT

SECRET = "not-a-real-secret"


def _ok(_request):
    return HttpResponse("ok")


@override_settings(EVIL_SECRET=SECRET)
class PenTestWarningHeaderTest(TestCase):
    def test_page_response_carries_the_warning(self):
        response = self.client.get("/")
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_warning_text_is_the_agreed_wording(self):
        self.assertEqual(WARNING_TEXT, "This is not an authorized pen test.")

    def test_not_found_response_carries_the_warning(self):
        response = self.client.get("/no-such-page-anywhere/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_rest_api_response_carries_the_warning(self):
        response = self.client.get("/ziggy/rest/no-such-endpoint/")
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    @override_settings(
        DOMAIN_REDIRECTS={"fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_domain_redirect_carries_the_warning(self):
        response = self.client.get("/", HTTP_HOST="fuckhealthinsurance.com")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_blocked_metrics_response_carries_the_warning(self):
        response = self.client.get(
            "/metrics", REMOTE_ADDR="203.0.113.5", HTTP_X_FORWARDED_FOR="203.0.113.5"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_matching_evil_header_gets_no_warning(self):
        response = self.client.get("/", headers={"X-Evil": SECRET})
        self.assertNotIn("X-Warning", response)

    def test_wrong_evil_header_still_gets_the_warning(self):
        response = self.client.get("/", headers={"X-Evil": SECRET + "x"})
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_empty_evil_header_still_gets_the_warning(self):
        response = self.client.get("/", headers={"X-Evil": ""})
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    @override_settings(EVIL_SECRET="")
    def test_no_configured_secret_warns_even_with_an_empty_evil_header(self):
        request = RequestFactory().get("/", headers={"X-Evil": ""})
        response = PenTestWarningMiddleware(_ok)(request)
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    @override_settings(EVIL_SECRET="")
    def test_no_configured_secret_warns_whatever_the_evil_header_says(self):
        request = RequestFactory().get("/", headers={"X-Evil": "anything"})
        response = PenTestWarningMiddleware(_ok)(request)
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

    def test_warning_replaces_a_value_set_by_a_view(self):
        def view(_request):
            response = HttpResponse("ok")
            response["X-Warning"] = "all clear"
            return response

        response = PenTestWarningMiddleware(view)(RequestFactory().get("/"))
        self.assertEqual(response["X-Warning"], WARNING_TEXT)

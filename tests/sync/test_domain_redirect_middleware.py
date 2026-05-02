"""Tests for DomainRedirectMiddleware."""

from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from fighthealthinsurance.middleware import DomainRedirectMiddleware


def _ok(_request):
    return HttpResponse("ok")


class DomainRedirectMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(
        DOMAIN_REDIRECTS={
            "fuckhealthinsurance.com": "www.fighthealthinsurance.com",
            "www.fuckhealthinsurance.com": "www.fighthealthinsurance.com",
        }
    )
    def test_redirects_alt_host_to_canonical_host(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.get(
            "/some/path?x=1", HTTP_HOST="fuckhealthinsurance.com"
        )
        response = middleware(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "https://www.fighthealthinsurance.com/some/path?x=1",
        )

    @override_settings(
        DOMAIN_REDIRECTS={"www.fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_redirect_is_case_insensitive(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.get("/", HTTP_HOST="WWW.FuckHealthInsurance.COM")
        response = middleware(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://www.fighthealthinsurance.com/")

    @override_settings(
        DOMAIN_REDIRECTS={"fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_canonical_host_is_passed_through(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.get("/", HTTP_HOST="www.fighthealthinsurance.com")
        response = middleware(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    @override_settings(DOMAIN_REDIRECTS={})
    def test_empty_map_is_noop(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.get("/", HTTP_HOST="fuckhealthinsurance.com")
        response = middleware(request)
        self.assertEqual(response.status_code, 200)

    @override_settings(
        DOMAIN_REDIRECTS={"fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_host_with_port_is_matched(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.get("/", HTTP_HOST="fuckhealthinsurance.com:8080")
        response = middleware(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://www.fighthealthinsurance.com/")

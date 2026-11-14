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

    @override_settings(
        DOMAIN_REDIRECTS={"fuckhealthinsurance.com": "www.fighthealthinsurance.com"}
    )
    def test_post_uses_method_preserving_redirect(self):
        middleware = DomainRedirectMiddleware(_ok)
        request = self.factory.post(
            "/submit", data={"a": "b"}, HTTP_HOST="fuckhealthinsurance.com"
        )
        response = middleware(request)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response["Location"], "https://www.fighthealthinsurance.com/submit"
        )


_BARE = {"fighthealthinsurance.com": "www.fighthealthinsurance.com"}


@override_settings(
    DOMAIN_REDIRECTS={
        **_BARE,
        "fuckhealthinsurance.com": "www.fighthealthinsurance.com",
    },
    DOMAIN_REDIRECTS_SAFE_METHODS_ONLY={"FightHealthInsurance.com"},
)
class SafeMethodsOnlyRedirectTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = DomainRedirectMiddleware(_ok)

    def test_get_on_safe_methods_only_host_redirects_keeping_path_and_query(self):
        request = self.factory.get(
            "/blog/post/?utm_source=x", HTTP_HOST="fighthealthinsurance.com"
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "https://www.fighthealthinsurance.com/blog/post/?utm_source=x",
        )

    def test_head_on_safe_methods_only_host_redirects(self):
        request = self.factory.head("/", HTTP_HOST="fighthealthinsurance.com")
        self.assertEqual(self.middleware(request).status_code, 301)

    def test_post_on_safe_methods_only_host_is_served_in_place(self):
        request = self.factory.post(
            "/webhook", data={"a": "b"}, HTTP_HOST="fighthealthinsurance.com"
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_options_on_safe_methods_only_host_is_served_in_place(self):
        request = self.factory.options("/", HTTP_HOST="fighthealthinsurance.com")
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_other_hosts_still_redirect_post_with_308(self):
        request = self.factory.post("/submit", HTTP_HOST="fuckhealthinsurance.com")
        self.assertEqual(self.middleware(request).status_code, 308)

    def test_canonical_www_host_is_served(self):
        request = self.factory.get("/", HTTP_HOST="www.fighthealthinsurance.com")
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_pod_ip_host_is_served(self):
        request = self.factory.get("/metrics", HTTP_HOST="10.1.2.3")
        self.assertEqual(self.middleware(request).status_code, 200)


class ProdBareDomainSettingsTest(TestCase):
    def test_prod_redirects_bare_domain_to_www_for_get_and_head_only(self):
        from fighthealthinsurance.settings import Prod

        self.assertEqual(
            Prod.DOMAIN_REDIRECTS["fighthealthinsurance.com"],
            "www.fighthealthinsurance.com",
        )
        self.assertEqual(
            Prod.DOMAIN_REDIRECTS_SAFE_METHODS_ONLY, {"fighthealthinsurance.com"}
        )

    def test_prod_leaves_fightpaperwork_and_api_hosts_alone(self):
        from fighthealthinsurance.settings import Prod

        for host in (
            "www.fighthealthinsurance.com",
            "api.fighthealthinsurance.com",
            "monitor.fighthealthinsurance.com",
            "fightpaperwork.com",
            "www.fightpaperwork.com",
        ):
            self.assertNotIn(host, Prod.DOMAIN_REDIRECTS)

    def test_prod_keeps_bare_domain_allowed_and_csrf_trusted(self):
        from fighthealthinsurance.settings import Prod

        self.assertIn("fighthealthinsurance.com", Prod.ALLOWED_HOSTS)
        self.assertIn("https://fighthealthinsurance.com", Prod.CSRF_TRUSTED_ORIGINS)

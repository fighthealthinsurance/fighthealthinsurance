"""
Tests for the no-login "Start Your Appeal" entry funnel landing page.

Covers:
1. The page renders 200 with no login / no account.
2. The primary CTA links into the existing scan flow.
3. Scan's already-supported deep-link params pass through to the CTA.
4. JSON-LD structured data (WebPage + BreadcrumbList + HowTo) is present.
5. A canonical URL is present.
"""

from urllib.parse import urlparse, parse_qs

from django.test import TestCase, Client
from django.urls import reverse


class StartAppealPageRenderTest(TestCase):
    """The landing page is public and renders without any account."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("start_appeal")

    def test_page_renders_200_without_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "start_appeal.html")

    def test_page_has_value_proposition(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Start Your Appeal Free")
        self.assertContains(response, "No account needed")

    def test_page_includes_not_legal_advice_disclaimer(self):
        response = self.client.get(self.url)
        self.assertContains(response, "do not constitute legal, medical, or financial advice")


class StartAppealCTATest(TestCase):
    """The primary CTA funnels visitors into the existing scan flow."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("start_appeal")

    def test_cta_links_to_scan(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        scan_path = reverse("scan")
        # The rendered CTA href resolves to the scan route.
        self.assertContains(response, f'href="{scan_path}"')
        self.assertEqual(response.context["scan_url"], scan_path)

    def test_page_also_offers_chat_entry(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["chat_url"], reverse("chat"))


class StartAppealDeepLinkPassthroughTest(TestCase):
    """Scan's already-supported deep-link params pass through to the CTA."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("start_appeal")

    def test_supported_params_pass_through_to_scan_url(self):
        params = {
            "default_procedure": "MRI Scan",
            "default_condition": "Back Pain",
            "microsite_slug": "mri-denial",
            "microsite_title": "Appealing MRI Denials",
        }
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)

        scan_url = response.context["scan_url"]
        parsed = urlparse(scan_url)
        self.assertEqual(parsed.path, reverse("scan"))
        query = parse_qs(parsed.query)
        self.assertEqual(query["default_procedure"], ["MRI Scan"])
        self.assertEqual(query["default_condition"], ["Back Pain"])
        self.assertEqual(query["microsite_slug"], ["mri-denial"])
        self.assertEqual(query["microsite_title"], ["Appealing MRI Denials"])

    def test_unsupported_params_are_not_forwarded(self):
        response = self.client.get(
            self.url,
            {"default_procedure": "MRI Scan", "evil": "javascript"},
        )
        scan_url = response.context["scan_url"]
        query = parse_qs(urlparse(scan_url).query)
        self.assertIn("default_procedure", query)
        self.assertNotIn("evil", query)

    def test_no_params_yields_plain_scan_url(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["scan_url"], reverse("scan"))
        self.assertFalse(response.context["has_prefill"])

    def test_prefill_params_appear_in_rendered_href(self):
        response = self.client.get(self.url, {"default_procedure": "MRI Scan"})
        # urlencode encodes the space as a plus sign in the href.
        self.assertContains(response, "default_procedure=MRI+Scan")


class StartAppealSEOTest(TestCase):
    """SEO signals: JSON-LD structured data and a canonical URL."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("start_appeal")

    def test_json_ld_present(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'application/ld+json')

    def test_json_ld_includes_expected_schema_types(self):
        response = self.client.get(self.url)
        self.assertContains(response, '"WebPage"')
        self.assertContains(response, '"BreadcrumbList"')
        self.assertContains(response, '"HowTo"')

    def test_canonical_url_present(self):
        response = self.client.get(self.url)
        self.assertContains(response, '<link rel="canonical"')
        # Canonical strips query params even when deep-link params are supplied.
        response_with_params = self.client.get(
            self.url, {"default_procedure": "MRI Scan"}
        )
        self.assertContains(
            response_with_params,
            'href="https://www.fighthealthinsurance.com/start-appeal/"',
        )

    def test_meta_description_present(self):
        response = self.client.get(self.url)
        self.assertContains(response, '<meta name="description"')


class StartAppealSitemapTest(TestCase):
    """The landing page is discoverable via the sitemap."""

    def test_start_appeal_in_sitemap(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertIn("start_appeal", StaticViewSitemap().items())

"""
Unit tests for the brand system (middleware, context processors, and brand detection).
"""

from django.test import RequestFactory, TestCase

from fighthealthinsurance.brand_config import (
    get_brand_config,
    get_brand_from_domain,
    get_brand_from_path,
)
from fighthealthinsurance.middleware.BrandMiddleware import BrandMiddleware


class BrandDetectionTest(TestCase):
    """Tests for brand detection functions."""

    def test_domain_detection_fhi(self):
        """Test that FHI domains are correctly detected."""
        self.assertEqual(get_brand_from_domain("fighthealthinsurance.com"), "fhi")
        self.assertEqual(get_brand_from_domain("www.fighthealthinsurance.com"), "fhi")
        self.assertEqual(get_brand_from_domain("FIGHTHEALTHINSURANCE.COM"), "fhi")
        self.assertEqual(get_brand_from_domain("fightpaperwork.com"), "fhi")
        self.assertEqual(get_brand_from_domain("localhost"), "fhi")

    def test_domain_detection_amc(self):
        """Test that AMC domains are correctly detected."""
        self.assertEqual(get_brand_from_domain("appealmyclaims.com"), "amc")
        self.assertEqual(get_brand_from_domain("www.appealmyclaims.com"), "amc")
        self.assertEqual(get_brand_from_domain("APPEALMYCLAIMS.COM"), "amc")

    def test_path_detection_amc(self):
        """Test that /appealmyclaims/ path prefix is detected."""
        self.assertEqual(get_brand_from_path("/appealmyclaims"), "amc")
        self.assertEqual(get_brand_from_path("/appealmyclaims/"), "amc")
        self.assertEqual(get_brand_from_path("/appealmyclaims/scan"), "amc")
        self.assertEqual(get_brand_from_path("/appealmyclaims/chat"), "amc")

    def test_path_detection_no_match(self):
        """Test that non-AMC paths return None."""
        self.assertIsNone(get_brand_from_path("/"))
        self.assertIsNone(get_brand_from_path("/scan"))
        self.assertIsNone(get_brand_from_path("/chat"))
        self.assertIsNone(get_brand_from_path("/appeal"))

    def test_get_brand_config_fhi(self):
        """Test getting FHI brand configuration."""
        brand = get_brand_config("fhi")
        self.assertEqual(brand.slug, "fhi")
        self.assertEqual(brand.name, "Fight Health Insurance")
        self.assertEqual(brand.primary_color, "#a5c422")
        self.assertTrue(brand.show_full_nav)
        self.assertIsNotNone(brand.logo_url)
        self.assertIsNone(brand.logo_text)

    def test_get_brand_config_amc(self):
        """Test getting AMC brand configuration."""
        brand = get_brand_config("amc")
        self.assertEqual(brand.slug, "amc")
        self.assertEqual(brand.name, "Appeal My Claims")
        self.assertEqual(brand.primary_color, "#1976d2")  # Material UI blue
        self.assertFalse(brand.show_full_nav)
        self.assertIsNone(brand.logo_url)
        self.assertEqual(brand.logo_text, "Appeal My Claims")

    def test_get_brand_config_defaults_to_fhi(self):
        """Test that invalid brand slugs default to FHI."""
        brand = get_brand_config("invalid_slug")
        self.assertEqual(brand.slug, "fhi")

    def test_amc_footer_text_has_powered_by_fhi(self):
        """Test that AMC brand has 'Powered by FHI' in footer."""
        brand = get_brand_config("amc")
        self.assertIn("Fight Health Insurance", brand.footer_text)
        self.assertIn("Powered by", brand.footer_text)


class BrandMiddlewareTest(TestCase):
    """Tests for the BrandMiddleware."""

    def setUp(self):
        """Set up test fixtures."""
        self.factory = RequestFactory()
        self.middleware = BrandMiddleware(lambda r: None)

    def test_middleware_attaches_brand_from_domain_fhi(self):
        """Test that middleware attaches FHI brand from domain."""
        request = self.factory.get("/", HTTP_HOST="fighthealthinsurance.com")
        self.middleware.process_request(request)

        self.assertEqual(request.brand_slug, "fhi")
        self.assertEqual(request.brand.name, "Fight Health Insurance")

    def test_middleware_attaches_brand_from_domain_amc(self):
        """Test that middleware attaches AMC brand from domain."""
        request = self.factory.get("/", HTTP_HOST="appealmyclaims.com")
        self.middleware.process_request(request)

        self.assertEqual(request.brand_slug, "amc")
        self.assertEqual(request.brand.name, "Appeal My Claims")

    def test_middleware_path_overrides_domain(self):
        """Test that path prefix takes priority over domain."""
        # FHI domain with AMC path should result in AMC brand
        request = self.factory.get(
            "/appealmyclaims/", HTTP_HOST="fighthealthinsurance.com"
        )
        self.middleware.process_request(request)

        self.assertEqual(request.brand_slug, "amc")

    def test_middleware_handles_port_in_host(self):
        """Test that middleware strips port from hostname."""
        request = self.factory.get("/", HTTP_HOST="appealmyclaims.com:8000")
        self.middleware.process_request(request)

        self.assertEqual(request.brand_slug, "amc")


class BrandContextProcessorTest(TestCase):
    """Tests for brand context processor."""

    def test_brand_context_in_templates_fhi(self):
        """Test that FHI brand context is available in templates."""
        response = self.client.get("/")

        self.assertIn("brand", response.context)
        self.assertEqual(response.context["brand"].slug, "fhi")
        self.assertTrue(response.context["is_fhi"])
        self.assertFalse(response.context["is_amc"])

    def test_brand_context_in_templates_amc(self):
        """Test that AMC brand context is available in templates via path."""
        response = self.client.get("/appealmyclaims/")

        self.assertIn("brand", response.context)
        self.assertEqual(response.context["brand"].slug, "amc")
        self.assertFalse(response.context["is_fhi"])
        self.assertTrue(response.context["is_amc"])


class CanonicalURLTest(TestCase):
    """Tests for canonical URL context processor with brand awareness."""

    def test_canonical_url_fhi(self):
        """Test that canonical URLs point to FHI domain for FHI brand."""
        response = self.client.get("/")

        self.assertIn("canonical_url", response.context)
        self.assertTrue(
            response.context["canonical_url"].startswith(
                "https://www.fighthealthinsurance.com"
            )
        )

    def test_canonical_url_amc_strips_prefix(self):
        """Test that canonical URLs strip /appealmyclaims/ prefix for AMC."""
        response = self.client.get("/appealmyclaims/")

        self.assertIn("canonical_url", response.context)
        canonical = response.context["canonical_url"]
        # Should point to AMC domain root, not /appealmyclaims/
        self.assertEqual(canonical, "https://www.appealmyclaims.com/")


class BrandRoutingTest(TestCase):
    """Tests for URL routing with brand paths."""

    def test_root_path_accessible(self):
        """Test that root path is accessible and shows FHI brand."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "fhi")

    def test_appealmyclaims_path_accessible(self):
        """Test that /appealmyclaims/ path is accessible and shows AMC brand."""
        response = self.client.get("/appealmyclaims/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "amc")

    def test_appealmyclaims_scan_redirects_to_wizard(self):
        """Test that /appealmyclaims/scan redirects to /appealmyclaims/ (wizard)."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/appealmyclaims/")

    def test_alt_root_redirects_to_appealmyclaims(self):
        """Test that /alt/ redirects to /appealmyclaims/."""
        response = self.client.get("/alt/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/appealmyclaims/")

    def test_alt_scan_redirects_to_appealmyclaims_scan(self):
        """Test that /alt/scan redirects to /appealmyclaims/scan."""
        response = self.client.get("/alt/scan")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/appealmyclaims/scan")

    def test_fhi_scan_uses_standard_template(self):
        """Test that /scan still uses the standard FHI template."""
        response = self.client.get("/scan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "fhi")
        # FHI scan should NOT have AMC wizard root
        self.assertNotContains(response, "amc-wizard-root")


class BrandAwareURLTest(TestCase):
    """Tests for brand-aware URL resolution in templates."""

    def test_fhi_privacy_policy_url(self):
        """Test that FHI pages link to FHI privacy policy."""
        response = self.client.get("/scan")
        self.assertIn("brand_url_names", response.context)
        self.assertEqual(
            response.context["brand_url_names"]["privacy_policy"], "privacy_policy"
        )
        self.assertEqual(response.context["brand_url_names"]["tos"], "tos")
        # Verify the page contains the correct URL
        self.assertContains(response, 'href="/privacy_policy"')
        self.assertContains(response, 'href="/tos"')

    def test_required_policies_are_mandatory_fhi(self):
        """Test that privacy policy and TOS agreements are required in FHI scan."""
        response_fhi = self.client.get("/scan")
        self.assertContains(response_fhi, 'id="privacy"')
        self.assertContains(response_fhi, 'id="tos"')
        self.assertContains(response_fhi, 'id="personalonly"')
        self.assertContains(response_fhi, 'id="pii"')


class AMCWizardTest(TestCase):
    """Tests for the AMC React wizard flow."""

    def test_amc_wizard_loads(self):
        """Test that /appealmyclaims/ loads the React wizard template."""
        response = self.client.get("/appealmyclaims/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="amc-wizard-root"')
        self.assertContains(response, "amc_wizard.bundle.js")

    def test_amc_wizard_has_brand_context(self):
        """Test that wizard has correct AMC brand context."""
        response = self.client.get("/appealmyclaims/")
        self.assertEqual(response.context["brand"].slug, "amc")
        self.assertTrue(response.context["is_amc"])

    def test_amc_wizard_passes_config_data(self):
        """Test that wizard template passes required config data attributes."""
        response = self.client.get("/appealmyclaims/")
        self.assertContains(response, "data-csrf-token")
        self.assertContains(response, "data-ws-host")
        self.assertContains(response, "data-privacy-url")
        self.assertContains(response, "data-tos-url")

    def test_amc_wizard_loads_manrope_font(self):
        """Test that AMC wizard loads the Manrope font."""
        response = self.client.get("/appealmyclaims/")
        self.assertContains(response, "Manrope")

    def test_fhi_scan_unchanged(self):
        """Test that FHI scan page still uses the standard template."""
        response = self.client.get("/scan")
        self.assertEqual(response.status_code, 200)
        # FHI should NOT have AMC wizard
        self.assertNotContains(response, "amc-wizard-root")
        self.assertNotContains(response, "amc_wizard.bundle.js")
        # FHI should still have its standard elements
        self.assertContains(response, 'id="submit"')
        self.assertContains(response, 'name="denial_text"')
        self.assertContains(response, 'id="email"')

    def test_amc_about_page_still_works(self):
        """Test that AMC branded static pages still work."""
        response = self.client.get("/appealmyclaims/about-us")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "amc")

    def test_amc_privacy_policy_still_works(self):
        """Test that AMC branded privacy policy still works."""
        response = self.client.get("/appealmyclaims/privacy_policy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "amc")


class AnonymousDenialAPITest(TestCase):
    """Tests for the anonymous denial creation REST endpoint."""

    def test_create_denial_requires_email(self):
        """Test that email is required for denial creation."""
        response = self.client.post(
            "/ziggy/rest/amc-denials/",
            data={
                "denial_text": "My claim was denied.",
                "pii": True,
                "tos": True,
                "privacy": True,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_denial_requires_denial_text(self):
        """Test that denial_text is required for denial creation."""
        response = self.client.post(
            "/ziggy/rest/amc-denials/",
            data={
                "email": "test@example.com",
                "pii": True,
                "tos": True,
                "privacy": True,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_denial_success(self):
        """Test successful anonymous denial creation."""
        response = self.client.post(
            "/ziggy/rest/amc-denials/",
            data={
                "email": "test@example.com",
                "denial_text": "Insurance Company: Aetna\nDenial Reason: Not medically necessary\nService Denied: MRI scan\nThe insurance company denied coverage for MRI scan stating: Not medically necessary.",
                "zip": "94105",
                "pii": True,
                "tos": True,
                "privacy": True,
                "insurance_company": "Aetna",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("denial_id", data)
        self.assertIn("semi_sekret", data)

    def test_create_denial_is_anonymous(self):
        """Test that no authentication is required."""
        # This test verifies the endpoint works without auth headers
        response = self.client.post(
            "/ziggy/rest/amc-denials/",
            data={
                "email": "anon@example.com",
                "denial_text": "My claim was denied for being out of network.",
                "zip": "",
                "pii": True,
                "tos": True,
                "privacy": True,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)

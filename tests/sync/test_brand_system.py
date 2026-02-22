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

    def test_appealmyclaims_scan_accessible(self):
        """Test that /appealmyclaims/scan uses AMC brand and AMC template."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["brand"].slug, "amc")

    def test_appealmyclaims_scan_uses_amc_template(self):
        """Test that /appealmyclaims/scan uses the AMC-styled template."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertEqual(response.status_code, 200)
        # Check for AMC-specific content: stepper and Material UI styling
        self.assertContains(response, "Upload Your Denial")
        self.assertContains(response, "amc-stepper")
        self.assertContains(response, "#1976d2")  # MUI primary blue
        self.assertContains(response, "Manrope")  # AMC font

    def test_appealmyclaims_scan_form_action(self):
        """Test that AMC scan form posts to the AMC scan endpoint."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, 'action="/appealmyclaims/scan"')

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
        # FHI scan should NOT have AMC stepper
        self.assertNotContains(response, "amc-stepper")


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

    def test_amc_privacy_policy_url(self):
        """Test that AMC pages link to AMC privacy policy."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertIn("brand_url_names", response.context)
        self.assertEqual(
            response.context["brand_url_names"]["privacy_policy"], "amc_privacy_policy"
        )
        self.assertEqual(response.context["brand_url_names"]["tos"], "amc_tos")
        # Verify the page contains the correct URL
        self.assertContains(response, 'href="/appealmyclaims/privacy_policy"')
        self.assertContains(response, 'href="/appealmyclaims/tos"')

    def test_amc_brand_home_link_points_to_amc_root(self):
        """Test that AMC pages use AMC root for the brand home/logo link."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, 'id="brand-home-link"')
        self.assertContains(response, 'id="brand-home-link" href="/appealmyclaims/"')

    def test_amc_consent_uses_fhi_policies(self):
        """Test that AMC consent checkboxes reference FHI policies (same policies for both brands)."""
        response = self.client.get("/appealmyclaims/scan")
        # Both brands require agreement to privacy policy and TOS
        self.assertContains(response, "I have read and understand the")
        self.assertContains(response, "privacy policy")
        self.assertContains(response, "I agree to the")
        self.assertContains(response, "terms of service")

    def test_mailing_list_subscription_is_optional(self):
        """Test that mailing list subscription is optional (not required)."""
        # FHI version
        response_fhi = self.client.get("/scan")
        # Check that subscribe checkbox is present
        self.assertContains(response_fhi, 'id="subscribe"')
        self.assertContains(response_fhi, 'name="subscribe"')
        # Verify it's in the "Optional" section
        self.assertContains(response_fhi, "<b>Optional</b>")
        self.assertContains(response_fhi, "Subscribe to our mailing list")

        # AMC version
        response_amc = self.client.get("/appealmyclaims/scan")
        # Check that subscribe checkbox is present
        self.assertContains(response_amc, 'id="subscribe"')
        self.assertContains(response_amc, 'name="subscribe"')
        # Verify it's in the optional section
        self.assertContains(response_amc, "Optional Settings")

    def test_required_policies_are_mandatory(self):
        """Test that privacy policy and TOS agreements are required."""
        # FHI version
        response_fhi = self.client.get("/scan")
        self.assertContains(response_fhi, 'id="privacy"')
        self.assertContains(response_fhi, 'id="tos"')
        self.assertContains(response_fhi, 'id="personalonly"')
        self.assertContains(response_fhi, 'id="pii"')

        # AMC version
        response_amc = self.client.get("/appealmyclaims/scan")
        self.assertContains(response_amc, 'id="privacy"')
        self.assertContains(response_amc, 'id="tos"')
        self.assertContains(response_amc, 'id="personalonly"')
        self.assertContains(response_amc, 'id="pii"')


class AMCFlowTest(TestCase):
    """Tests specific to the AMC-styled flow."""

    def test_amc_landing_page_content(self):
        """Test that AMC landing page has expected content."""
        response = self.client.get("/appealmyclaims/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Healthcare Appeal Helper")
        self.assertContains(response, "Start Your Appeal")

    def test_amc_scan_has_all_required_form_fields(self):
        """Test that AMC scan has all form fields needed for backend processing."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertEqual(response.status_code, 200)
        # Contact fields
        self.assertContains(response, 'id="store_fname"')
        self.assertContains(response, 'id="store_lname"')
        self.assertContains(response, 'id="email"')
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'id="store_street"')
        self.assertContains(response, 'id="store_zip"')
        # Denial fields
        self.assertContains(response, 'name="denial_text"')
        self.assertContains(response, 'id="denial_text"')
        self.assertContains(response, 'id="uploader"')
        # Scrub button
        self.assertContains(response, 'id="scrub-2"')
        # Submit
        self.assertContains(response, 'id="submit"')

    def test_amc_scan_has_error_divs(self):
        """Test that AMC scan template has all required error message divs."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, 'id="email_error"')
        self.assertContains(response, 'id="agree_chk_error"')
        self.assertContains(response, 'id="pii_error"')
        self.assertContains(response, 'id="need_denial"')

    def test_amc_scan_has_stepper(self):
        """Test that AMC scan shows the step indicator."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, "amc-stepper")
        self.assertContains(response, "Upload Denial")
        self.assertContains(response, "Review &amp; Customize")
        self.assertContains(response, "Send Appeal")

    def test_amc_scan_has_powered_by_fhi(self):
        """Test that AMC scan page shows 'Powered by Fight Health Insurance'."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, "Powered by")
        self.assertContains(response, "Fight Health Insurance")

    def test_amc_scan_has_csrf_token(self):
        """Test that AMC scan form includes CSRF token."""
        response = self.client.get("/appealmyclaims/scan")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_fhi_scan_unchanged(self):
        """Test that FHI scan page still uses the standard template."""
        response = self.client.get("/scan")
        self.assertEqual(response.status_code, 200)
        # FHI should NOT have AMC stepper or Material UI colors
        self.assertNotContains(response, "amc-stepper")
        self.assertNotContains(response, "Manrope")
        # FHI should still have its standard elements
        self.assertContains(response, 'id="submit"')
        self.assertContains(response, 'name="denial_text"')
        self.assertContains(response, 'id="email"')

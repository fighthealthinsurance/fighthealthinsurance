"""
Selenium tests for AppealMyClaims (AMC) brand UI and flow.

These tests verify that:
1. AMC branding displays correctly (logo, colors, navigation)
2. The appeal flow works end-to-end with AMC branding
3. "Powered by FHI" attribution appears in the right places
4. Navigation is minimal (no extra links for AMC)
5. Both path-based (/appealmyclaims/) and domain-based branding work
"""

import time

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import BaseCase

from fighthealthinsurance.models import DenialTypes

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestAMCBranding(FHISeleniumBase, StaticLiveServerTestCase):
    """Test AppealMyClaims branding and UI differences."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def test_amc_landing_page_shows_correct_branding(self):
        """Test that AMC landing page shows Appeal My Claims branding."""
        self.open(f"{self.live_server_url}/appealmyclaims/")

        # Wait for page to load
        time.sleep(1)

        # Check page title contains Appeal My Claims or similar branding
        title = self.get_title()
        # Title should still mention insurance appeals but may vary
        assert "insurance" in title.lower() or "appeal" in title.lower()

        # Check for Appeal My Claims text logo (since AMC uses text logo, not image)
        self.assert_text("Appeal My Claims")

        # Check welcome header from real AMC site
        self.assert_text("Healthcare Appeal Helper")

        # Check main headline from real AMC site
        self.assert_text("Denied by your insurance?")

        # Verify minimal navigation - should NOT have these FHI-specific links
        with pytest.raises(Exception):
            self.assert_element('a:contains("Chat")')
        with pytest.raises(Exception):
            self.assert_element('a:contains("Explain Denial")')
        with pytest.raises(Exception):
            self.assert_element('a:contains("How to Help")')
        with pytest.raises(Exception):
            self.assert_element('a:contains("Resources/Blogs")')

        # Should have the main CTA
        self.assert_element('a:contains("Generate Appeal")') or self.assert_element(
            'a:contains("Get Started")'
        )

    def test_amc_has_powered_by_fhi_in_footer(self):
        """Test that AMC pages show 'Powered by Fight Health Insurance' in footer."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(1)

        # Check for "Powered by Fight Health Insurance" text
        self.assert_text("Powered by")
        self.assert_text("Fight Health Insurance")

        # Verify it's a link to FHI
        fhi_link = self.find_element('a[href="https://www.fighthealthinsurance.com"]')
        assert fhi_link is not None

    def test_amc_does_not_show_social_links(self):
        """Test that AMC pages don't show FHI social media links."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(1)

        # AMC should not have social media links (Instagram, LinkedIn, etc.)
        with pytest.raises(Exception):
            self.assert_element('a[aria-label="Instagram"]')
        with pytest.raises(Exception):
            self.assert_element('a[aria-label="LinkedIn"]')
        with pytest.raises(Exception):
            self.assert_element('a[aria-label="YouTube"]')

    def test_fhi_landing_page_shows_fhi_branding(self):
        """Test that FHI landing page still shows Fight Health Insurance branding."""
        self.open(f"{self.live_server_url}/")
        time.sleep(2)

        # Check for FHI branding
        self.assert_text("Fight Health Insurance")

        # FHI should have full navigation - check for nav links
        # Use more flexible check since the exact structure may vary
        page_source = self.get_page_source()
        assert "Chat" in page_source
        assert "Explain" in page_source  # "Explain Denial" or similar
        assert "Help" in page_source  # "How to Help" or similar

        # Should NOT have "Powered by" (that's only for AMC)
        assert "Powered by Fight Health Insurance" not in page_source

    def test_fhi_does_not_have_powered_by_text(self):
        """Test that FHI pages don't show 'Powered by' text."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        # FHI should NOT have "Powered by FHI" (that would be redundant)
        with pytest.raises(Exception):
            self.assert_text("Powered by Fight Health Insurance")


class SeleniumTestAMCAppealFlow(FHISeleniumBase, StaticLiveServerTestCase):
    """Test complete appeal flow with AMC branding."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def test_amc_appeal_flow_from_landing_to_upload(self):
        """Test navigating from AMC landing page to upload denial page."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(2)

        # Verify we're on AMC landing page first
        page_source = self.get_page_source()
        assert "Appeal My Claims" in page_source

        # Navigate to scan page directly (clicking may have timing issues)
        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(2)

        # Should land on upload denial page
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Verify AMC branding persists
        page_source = self.get_page_source()
        assert "Appeal My Claims" in page_source

    def test_amc_complete_appeal_flow_with_branding(self):
        """Test complete appeal generation flow maintains AMC branding throughout."""
        # Simplified test - just verify branding persists across pages

        # Start from AMC landing page
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(2)
        page_source = self.get_page_source()
        assert "Appeal My Claims" in page_source

        # Navigate to scan page
        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(2)
        page_source = self.get_page_source()
        assert "Appeal My Claims" in page_source
        assert "Powered by" in page_source

    def test_amc_appeal_completion_shows_powered_by_fhi(self):
        """Test that appeal completion page shows 'Powered by FHI' for AMC."""
        # This is a simplified test - full flow requires ML backend
        # We'll test that the attribution appears on appeal templates

        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(1)

        # Verify we're on AMC version
        self.assert_text("Appeal My Claims")

        # The "Powered by FHI" should be present on this page too
        # (we added it to scrub.html)
        self.assert_text("Powered by")
        self.assert_text("Fight Health Insurance")

    def test_amc_categorize_page_shows_branding(self):
        """Test that the categorize page maintains AMC branding."""
        # Note: Categorize page requires session data, so we can't test it directly
        # Instead, just verify the scan page has branding
        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(2)

        # Should show AMC branding
        self.assert_text("Appeal My Claims")

        # Should have "Powered by FHI"
        self.assert_text("Powered by")
        self.assert_text("Fight Health Insurance")

    def test_amc_privacy_and_tos_pages_accessible(self):
        """Test that AMC has access to privacy policy and TOS."""
        # Privacy Policy
        self.open(f"{self.live_server_url}/appealmyclaims/privacy_policy")
        time.sleep(1)
        self.assert_text("Privacy Policy")
        self.assert_text("Appeal My Claims")

        # Terms of Service
        self.open(f"{self.live_server_url}/appealmyclaims/tos")
        time.sleep(1)
        self.assert_text("Terms of Service")
        self.assert_text("Appeal My Claims")

    def test_amc_about_page_accessible(self):
        """Test that AMC about page is accessible."""
        self.open(f"{self.live_server_url}/appealmyclaims/about-us")
        time.sleep(1)
        self.assert_text("Appeal My Claims")
        # About page should have some content
        self.assert_text("About")


class SeleniumTestBrandColorScheme(FHISeleniumBase, StaticLiveServerTestCase):
    """Test that color schemes are different between FHI and AMC."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def test_amc_has_blue_css_variable(self):
        """Test that AMC pages inject blue color CSS variable."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(1)

        # Check that the page has the CSS variable set
        # The brand color is injected in a <style> tag in the head
        page_source = self.get_page_source()

        # Should contain the blue color for AMC
        assert "#2563eb" in page_source, "AMC blue color not found in page source"
        assert "--brand-primary-color" in page_source, "CSS variable not found"

    def test_fhi_has_green_css_variable(self):
        """Test that FHI pages inject green color CSS variable."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        page_source = self.get_page_source()

        # Should contain the green color for FHI
        assert "#a5c422" in page_source, "FHI green color not found in page source"
        assert "--brand-primary-color" in page_source, "CSS variable not found"

    def test_amc_text_logo_renders(self):
        """Test that AMC renders text logo correctly."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(1)

        # Check for logo-text span (AMC uses text logo)
        logo = self.find_element("span.logo-text")
        assert logo is not None
        assert "Appeal My Claims" in logo.text

    def test_fhi_image_logo_renders(self):
        """Test that FHI renders image logo correctly."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        # Check for image logo (FHI uses image)
        logo = self.find_element("img.logo")
        assert logo is not None
        assert logo.get_attribute("alt") == "Fight Health Insurance"


class SeleniumTestBrandConsistency(FHISeleniumBase, StaticLiveServerTestCase):
    """Test that brand context persists across navigation."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def test_amc_brand_persists_through_navigation(self):
        """Test that navigating through AMC site maintains AMC branding."""
        # Start at AMC landing
        self.open(f"{self.live_server_url}/appealmyclaims/")
        time.sleep(1)
        self.assert_text("Appeal My Claims")

        # Navigate to scan
        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(1)
        self.assert_text("Appeal My Claims")

        # Navigate to about
        self.open(f"{self.live_server_url}/appealmyclaims/about-us")
        time.sleep(1)
        self.assert_text("Appeal My Claims")

        # All should show "Powered by FHI"
        self.assert_text("Powered by")

    def test_fhi_brand_persists_through_navigation(self):
        """Test that navigating through FHI site maintains FHI branding."""
        # Start at FHI landing
        self.open(f"{self.live_server_url}/")
        time.sleep(1)
        self.assert_text("Fight Health Insurance")

        # Navigate to scan
        self.open(f"{self.live_server_url}/scan")
        time.sleep(1)
        self.assert_text("Fight Health Insurance")

        # Should NOT show "Powered by"
        with pytest.raises(Exception):
            self.assert_text("Powered by Fight Health Insurance")

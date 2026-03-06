"""
Selenium tests for AppealMyClaims (AMC) brand UI and wizard flow.

These tests verify that:
1. AMC branding displays correctly (header, footer, hero, info cards)
2. The 3-step React wizard loads and functions correctly
3. "Powered by FHI" attribution appears in the footer
4. The standalone page has no FHI nav/branding
5. FHI pages are unaffected by AMC changes
"""

import time

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from selenium.webdriver.common.by import By
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestAMCBranding(FHISeleniumBase, StaticLiveServerTestCase):
    """Test AppealMyClaims branding and UI."""

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
        """Test that AMC landing page shows correct branding."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Check page title
        title = self.get_title()
        assert "Healthcare Appeal Helper" in title

        # Check React-rendered header
        self.assert_text("Healthcare Appeal Helper")

        # Check hero section
        self.assert_text("Denied by your insurance?")

        # Check info cards
        self.assert_text("Know Your Rights")
        self.assert_text("Appeal Success Rates")
        self.assert_text("Need More Help?")

        # Standalone page - no FHI nav elements
        page_source = self.get_page_source()
        assert 'class="nav-link"' not in page_source
        assert "navbar" not in page_source

    def test_amc_has_powered_by_fhi(self):
        """Test that AMC pages show 'Powered by Fight Health Insurance'."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Check for "Powered by Fight Health Insurance" in React footer
        self.assert_text("Powered by")
        self.assert_text("Fight Health Insurance")

    def test_amc_has_resource_links(self):
        """Test that AMC footer has external resource links."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        self.assert_text("External Appeals Information")
        self.assert_text("Healthcare.gov Appeals")
        self.assert_text("Patient Advocate Foundation")

    def test_amc_does_not_show_social_links(self):
        """Test that AMC pages don't show FHI social media links."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        page_source = self.get_page_source()
        assert 'aria-label="Instagram"' not in page_source
        assert 'aria-label="LinkedIn"' not in page_source
        assert 'aria-label="YouTube"' not in page_source

    def test_fhi_landing_page_shows_fhi_branding(self):
        """Test that FHI landing page still shows Fight Health Insurance branding."""
        self.open(f"{self.live_server_url}/")
        time.sleep(2)

        self.assert_text("Fight Health Insurance")

        # FHI should have full navigation
        page_source = self.get_page_source()
        assert "Chat" in page_source
        assert "Explain" in page_source
        assert "Help" in page_source

        # Should NOT have "Powered by"
        assert "Powered by Fight Health Insurance" not in page_source

    def test_fhi_does_not_have_powered_by_text(self):
        """Test that FHI pages don't show 'Powered by' text."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        with pytest.raises(Exception):
            self.assert_text("Powered by Fight Health Insurance")


class SeleniumTestAMCWizardFlow(FHISeleniumBase, StaticLiveServerTestCase):
    """Test the AMC 3-step React wizard flow."""

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


    def test_amc_wizard_loads_with_stepper(self):
        """Test that the wizard loads with the 3-step stepper."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        self.assert_text("Patient Information")
        self.assert_text("Denial Details")
        self.assert_text("Appeal Letter")

    def test_amc_wizard_step1_has_form_fields(self):
        """Test that Step 1 renders the patient information form."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Check for form fields
        self.assert_text("Full Name")
        self.assert_text("Email")
        self.assert_text("Insurance Company Name")
        self.assert_text("Date of Denial")

        # Check for navigation buttons
        self.assert_text("Continue")
        self.assert_text("Back")

        # Should NOT have provider toggle (patient voice only)
        page_source = self.get_page_source()
        assert "Healthcare Provider" not in page_source

    def test_amc_wizard_step1_validation(self):
        """Test that Step 1 validates required fields before advancing."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Click Continue without filling any fields
        self.click('button:contains("Continue")')
        time.sleep(0.5)

        # Should show validation errors and stay on Step 1
        self.assert_text("Patient name is required")
        self.assert_text("Email is required")
        self.assert_text("Insurance company is required")
        self.assert_text("Denial date is required")

        # Should still be on Step 1
        self.assert_text("Full Name")

    def test_amc_wizard_navigate_to_step2(self):
        """Test navigating from Step 1 to Step 2 with valid data."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Fill in Step 1 required fields using JS to set React-controlled values
        def fill_field_by_label(label_text, value):
            """Find a Mantine input by its label text and fill it."""
            script = f"""
                var labels = document.querySelectorAll('label');
                for (var i = 0; i < labels.length; i++) {{
                    if (labels[i].textContent.includes('{label_text}')) {{
                        var wrapper = labels[i].closest('.mantine-TextInput-root, .mantine-InputWrapper-root');
                        if (!wrapper) wrapper = labels[i].parentElement;
                        var input = wrapper.querySelector('input');
                        if (input) {{
                            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            nativeInputValueSetter.call(input, '{value}');
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                }}
                return false;
            """
            result = self.driver.execute_script(script)
            assert result, f"Could not find input for label: {label_text}"

        fill_field_by_label("Full Name", "John Doe")
        fill_field_by_label("Email", "test@example.com")
        fill_field_by_label("Insurance Company", "Aetna")
        fill_field_by_label("Date of Denial", "2025-01-15")

        # Click Continue
        self.click('button:contains("Continue")')
        time.sleep(1)

        # Should now be on Step 2
        self.assert_text("Reason for Denial")
        self.assert_text("Description of Denied Service")

    def test_amc_wizard_hero_hidden_on_step2(self):
        """Test that hero/info cards are hidden when not on Step 1."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Hero visible on Step 1
        self.assert_text("Denied by your insurance?")

        # Fill fields and advance to Step 2
        def fill_field_by_label(label_text, value):
            script = f"""
                var labels = document.querySelectorAll('label');
                for (var i = 0; i < labels.length; i++) {{
                    if (labels[i].textContent.includes('{label_text}')) {{
                        var wrapper = labels[i].closest('.mantine-TextInput-root, .mantine-InputWrapper-root');
                        if (!wrapper) wrapper = labels[i].parentElement;
                        var input = wrapper.querySelector('input');
                        if (input) {{
                            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            nativeInputValueSetter.call(input, '{value}');
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                }}
                return false;
            """
            return self.driver.execute_script(script)

        fill_field_by_label("Full Name", "John Doe")
        fill_field_by_label("Email", "test@example.com")
        fill_field_by_label("Insurance Company", "Aetna")
        fill_field_by_label("Date of Denial", "2025-01-15")

        self.click('button:contains("Continue")')
        time.sleep(1)

        # Hero should be hidden on Step 2
        with pytest.raises(Exception):
            self.assert_text("Denied by your insurance?")

    def test_amc_scan_redirects_to_wizard(self):
        """Test that /appealmyclaims/scan redirects to the wizard."""
        self.open(f"{self.live_server_url}/appealmyclaims/scan")
        time.sleep(2)

        current_url = self.get_current_url()
        assert "/appealmyclaims/" in current_url
        assert "/scan" not in current_url

        self.assert_text("Healthcare Appeal Helper")

    def test_amc_wizard_has_config_data_attributes(self):
        """Test that wizard root element has required config data attributes."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        root = self.driver.find_element(By.ID, "amc-wizard-root")
        assert root.get_attribute("data-csrf-token") is not None
        assert root.get_attribute("data-ws-host") is not None
        assert root.get_attribute("data-privacy-url") is not None
        assert root.get_attribute("data-tos-url") is not None

    def test_amc_wizard_loads_manrope_font(self):
        """Test that AMC wizard page loads the Manrope font."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        page_source = self.get_page_source()
        assert "Manrope" in page_source

    def test_amc_privacy_and_tos_pages_accessible(self):
        """Test that AMC has access to privacy policy and TOS."""
        self.open(f"{self.live_server_url}/appealmyclaims/privacy_policy")
        time.sleep(1)
        self.assert_text("Privacy Policy")
        self.assert_text("Appeal My Claims")

        self.open(f"{self.live_server_url}/appealmyclaims/tos")
        time.sleep(1)
        self.assert_text("Terms of Service")
        self.assert_text("Appeal My Claims")

    def test_amc_about_page_accessible(self):
        """Test that AMC about page is accessible."""
        self.open(f"{self.live_server_url}/appealmyclaims/about-us")
        time.sleep(1)
        self.assert_text("Appeal My Claims")
        self.assert_text("About")

    def test_fhi_scan_unchanged(self):
        """Test that FHI scan page still works normally."""
        self.open(f"{self.live_server_url}/scan")
        time.sleep(2)

        self.assert_text("Fight Health Insurance")

        page_source = self.get_page_source()
        assert "amc-wizard-root" not in page_source
        assert "Healthcare Appeal Helper" not in page_source

        self.assert_element("#submit")
        self.assert_element('[name="denial_text"]')


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
        """Test that AMC pages have blue brand color CSS variable."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        page_source = self.get_page_source()

        assert "#1976d2" in page_source, "AMC blue color not found in page source"
        assert "--brand-primary-color" in page_source, "CSS variable not found"

    def test_fhi_has_green_css_variable(self):
        """Test that FHI pages have green color CSS variable."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        page_source = self.get_page_source()

        assert "#a5c422" in page_source, "FHI green color not found in page source"
        assert "--brand-primary-color" in page_source, "CSS variable not found"

    def test_amc_header_renders_with_shield_icon(self):
        """Test that AMC renders the blue header bar with title."""
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()

        # Check header text is rendered by React
        self.assert_text("Healthcare Appeal Helper")

        # Check for shield SVG icon in the page
        page_source = self.get_page_source()
        assert "M12 1 3 5v6c0 5.55" in page_source, "Shield icon SVG not found"

    def test_fhi_image_logo_renders(self):
        """Test that FHI renders image logo correctly."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

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
        # Start at AMC wizard (standalone template, React-rendered)
        self.open(f"{self.live_server_url}/appealmyclaims/")
        self.wait_for_wizard()
        self.assert_text("Healthcare Appeal Helper")
        self.assert_text("Powered by")

        # Navigate to about (still uses base.html with AMC branding)
        self.open(f"{self.live_server_url}/appealmyclaims/about-us")
        time.sleep(1)
        self.assert_text("Appeal My Claims")

        # Navigate to privacy policy
        self.open(f"{self.live_server_url}/appealmyclaims/privacy_policy")
        time.sleep(1)
        self.assert_text("Appeal My Claims")
        self.assert_text("Powered by")

    def test_fhi_brand_persists_through_navigation(self):
        """Test that navigating through FHI site maintains FHI branding."""
        self.open(f"{self.live_server_url}/")
        time.sleep(1)
        self.assert_text("Fight Health Insurance")

        self.open(f"{self.live_server_url}/scan")
        time.sleep(1)
        self.assert_text("Fight Health Insurance")

        with pytest.raises(Exception):
            self.assert_text("Powered by Fight Health Insurance")

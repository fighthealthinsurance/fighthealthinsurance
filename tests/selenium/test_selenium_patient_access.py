"""Selenium tests for Patient Access & Market Access landing page."""

import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestPatientAccessPage(FHISeleniumBase, StaticLiveServerTestCase):
    """Test patient access landing page loads and displays correctly."""

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

    def test_patient_access_page_loads(self):
        """Test that the patient access page loads successfully."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.assert_element("#patient-access-hero")

    def test_patient_access_hero_content(self):
        """Test that the hero section displays correct content."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.assert_text("Appeal Infrastructure for Patient Access Teams", "h1")
        self.assert_text("Patient access at scale", ".hero-tagline")

    def test_patient_access_book_demo_cta(self):
        """Test that the Book a Demo CTA is present and clickable."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.assert_element("a.primary-cta")
        self.assert_text("Book a Demo", "a.primary-cta")
        # Verify it's a mailto link
        href = self.get_attribute("a.primary-cta", "href")
        self.assertIn("mailto:support42@fighthealthinsurance.com", href)

    def test_patient_access_sections_present(self):
        """Test that all main sections are present on the page."""
        self.open(f"{self.live_server_url}/professionals/patient-access")

        # Check all section IDs exist
        self.assert_element("#landing-pages")
        self.assert_element("#evidence")
        self.assert_element("#workflow")
        self.assert_element("#use-cases")
        self.assert_element("#compliance")
        self.assert_element("#cta")

    def test_patient_access_landing_pages_section(self):
        """Test the Landing Pages You Deploy section content."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.scroll_to("#landing-pages")
        time.sleep(0.5)

        self.assert_text("AI and landing pages", "#landing-pages h2")
        self.assert_text("Drug-Specific Pages", "#landing-pages")
        self.assert_text("Condition-Specific Pages", "#landing-pages")
        self.assert_text("DME", "#landing-pages")

    def test_patient_access_workflow_options(self):
        """Test that all workflow options are displayed."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.scroll_to("#workflow")
        time.sleep(0.5)

        self.assert_text("Workflow Integration", "#workflow h2")
        self.assert_text("Fully Automated", "#workflow")
        self.assert_text("Human Reviewed", "#workflow")
        self.assert_text("Co-Authored", "#workflow")
        self.assert_text("Approval Gates", "#workflow")
        self.assert_text("Human review is optional", "#workflow")

    def test_patient_access_compliance_section(self):
        """Test the compliance section content."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.scroll_to("#compliance")
        time.sleep(0.5)

        self.assert_text("Built for Compliance", "#compliance h2")

    def test_patient_access_use_cases(self):
        """Test that concrete use cases are displayed."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.scroll_to("#use-cases")
        time.sleep(0.5)

        self.assert_text("hub team", "#use-cases")

    def test_patient_access_link_to_biologic_denial(self):
        """Test that link to biologic denial microsite works."""
        self.open(f"{self.live_server_url}/professionals/patient-access")

        # Find and click the Try as a Patient link in the hero
        self.click("a[href*='biologic-denial']")
        time.sleep(1)

        # Should be on the biologic denial microsite page
        current_url = self.get_current_url()
        self.assertIn("biologic-denial", current_url)


class SeleniumTestPatientAccessNavigation(FHISeleniumBase, StaticLiveServerTestCase):
    """Test navigation dropdown for Professional menu."""

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

    def test_homepage_has_professional_dropdown(self):
        """Test that the homepage has the Professional dropdown."""
        self.open(f"{self.live_server_url}/")
        self.assert_element("#professionalDropdown")
        self.assert_text("Professional", "#professionalDropdown")

    def test_dropdown_opens_on_click(self):
        """Test that clicking the dropdown toggle opens the menu."""
        self.open(f"{self.live_server_url}/")

        # Click the dropdown toggle
        self.click("#professionalDropdown")
        time.sleep(0.5)

        # Dropdown menu should be visible
        self.assert_element(".dropdown-menu.show")

    def test_dropdown_contains_practices_link(self):
        """Test that dropdown contains Practices & Hospitals link."""
        self.open(f"{self.live_server_url}/")
        self.click("#professionalDropdown")
        time.sleep(0.5)

        self.assert_text("Practices", ".dropdown-menu")

    def test_dropdown_contains_patient_access_link(self):
        """Test that dropdown contains Patient & Market Access link."""
        self.open(f"{self.live_server_url}/")
        self.click("#professionalDropdown")
        time.sleep(0.5)

        self.assert_text("Patient", ".dropdown-menu")
        self.assert_text("Market Access", ".dropdown-menu")

    def test_navigate_to_patient_access_from_dropdown(self):
        """Test navigating to patient access page via dropdown."""
        self.open(f"{self.live_server_url}/")

        # Open dropdown
        self.click("#professionalDropdown")
        time.sleep(0.5)

        # Click patient access link
        self.click("a.dropdown-item[href*='patient-access']")
        time.sleep(1)

        # Should be on patient access page
        current_url = self.get_current_url()
        self.assertIn("patient-access", current_url)
        self.assert_element("#patient-access-hero")

    def test_patient_access_page_has_dropdown(self):
        """Test that the patient access page also has the navigation dropdown."""
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.assert_element("#professionalDropdown")

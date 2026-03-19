"""Selenium tests for PHI placeholder warning before fax submission.

Verifies that when the appeal text still contains unfilled PHI placeholders
(e.g. {{FIRST_NAME}}, FirstName defaults), the user sees a confirmation
dialog before the fax form is submitted.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import BaseCase

from fighthealthinsurance.models import Denial

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

# Appeal text with unfilled placeholders
APPEAL_WITH_PLACEHOLDERS = (
    "Dear Insurance Company,\n\n"
    "I, {{FIRST_NAME}} {{LAST_NAME}}, am writing to appeal the denial "
    "of coverage. My subscriber ID is {{SCSID}}.\n\n"
    "Sincerely,\n"
    "{{Your Name}}"
)

# Appeal text with all placeholders filled in (no warnings expected)
APPEAL_FILLED_IN = (
    "Dear Insurance Company,\n\n"
    "I, Alice Wonderland, am writing to appeal the denial "
    "of coverage. My subscriber ID is SUB-12345.\n\n"
    "Sincerely,\n"
    "Alice Wonderland"
)

TEST_EMAIL = "phi_warn_test@example.com"
TEST_SEMI_SEKRET = "test-sekret-phi"


class SeleniumTestPHIFaxWarning(FHISeleniumBase, StaticLiveServerTestCase):
    """Test that unfilled PHI placeholders trigger a warning before fax."""

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

    def _create_denial(self):
        return Denial.objects.create(
            hashed_email=Denial.get_hashed_email(TEST_EMAIL),
            denial_text="Test denial for PHI warning",
            semi_sekret=TEST_SEMI_SEKRET,
            insurance_company="Test Insurance Co",
            claim_id="CLM-00001",
            procedure="Test Procedure",
            diagnosis="Test Diagnosis",
        )

    def _navigate_to_appeal_page(self, denial, appeal_text):
        """POST to choose_appeal to land on the appeal page with given text."""
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return document.querySelector('input[name=csrfmiddlewaretoken]')?.value "
                "|| document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';"
            )
        )
        csrf_token = self.execute_script(
            "return document.querySelector('input[name=csrfmiddlewaretoken]')?.value "
            "|| document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';"
        )
        assert csrf_token, "CSRF token not found — page may not have loaded correctly"

        appeal_escaped = (
            appeal_text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        )

        js = f"""
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = '{self.live_server_url}/choose_appeal';

        var fields = {{
            'csrfmiddlewaretoken': '{csrf_token}',
            'denial_id': '{denial.denial_id}',
            'email': '{TEST_EMAIL}',
            'semi_sekret': '{TEST_SEMI_SEKRET}',
            'appeal_text': '{appeal_escaped}'
        }};

        for (var key in fields) {{
            var input = document.createElement('input');
            input.type = 'hidden';
            input.name = key;
            input.value = fields[key];
            form.appendChild(input);
        }}

        document.body.appendChild(form);
        form.submit();
        """
        self.execute_script(js)

        # Wait for appeal page to load
        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.ID, "id_completed_appeal_text"))
        )

        # Wait for the appeal.bundle.js module to finish executing.
        # The module sets up a submit listener on the fax form. We detect
        # it's ready by waiting for the fax_appeal button to exist and
        # for document.readyState to be complete.
        self.wait_for_page_ready()

    def _fill_fax_form_fields(self, name="Test User"):
        """Fill required fax form fields."""
        if self.is_element_present("input#id_fax_phone"):
            self.type("input#id_fax_phone", "5551234567")
        if self.is_element_present("input#id_name"):
            self.type("input#id_name", name)
        if self.is_element_present("input#id_insurance_company"):
            self.type("input#id_insurance_company", "Test Insurance")

    def _click_fax_via_js(self):
        """Click the fax button via JS to ensure proper event dispatching."""
        self.execute_script("document.getElementById('fax_appeal').click();")

    def test_fax_warns_on_unfilled_placeholders(self):
        """Clicking fax with {{placeholders}} in text triggers a confirm dialog."""
        denial = self._create_denial()

        self.open(f"{self.live_server_url}/scan")
        self.wait_for_page_ready()
        # Do NOT set localStorage PII so placeholders remain as defaults
        self._navigate_to_appeal_page(denial, APPEAL_WITH_PLACEHOLDERS)

        # Verify placeholders are present in the completed appeal text
        completed_text = self.driver.find_element(
            By.ID, "id_completed_appeal_text"
        ).get_attribute("value")
        assert (
            "{{" in completed_text or "FirstName" in completed_text
        ), f"Expected placeholders in appeal text but got: {completed_text}"

        self._fill_fax_form_fields()

        # Click fax button via JS to ensure proper event propagation
        self._click_fax_via_js()

        # Should trigger a confirm() dialog
        alert = WebDriverWait(self.driver, 5).until(EC.alert_is_present())
        alert_text = alert.text

        # Verify the alert mentions placeholders
        assert (
            "placeholder" in alert_text.lower() or "{{" in alert_text
        ), f"Expected warning about placeholders, got: {alert_text}"

        # Dismiss the dialog (cancel) — form should NOT submit
        alert.dismiss()

        # Verify we're still on the same page (not redirected)
        assert self.is_element_present(
            "#id_completed_appeal_text"
        ), "Should still be on appeal page after dismissing warning"

    def test_fax_no_warning_when_placeholders_filled(self):
        """Clicking fax with all placeholders filled should NOT trigger a warning."""
        denial = self._create_denial()

        self.open(f"{self.live_server_url}/scan")
        self.wait_for_page_ready()
        self._navigate_to_appeal_page(denial, APPEAL_FILLED_IN)

        # Verify no placeholders in the completed appeal text
        completed_text = self.driver.find_element(
            By.ID, "id_completed_appeal_text"
        ).get_attribute("value")
        assert (
            "{{" not in completed_text
        ), f"Expected no placeholders but found some: {completed_text}"
        assert (
            "FirstName" not in completed_text
        ), f"Expected no default placeholder 'FirstName' but found it: {completed_text}"

        self._fill_fax_form_fields(name="Alice Wonderland")

        # Prevent actual form submission (we only want to test if alert appears)
        self.execute_script(
            "document.getElementById('fax_appeal').closest('form')"
            ".addEventListener('submit', function(e) {"
            "  if (!e.defaultPrevented) { e.preventDefault(); "
            "    window.__faxFormSubmitted = true; }"
            "}, {capture: true, once: true});"
        )

        # Click fax button via JS
        self._click_fax_via_js()

        # Give a brief moment for any alert to appear — use a short explicit
        # wait for the submit button to remain clickable (no navigation away).
        WebDriverWait(self.driver, 2).until(
            EC.element_to_be_clickable((By.ID, "fax_appeal"))
        )

        # Verify no alert is present (validation passed, no warning)
        try:
            alert = self.driver.switch_to.alert
            raise AssertionError(
                f"No alert should appear when placeholders are filled in, "
                f"but got: {alert.text}"
            )
        except NoAlertPresentException:
            pass  # Expected — no alert means validation passed

        # Verify the form would have submitted (our intercept caught it)
        submitted = self.execute_script("return window.__faxFormSubmitted === true;")
        assert (
            submitted
        ), "Form should have attempted to submit (no placeholder warning)"

    def test_fax_warning_dismiss_stays_on_page(self):
        """Dismissing the confirm dialog keeps user on the appeal page."""
        denial = self._create_denial()

        self.open(f"{self.live_server_url}/scan")
        self.wait_for_page_ready()
        self._navigate_to_appeal_page(denial, APPEAL_WITH_PLACEHOLDERS)

        self._fill_fax_form_fields()

        # Record the current URL before clicking
        url_before = self.driver.current_url

        # Click fax button — triggers confirm dialog
        self._click_fax_via_js()

        alert = WebDriverWait(self.driver, 5).until(EC.alert_is_present())
        alert_text = alert.text

        # Verify the alert content mentions placeholders
        assert (
            "placeholder" in alert_text.lower() or "{{" in alert_text
        ), f"Expected warning about placeholders, got: {alert_text}"

        # Dismiss (cancel) — should stay on same page
        alert.dismiss()

        # Wait for the page to remain stable (appeal text still present)
        WebDriverWait(self.driver, 2).until(
            EC.presence_of_element_located((By.ID, "id_completed_appeal_text"))
        )

        # Verify URL hasn't changed (form was NOT submitted)
        assert self.driver.current_url == url_before, (
            f"URL should not change after dismissing. Before: {url_before}, "
            f"After: {self.driver.current_url}"
        )

        # Verify appeal text is still editable
        assert self.is_element_present(
            "#id_completed_appeal_text"
        ), "Appeal text area should still be present"

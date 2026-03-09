"""Selenium e2e tests for PII placeholder rehydration.

Verifies that the descrub() function in appeal.ts correctly replaces
{{PLACEHOLDER}} tokens (and legacy formats) with actual user PII values
stored in localStorage.
"""

import hashlib

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import Denial
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

# Stubbed appeal text containing every placeholder format we support
STUBBED_APPEAL = (
    "Dear {{insurance_company}},\n\n"
    "I, {{FIRST_NAME}} {{LAST_NAME}}, am writing to appeal the denial "
    "of coverage. My subscriber ID is {{SCSID}}, my group ID is {{GPID}}, "
    "and the case reference is {{CASEID}}.\n\n"
    "Please contact me at {{Your Email Address}} or {{Your Phone Number}}.\n\n"
    "Sincerely,\n"
    "{{Your Name}}"
)

TEST_EMAIL = "testpii@example.com"
TEST_SEMI_SEKRET = "test-sekret-123"


class SeleniumTestPIIRehydration(FHISeleniumBase, StaticLiveServerTestCase):
    """Test that PII placeholders are correctly replaced on the appeal page."""

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
        """Create a minimal Denial object for the test."""
        hashed_email = hashlib.sha512(TEST_EMAIL.encode("utf-8").lower()).hexdigest()
        denial = Denial.objects.create(
            hashed_email=hashed_email,
            denial_text="Test denial for PII rehydration",
            semi_sekret=TEST_SEMI_SEKRET,
            insurance_company="Test Insurance Co",
            claim_id="CLM-98765",
            procedure="Test Procedure",
            diagnosis="Test Diagnosis",
        )
        return denial

    def _set_localstorage_pii(self):
        """Set PII values in localStorage matching what the form stores."""
        pii = {
            "store_fname": "Alice",
            "store_lname": "Wonderland",
            "subscriber_id": "SUB-12345",
            "group_id": "GRP-67890",
            "claim_id": "CLM-98765",
            "email_address": "alice@example.com",
            "phone_number": "555-867-5309",
        }
        for key, value in pii.items():
            self.execute_script(f"localStorage.setItem('{key}', '{value}');")

    def _navigate_to_appeal_page(self, denial):
        """Submit the choose_appeal form via JS to reach the appeal page."""
        # The CSRF token is already available from the page loaded in setUp
        # (we navigate to /scan which renders a form with {% csrf_token %})
        csrf_token = self.execute_script(
            "return document.querySelector('input[name=csrfmiddlewaretoken]')?.value "
            "|| document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';"
        )

        appeal_text_escaped = (
            STUBBED_APPEAL.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
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
            'appeal_text': '{appeal_text_escaped}'
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

    def test_pii_placeholders_replaced(self):
        """Test that {{PLACEHOLDER}} tokens are replaced with PII from localStorage."""
        denial = self._create_denial()

        # Open /scan which has a form with {% csrf_token %} so the CSRF cookie is set
        self.open(f"{self.live_server_url}/scan")
        self.wait_for_page_ready()
        self._set_localstorage_pii()

        # Navigate to the appeal page via form POST
        self._navigate_to_appeal_page(denial)

        # Wait for the appeal page to load
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "id_completed_appeal_text"))
        )
        # Wait a moment for descrub() to execute (it runs on page load)
        WebDriverWait(self.driver, 10).until(
            lambda d: "Alice"
            in (
                d.find_element(By.ID, "id_completed_appeal_text").get_attribute("value")
                or ""
            )
        )

        completed_text = self.driver.find_element(
            By.ID, "id_completed_appeal_text"
        ).get_attribute("value")

        # Verify all placeholders were replaced with correct values
        assert (
            "Alice" in completed_text
        ), f"First name 'Alice' not found in completed appeal text: {completed_text}"
        assert (
            "Wonderland" in completed_text
        ), f"Last name 'Wonderland' not found in completed appeal text: {completed_text}"
        assert (
            "Alice Wonderland" in completed_text
        ), f"Full name 'Alice Wonderland' not found in completed appeal text: {completed_text}"
        assert (
            "SUB-12345" in completed_text
        ), f"Subscriber ID 'SUB-12345' not found in completed appeal text: {completed_text}"
        assert (
            "GRP-67890" in completed_text
        ), f"Group ID 'GRP-67890' not found in completed appeal text: {completed_text}"
        assert (
            "CLM-98765" in completed_text
        ), f"Claim/Case ID 'CLM-98765' not found in completed appeal text: {completed_text}"
        assert (
            "alice@example.com" in completed_text
        ), f"Email 'alice@example.com' not found in completed appeal text: {completed_text}"
        assert (
            "555-867-5309" in completed_text
        ), f"Phone '555-867-5309' not found in completed appeal text: {completed_text}"

        # Verify no raw placeholders remain
        assert (
            "{{FIRST_NAME}}" not in completed_text
        ), "{{FIRST_NAME}} placeholder was not replaced"
        assert (
            "{{LAST_NAME}}" not in completed_text
        ), "{{LAST_NAME}} placeholder was not replaced"
        assert (
            "{{SCSID}}" not in completed_text
        ), "{{SCSID}} placeholder was not replaced"
        assert "{{GPID}}" not in completed_text, "{{GPID}} placeholder was not replaced"
        assert (
            "{{CASEID}}" not in completed_text
        ), "{{CASEID}} placeholder was not replaced"
        assert (
            "{{Your Name}}" not in completed_text
        ), "{{Your Name}} placeholder was not replaced"
        assert (
            "{{Your Email Address}}" not in completed_text
        ), "{{Your Email Address}} placeholder was not replaced"
        assert (
            "{{Your Phone Number}}" not in completed_text
        ), "{{Your Phone Number}} placeholder was not replaced"

    def test_legacy_placeholders_replaced(self):
        """Test that legacy placeholder formats are also replaced."""
        denial = self._create_denial()

        legacy_appeal = (
            "Dear Insurance Company,\n\n"
            "My name is [Your Name] and I am a patient of "
            "[Patient's Name]. My subscriber id is subscriber_id "
            "with group id group_id.\n\n"
            "Please contact me at [Email Address].\n\n"
            "Sincerely,\n"
            "$your_name_here"
        )

        # Open /scan which has a form with {% csrf_token %} so the CSRF cookie is set
        self.open(f"{self.live_server_url}/scan")
        self.wait_for_page_ready()
        self._set_localstorage_pii()

        # Override STUBBED_APPEAL with legacy text for this test
        appeal_text_escaped = (
            legacy_appeal.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        )

        csrf_token = self.execute_script(
            "return document.querySelector('input[name=csrfmiddlewaretoken]')?.value "
            "|| document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';"
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
            'appeal_text': '{appeal_text_escaped}'
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

        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "id_completed_appeal_text"))
        )
        WebDriverWait(self.driver, 10).until(
            lambda d: "Alice"
            in (
                d.find_element(By.ID, "id_completed_appeal_text").get_attribute("value")
                or ""
            )
        )

        completed_text = self.driver.find_element(
            By.ID, "id_completed_appeal_text"
        ).get_attribute("value")

        # Legacy [Your Name] should be replaced
        assert (
            "Alice Wonderland" in completed_text
        ), f"Full name not found after legacy placeholder replacement: {completed_text}"
        # Legacy $your_name_here should be replaced
        assert (
            "$your_name_here" not in completed_text
        ), f"Legacy $your_name_here was not replaced: {completed_text}"
        # Legacy [Email Address] should be replaced
        assert (
            "alice@example.com" in completed_text
        ), f"Email not found after legacy placeholder replacement: {completed_text}"

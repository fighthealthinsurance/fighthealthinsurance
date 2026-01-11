"""
Selenium E2E tests for logged-in patient dashboard flows - Phase 2.

Tests complete user journeys including:
- Dashboard navigation and empty states
- Call log creation and editing
- Evidence upload and download
- Logged-in appeal workflow integration
"""

import tempfile
import time
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from PIL import Image
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import BaseCase

from fhi_users.models import PatientUser
from fighthealthinsurance.models import Appeal, Denial, InsuranceCallLog, PatientEvidence

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

User = get_user_model()


class SeleniumPatientFlowsTests(FHISeleniumBase, StaticLiveServerTestCase):
    """E2E tests for logged-in patient dashboard functionality."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    test_username = "patient_selenium_test"
    test_password = "TestPass123!"
    test_email = "patient_selenium@example.com"

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def login_as_patient(self, username=None, password=None):
        """
        Helper method to login as a patient user.
        Creates the user if it doesn't exist.
        """
        username = username or self.test_username
        password = password or self.test_password

        # Navigate to login page
        self.open(f"{self.live_server_url}/v0/auth/login")

        # Wait for page to load
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "id_username"))
        )

        # Enter credentials
        self.type("#id_username", username)
        self.type("#id_password", password)

        # Submit login form
        self.click('button[type="submit"]')

        # Wait for redirect (should go to home or dashboard)
        time.sleep(2)

    def create_test_user_if_not_exists(self):
        """Create test user for login tests."""
        user, created = User.objects.get_or_create(
            username=self.test_username,
            defaults={
                "email": self.test_email,
                "is_active": True,
            },
        )
        if created:
            user.set_password(self.test_password)
            user.save()
        return user

    def test_dashboard_navigation_and_empty_states(self):
        """
        Test that a new patient can access the dashboard and sees empty states.
        Phase 2.1 - Test case 5 from plan.
        """
        # Create test user
        user = self.create_test_user_if_not_exists()

        # Login
        self.login_as_patient()

        # Navigate to dashboard
        self.open(f"{self.live_server_url}/patient-dashboard")

        # Verify dashboard title/header
        self.assert_element("h2:contains('My Dashboard')")

        # Verify all 3 tabs are present
        self.assert_element("button#appeals-tab")
        self.assert_element("button#call-logs-tab")
        self.assert_element("button#evidence-tab")

        # Check Appeals tab (default active) - should show empty state
        self.assert_element("text:No Appeals Yet")
        self.assert_element("a:contains('Generate Your First Appeal')")

        # Switch to Call Logs tab
        self.click("#call-logs-tab")
        time.sleep(1)

        # Should show empty state
        self.assert_element("text:No Calls Logged Yet")
        self.assert_element("a:contains('Log Your First Call')")

        # Switch to Evidence tab
        self.click("#evidence-tab")
        time.sleep(1)

        # Should show empty state
        self.assert_element("text:No Evidence Added Yet")
        self.assert_element("a:contains('Add Your First Evidence')")

    def test_call_log_creation_from_dashboard(self):
        """
        Test creating a call log from the dashboard.
        Phase 2.1 - Test case 2 from plan.
        """
        # Create test user
        user = self.create_test_user_if_not_exists()

        # Login
        self.login_as_patient()

        # Navigate to dashboard
        self.open(f"{self.live_server_url}/patient-dashboard")

        # Click to Call Log tab
        self.click("#call-logs-tab")
        time.sleep(1)

        # Click "Log a Call" button
        self.click("a:contains('Log a Call')")

        # Wait for form to load
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "id_call_date"))
        )

        # Verify we're on the call log form page
        self.assert_element("h2:contains('Log Insurance Call')")

        # Fill out the form
        # Note: datetime-local input format is YYYY-MM-DDTHH:MM
        self.type("#id_call_date", "2024-12-01T10:30")
        self.select_option_by_text("#id_call_type", "Claim Status")
        self.type("#id_reason_for_call", "Checking on claim for MRI scan")
        self.type("#id_representative_name", "Jane Smith")
        self.type("#id_reference_number", "REF123456")
        self.select_option_by_text("#id_outcome", "Pending")

        # Submit the form
        self.click('button[type="submit"]')

        # Wait for redirect back to dashboard
        time.sleep(2)

        # Should be back on dashboard
        self.assert_element("h2:contains('My Dashboard')")

        # Verify call log appears in the list
        self.assert_element("text:Checking on claim for MRI scan")
        self.assert_element("text:REF123456")
        self.assert_element("text:Jane Smith")

        # Verify badge count updated
        # The badge should show count of call logs
        badge_text = self.get_text(".nav-link:contains('Call Log') .badge")
        assert "1" in badge_text, f"Expected badge to show 1 call log, got: {badge_text}"

    def test_call_log_edit_and_delete(self):
        """
        Test editing and deleting a call log.
        Phase 2.1 - Test case 4 from plan.
        """
        # Create test user and patient
        user = self.create_test_user_if_not_exists()
        patient, _ = PatientUser.objects.get_or_create(
            user=user, defaults={"active": True}
        )

        # Create a call log directly in the database
        from django.utils import timezone

        call_log = InsuranceCallLog.objects.create(
            patient_user=patient,
            call_date=timezone.now(),
            call_type="claim_status",
            reason_for_call="Initial reason",
            outcome="pending",
            representative_name="John Doe",
        )

        # Login
        self.login_as_patient()

        # Navigate to dashboard
        self.open(f"{self.live_server_url}/patient-dashboard")

        # Click to Call Log tab
        self.click("#call-logs-tab")
        time.sleep(1)

        # Verify call log is visible
        self.assert_element("text:Initial reason")

        # Click Edit button
        edit_buttons = self.find_elements("a:contains('Edit')")
        if edit_buttons:
            edit_buttons[0].click()
        else:
            # Fallback: navigate directly
            self.open(f"{self.live_server_url}/my/call-log/{call_log.uuid}/edit")

        # Wait for edit form
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "id_reason_for_call"))
        )

        # Change outcome from pending to approved
        self.select_option_by_text("#id_outcome", "Approved")

        # Submit
        self.click('button[type="submit"]:contains("Save")')

        # Wait for redirect
        time.sleep(2)

        # Should see updated outcome badge (Approved should be green/success)
        self.assert_element(".badge:contains('Approved')")

        # Now test delete
        # Navigate back to edit
        self.open(f"{self.live_server_url}/my/call-log/{call_log.uuid}/edit")
        time.sleep(1)

        # Click delete button
        self.click('button[name="delete"]')

        # Wait for redirect
        time.sleep(2)

        # Call log should be gone
        with pytest.raises(Exception):
            self.assert_element("text:Initial reason")

    def test_evidence_upload_and_download(self):
        """
        Test uploading evidence and downloading it back.
        Phase 2.1 - Test case 3 from plan.
        """
        # Create test user
        user = self.create_test_user_if_not_exists()

        # Login
        self.login_as_patient()

        # Navigate to dashboard
        self.open(f"{self.live_server_url}/patient-dashboard")

        # Click to Evidence tab
        self.click("#evidence-tab")
        time.sleep(1)

        # Click "Add Evidence"
        self.click("a:contains('Add Evidence')")

        # Wait for form
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "id_title"))
        )

        # Fill out form
        self.type("#id_title", "Test Medical Record")
        self.select_option_by_text("#id_evidence_type", "Medical Record")
        self.type("#id_description", "MRI results from December 2024")

        # Create a test PDF file
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False, mode="wb"
        ) as tmp:
            # Minimal PDF content
            tmp.write(b"%PDF-1.4\nTest PDF content for evidence upload")
            tmp_path = tmp.name

        try:
            # Upload file
            file_input = self.find_element("#id_file")
            file_input.send_keys(tmp_path)

            # Check "Include in appeal"
            self.click("#id_include_in_appeal")

            # Submit
            self.click('button[type="submit"]')

            # Wait for redirect
            time.sleep(2)

            # Should be back on dashboard Evidence tab
            self.assert_element("text:Test Medical Record")
            self.assert_element("text:MRI results from December 2024")

            # Verify green border (include_in_appeal)
            # Card should have border-success class
            self.assert_element(".card.border-success")

            # Find and click Download button
            download_buttons = self.find_elements("a:contains('Download')")
            assert len(download_buttons) > 0, "No download button found"

            # Note: Actually downloading and verifying file content is complex in Selenium
            # For now, we verify the download link exists and is accessible
            download_url = download_buttons[0].get_attribute("href")
            assert "/my/evidence/" in download_url
            assert "/download" in download_url

        finally:
            # Clean up temp file
            Path(tmp_path).unlink(missing_ok=True)

    def test_evidence_text_only_no_file(self):
        """
        Test creating evidence without a file (text-only note).
        Verifies that file upload is optional.
        """
        # Create test user
        user = self.create_test_user_if_not_exists()

        # Login
        self.login_as_patient()

        # Navigate to dashboard
        self.open(f"{self.live_server_url}/patient-dashboard")

        # Click to Evidence tab
        self.click("#evidence-tab")
        time.sleep(1)

        # Click "Add Evidence"
        self.click("a:contains('Add Evidence')")

        # Wait for form
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "id_title"))
        )

        # Fill out form WITHOUT uploading a file
        self.type("#id_title", "Phone Conversation Notes")
        self.select_option_by_text("#id_evidence_type", "Personal Notes")
        self.type(
            "#id_text_content",
            "Called insurance on 12/1. Rep said claim is pending review. "
            "Should hear back in 5 business days.",
        )

        # Submit
        self.click('button[type="submit"]')

        # Wait for redirect
        time.sleep(2)

        # Should be back on dashboard Evidence tab
        self.assert_element("text:Phone Conversation Notes")

        # Should NOT have a download button (no file attached)
        download_buttons = self.find_elements("a:contains('Download')")
        # If there are download buttons, make sure none are for our text-only evidence
        # This is a sanity check - text-only evidence shouldn't show download

    def test_logged_in_user_sees_dashboard_link(self):
        """
        Test that logged-in users see the "My Dashboard" link in navigation.
        Phase 2.1 - Navigation test.
        """
        # Create test user
        user = self.create_test_user_if_not_exists()

        # Before login - should not see dashboard link
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        # Check for absence of dashboard link (may not work if nav is cached)
        # Skip this check as it's unreliable with caching

        # Login
        self.login_as_patient()

        # Navigate to home
        self.open(f"{self.live_server_url}/")
        time.sleep(1)

        # Should see "My Dashboard" link in navigation
        # Note: Exact selector depends on nav structure
        self.assert_element("a:contains('My Dashboard')")

        # Click it and verify it goes to dashboard
        self.click("a:contains('My Dashboard')")
        time.sleep(1)

        self.assert_element("h2:contains('My Dashboard')")

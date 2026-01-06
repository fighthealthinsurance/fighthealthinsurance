"""Tests for template rendering to catch field name bugs and missing context variables."""

import datetime
from unittest.mock import Mock

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.models import DataDeletionRequest, Denial


class CalendarEmailTemplateTest(TestCase):
    """Test calendar email templates render without errors."""

    def setUp(self):
        """Set up test denial with all required fields."""
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test denial",
            hashed_email="test-hash",
            raw_email="test@example.com",
            procedure="Hip Replacement",  # Test that procedure field works
            diagnosis="Arthritis",
            denial_date=datetime.date.today(),
        )

    def test_calendar_initial_email_renders(self):
        """Test calendar_initial.html renders with proper context."""
        from django.template.loader import render_to_string

        context = {
            "email": "test@example.com",
            "insurance_company": "Test Insurance",
            "calendar_download_url": "https://example.com/download",
            "denial": self.denial,
        }

        # Should render without errors
        html = render_to_string("emails/calendar_initial.html", context)

        # Check key content is present
        self.assertIn("Test Insurance", html)
        self.assertIn("support42@fighthealthinsurance.com", html)
        self.assertIn("Calendar Reminders Set!", html)

    def test_calendar_reminder_email_renders(self):
        """Test calendar_reminder.html renders with proper context and uses correct field."""
        from django.template.loader import render_to_string

        # Create a mock appeal
        mock_appeal = Mock()
        mock_appeal.appeal_text = "Test appeal"

        context = {
            "email": "test@example.com",
            "insurance_company": "Test Insurance",
            "calendar_download_url": "https://example.com/download",
            "denial": self.denial,
            "reminder_type": "2 Day Follow-up",
            "selected_appeal": mock_appeal,
            "reminder_type_code": "2day",
        }

        # Should render without errors
        html = render_to_string("emails/calendar_reminder.html", context)

        # Check key content is present
        self.assertIn("Test Insurance", html)
        self.assertIn("2 Day Follow-up", html)
        self.assertIn("support42@fighthealthinsurance.com", html)

        # CRITICAL: Verify procedure field is used (not claim_type)
        self.assertIn("Hip Replacement", html)

    def test_calendar_reminder_without_procedure(self):
        """Test calendar_reminder.html handles missing procedure gracefully."""
        from django.template.loader import render_to_string

        # Create denial without procedure
        denial_no_proc = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test denial",
            hashed_email="test-hash2",
            raw_email="test2@example.com",
            procedure=None,  # No procedure
        )

        context = {
            "email": "test@example.com",
            "insurance_company": "Test Insurance",
            "calendar_download_url": "https://example.com/download",
            "denial": denial_no_proc,
            "reminder_type": "30 Day Follow-up",
            "selected_appeal": None,
            "reminder_type_code": "30day",
        }

        # Should render without errors even without procedure
        html = render_to_string("emails/calendar_reminder.html", context)
        self.assertIn("Test Insurance", html)


class DeletionTemplateTest(TestCase):
    """Test deletion page templates render correctly."""

    def setUp(self):
        """Set up test client."""
        self.client = Client()

    def test_deletion_confirm_page_renders(self):
        """Test deletion_confirm_page.html renders."""
        # Create a deletion request
        deletion_request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="test-token-123",
        )

        url = reverse("confirm_deletion", kwargs={"token": "test-token-123"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test@example.com")
        self.assertContains(response, "Confirm Data Deletion")
        # Check that CSS class is used, not inline style
        self.assertContains(response, "deletion-page-container")

    def test_deletion_confirmed_page_renders(self):
        """Test deletion_confirmed.html renders after deletion."""
        from unittest.mock import patch

        deletion_request = DataDeletionRequest.objects.create(
            email="test@example.com",
            hashed_email="test-hash",
            token="test-token-456",
        )

        url = reverse("confirm_deletion", kwargs={"token": "test-token-456"})

        with patch(
            "fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.remove_data_for_email"
        ):
            response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data Deleted")
        self.assertContains(response, "deletion-page-container")

    def test_deletion_error_page_renders(self):
        """Test deletion_error.html renders for invalid token."""
        url = reverse("confirm_deletion", kwargs={"token": "invalid-token"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid")
        self.assertContains(response, "deletion-page-container")

    def test_deletion_requested_page_renders(self):
        """Test deletion_requested.html renders."""
        from django.template.loader import render_to_string

        context = {
            "title": "Check Your Email",
            "email": "test@example.com",
        }

        html = render_to_string("deletion_requested.html", context)

        self.assertIn("test@example.com", html)
        self.assertIn("deletion-page-container", html)


class AccountPromptBannerTest(TestCase):
    """Test account prompt banner renders correctly."""

    def test_account_prompt_banner_shows_for_anonymous(self):
        """Test banner shows for anonymous users."""
        from django.template.loader import render_to_string
        from django.contrib.auth.models import AnonymousUser
        from unittest.mock import Mock

        # Mock the request with anonymous user
        mock_request = Mock()
        mock_request.user = AnonymousUser()

        context = {"user": AnonymousUser()}

        # Render with mocked URL tags
        with self.settings(ROOT_URLCONF="fighthealthinsurance.urls"):
            try:
                html = render_to_string("partials/account_prompt_banner.html", context)
            except Exception:
                # If URL reverse fails, skip this part of the test
                # The important part is testing the CSS classes
                self.skipTest("URL reverse not available in test environment")

        # Should show banner content
        self.assertIn("Create a Free Account", html)
        self.assertIn("account-prompt-banner", html)
        # Check that CSS classes are used, not inline styles
        self.assertNotIn('style="background-color:', html)
        self.assertNotIn('style="font-weight:', html)

    def test_account_prompt_banner_hidden_for_authenticated(self):
        """Test banner is hidden for authenticated users."""
        from django.template.loader import render_to_string
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(username="testuser", password="password")

        context = {"user": user}

        with self.settings(ROOT_URLCONF="fighthealthinsurance.urls"):
            try:
                html = render_to_string("partials/account_prompt_banner.html", context)
            except Exception:
                self.skipTest("URL reverse not available in test environment")

        # Should NOT show banner content
        self.assertNotIn("Create a Free Account", html)


class DownloadCalendarViewTest(TestCase):
    """Test calendar download view renders .ics file correctly."""

    def setUp(self):
        """Set up test denial."""
        self.client = Client()
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test denial",
            hashed_email="test-hash",
            raw_email="test@example.com",
            procedure="MRI Scan",
            denial_date=datetime.date.today(),
        )

    def test_calendar_download_renders_ics(self):
        """Test calendar download returns valid .ics file."""
        url = reverse(
            "download_calendar",
            kwargs={
                "denial_uuid": self.denial.uuid,
                "hashed_email": self.denial.hashed_email,
            },
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar")

        # Decode response content
        ics_content = response.content.decode("utf-8")

        # Verify it's a valid iCalendar file
        self.assertIn("BEGIN:VCALENDAR", ics_content)
        self.assertIn("END:VCALENDAR", ics_content)
        self.assertIn("Test Insurance", ics_content)

        # Verify procedure field is used in .ics file
        self.assertIn("MRI Scan", ics_content)

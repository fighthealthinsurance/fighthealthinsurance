"""Test the mailing list mail admin page functionality"""

import smtplib

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, Client
from unittest.mock import patch, MagicMock

from fighthealthinsurance.forms import SendMailingListMailForm
from fighthealthinsurance.mailing_list_actor import (
    BULK_SEND_DELAY_MAX_SECONDS,
    BULK_SEND_DELAY_MIN_SECONDS,
    send_bulk_email,
)
from fighthealthinsurance.models import MailingListSubscriber
from fighthealthinsurance.utils import mask_email_for_logging

User = get_user_model()


class TestMaskEmailForLogging(TestCase):
    """Test the mask_email_for_logging utility function."""

    def test_mask_normal_email(self):
        """Test masking a normal email address."""
        result = mask_email_for_logging("holden.karau@gmail.com")
        self.assertEqual(result, "h*****@gmail.com")

    def test_mask_short_local_part(self):
        """Test masking an email with a single character local part."""
        result = mask_email_for_logging("a@example.com")
        self.assertEqual(result, "a*****@example.com")

    def test_mask_invalid_email_no_at(self):
        """Test handling of invalid email without @."""
        result = mask_email_for_logging("invalidemail")
        self.assertEqual(result, "***invalid***")

    def test_mask_empty_email(self):
        """Test handling of empty email."""
        result = mask_email_for_logging("")
        self.assertEqual(result, "***invalid***")

    def test_mask_none_email(self):
        """Test handling of None email."""
        result = mask_email_for_logging(None)
        self.assertEqual(result, "***invalid***")


class TestSendMailingListMailForm(TestCase):
    """Test the SendMailingListMailForm validation."""

    def test_form_valid_with_all_fields(self):
        """Test that form is valid with all required fields."""
        form = SendMailingListMailForm(
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML content</p>",
                "text_content": "Test plain text content",
                "test_email": "test@example.com",
            }
        )
        self.assertTrue(form.is_valid())

    def test_form_valid_without_test_email(self):
        """Test that form is valid without optional test_email."""
        form = SendMailingListMailForm(
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML content</p>",
                "text_content": "Test plain text content",
            }
        )
        self.assertTrue(form.is_valid())

    def test_form_invalid_missing_subject(self):
        """Test that form is invalid without subject."""
        form = SendMailingListMailForm(
            data={
                "html_content": "<p>Test HTML content</p>",
                "text_content": "Test plain text content",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("subject", form.errors)

    def test_form_invalid_missing_html_content(self):
        """Test that form is invalid without HTML content."""
        form = SendMailingListMailForm(
            data={
                "subject": "Test Subject",
                "text_content": "Test plain text content",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("html_content", form.errors)

    def test_form_invalid_missing_text_content(self):
        """Test that form is invalid without text content."""
        form = SendMailingListMailForm(
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML content</p>",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("text_content", form.errors)

    def test_form_invalid_bad_test_email(self):
        """Test that form is invalid with bad email format."""
        form = SendMailingListMailForm(
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML content</p>",
                "text_content": "Test plain text content",
                "test_email": "not-an-email",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("test_email", form.errors)


class TestSendMailingListMailView(TestCase):
    """Test the SendMailingListMailView."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """Set up test client and staff user."""
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staffuser",
            password="testpass123",
            email="staff@example.com",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularuser",
            password="testpass123",
            email="regular@example.com",
            is_staff=False,
        )

    def test_access_denied_for_anonymous_user(self):
        """Test that anonymous users are redirected to login."""
        response = self.client.get("/timbit/help/send_mailing_list_mail")
        # Staff member required redirects to login
        self.assertIn(response.status_code, [302, 403])

    def test_access_denied_for_regular_user(self):
        """Test that regular users are denied access."""
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.get("/timbit/help/send_mailing_list_mail")
        # Non-staff users should be denied or redirected
        self.assertIn(response.status_code, [302, 403])

    def test_access_granted_for_staff_user(self):
        """Test that staff users can access the page."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get("/timbit/help/send_mailing_list_mail")
        self.assertEqual(response.status_code, 200)

    def test_page_shows_subscriber_count(self):
        """Test that the page shows the subscriber count."""
        # Create some test subscribers
        MailingListSubscriber.objects.create(email="sub1@example.com", name="Sub 1")
        MailingListSubscriber.objects.create(email="sub2@example.com", name="Sub 2")

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get("/timbit/help/send_mailing_list_mail")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2")  # subscriber count

    def test_subscriber_count_dedupes_by_email(self):
        """Duplicate signups count once, matching what the actor sends."""
        MailingListSubscriber.objects.create(email="dupe@example.com")
        MailingListSubscriber.objects.create(email="DUPE@example.com")
        MailingListSubscriber.objects.create(email="other@example.com")

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get("/timbit/help/send_mailing_list_mail")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["recipient_count"], 2)

    # ray_cluster_available is forced True to model production. Without a
    # cluster the view returns 503 rather than auto-initing a local Ray cluster
    # in the web process just to send staff mail.
    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=True)
    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_form_submission_test_email(
        self, mock_ray, mock_actor_ref, mock_cluster_available
    ):
        """Test form submission with test email."""
        # Mock the ray actor
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        mock_actor.send_mailing_list_email.remote.return_value = "future"
        mock_ray.get.return_value = (1, 0, 0)

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(
            "/timbit/help/send_mailing_list_mail",
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML</p>",
                "text_content": "Test text",
                "test_email": "test@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test email sent successfully")

    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=True)
    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_form_submission_real_send_runs_in_background(
        self, mock_ray, mock_actor_ref, mock_cluster_available
    ):
        """A real (non-test) send dispatches to the actor and returns at once.

        The actor paces whole-list sends, so blocking on ray.get would time the
        request out and tempt the operator into a duplicate re-submit.
        """
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        MailingListSubscriber.objects.create(email="sub1@clinic.com", name="Sub 1")

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(
            "/timbit/help/send_mailing_list_mail",
            data={
                "subject": "Test Subject",
                "html_content": "<p>Test HTML</p>",
                "text_content": "Test text",
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_actor.send_mailing_list_email.remote.assert_called_once_with(
            "Test Subject", "<p>Test HTML</p>", "Test text", ""
        )
        mock_ray.get.assert_not_called()
        self.assertContains(response, "started in the background")
        self.assertContains(response, "~1 recipients")


class SendBulkEmailPacingTest(TestCase):
    """Real broadcasts trickle out slowly; test sends go out immediately."""

    RECIPIENTS = [
        ("a@clinic.com", "https://www.fighthealthinsurance.com/v0/unsubscribe/a"),
        ("b@clinic.com", None),
        ("c@clinic.com", "https://www.fighthealthinsurance.com/v0/unsubscribe/c"),
    ]

    @patch("fighthealthinsurance.mailing_list_actor.time.sleep")
    def test_real_send_paces_between_each_email(self, mock_sleep):
        counts = send_bulk_email(
            "Subject",
            "<p>Hi</p>",
            "Hi",
            list(self.RECIPIENTS),
            audience="mailing list",
            pace=True,
        )

        self.assertEqual(counts, (3, 0, 0))
        self.assertEqual(len(mail.outbox), 3)
        # Three sends -> two gaps: no sleep before the first or after the last.
        self.assertEqual(mock_sleep.call_count, 2)
        for sleep_call in mock_sleep.call_args_list:
            self.assertGreaterEqual(sleep_call.args[0], BULK_SEND_DELAY_MIN_SECONDS)
            self.assertLessEqual(sleep_call.args[0], BULK_SEND_DELAY_MAX_SECONDS)

    @patch("fighthealthinsurance.mailing_list_actor.time.sleep")
    def test_test_send_is_not_paced(self, mock_sleep):
        counts = send_bulk_email(
            "Subject",
            "<p>Hi</p>",
            "Hi",
            [("staff@clinic.com", None)],
            audience="mailing list",
            pace=False,
        )

        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(len(mail.outbox), 1)
        mock_sleep.assert_not_called()

    @patch("fighthealthinsurance.mailing_list_actor.time.sleep")
    def test_blocked_recipients_are_skipped_without_pacing(self, mock_sleep):
        counts = send_bulk_email(
            "Subject",
            "<p>Hi</p>",
            "Hi",
            [("blocked@example.com", None), ("ok@clinic.com", None)],
            audience="mailing list",
            pace=True,
        )

        self.assertEqual(counts, (1, 0, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ok@clinic.com"])
        # Blocked addresses never touch the relay: only one real attempt
        # happened, so there is nothing to pace.
        mock_sleep.assert_not_called()

    @patch("fighthealthinsurance.mailing_list_actor.time.sleep")
    def test_failed_sends_are_counted_and_still_paced(self, mock_sleep):
        with patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=smtplib.SMTPException("mailbox unavailable"),
        ):
            counts = send_bulk_email(
                "Subject",
                "<p>Hi</p>",
                "Hi",
                [("a@clinic.com", None), ("b@clinic.com", None)],
                audience="mailing list",
                pace=True,
            )

        self.assertEqual(counts, (0, 2, 0))
        # A failed attempt still hit the relay, so the gap before the next
        # attempt remains.
        self.assertEqual(mock_sleep.call_count, 1)

    def test_unsubscribe_footer_only_added_when_url_present(self):
        send_bulk_email(
            "Subject",
            "<p>Hi</p>",
            "Hi",
            list(self.RECIPIENTS),
            audience="mailing list",
            pace=False,
        )

        with_url, without_url, _ = mail.outbox
        self.assertIn(
            "To unsubscribe from future emails, visit: "
            "https://www.fighthealthinsurance.com/v0/unsubscribe/a",
            with_url.body,
        )
        self.assertIn(
            "https://www.fighthealthinsurance.com/v0/unsubscribe/a",
            with_url.alternatives[0][0],
        )
        self.assertNotIn("unsubscribe", without_url.body)


class TestStaffDashboardView(TestCase):
    """Test the StaffDashboardView."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """Set up test client and users."""
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staffuser",
            password="testpass123",
            email="staff@example.com",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularuser",
            password="testpass123",
            email="regular@example.com",
            is_staff=False,
        )

    def test_access_denied_for_anonymous_user(self):
        """Test that anonymous users are redirected to login."""
        response = self.client.get("/timbit/help/")
        self.assertIn(response.status_code, [302, 403])

    def test_access_denied_for_regular_user(self):
        """Test that regular users are denied access."""
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.get("/timbit/help/")
        self.assertIn(response.status_code, [302, 403])

    def test_access_granted_for_staff_user(self):
        """Test that staff users can access the dashboard."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get("/timbit/help/")
        self.assertEqual(response.status_code, 200)

    def test_dashboard_contains_key_links(self):
        """Test that the dashboard contains links to key staff views."""
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get("/timbit/help/")
        self.assertEqual(response.status_code, 200)
        # Check for key sections and links
        self.assertContains(response, "Staff Dashboard")
        self.assertContains(response, "Send Mailing List Email")
        self.assertContains(response, "Send Interested Professional Email")
        self.assertContains(response, "Activate Pro User")
        self.assertContains(response, "Enable Beta Features")
        self.assertContains(response, "Charts")

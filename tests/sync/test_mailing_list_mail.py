"""Test the mailing list mail admin page functionality"""

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from unittest.mock import patch, MagicMock

from fighthealthinsurance.forms import SendMailingListMailForm
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

    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_form_submission_test_email(self, mock_ray, mock_actor_ref):
        """Test form submission with test email."""
        # Mock the ray actor
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        mock_actor.send_mailing_list_email.remote.return_value = "future"
        mock_ray.get.return_value = (1, 0)

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
        self.assertContains(response, "Activate Pro User")
        self.assertContains(response, "Enable Beta Features")
        self.assertContains(response, "Charts")

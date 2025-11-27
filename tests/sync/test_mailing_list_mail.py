"""Test the mailing list mail admin page functionality"""

from django.contrib.auth import get_user_model
from django.test import TestCase, Client

from fighthealthinsurance.forms import SendMailingListMailForm
from fighthealthinsurance.models import MailingListSubscriber

User = get_user_model()


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

    def test_form_submission_test_email(self):
        """Test form submission with test email."""
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

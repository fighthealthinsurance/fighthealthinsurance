"""
Tests for the Explain Denial view functionality.
"""

from django.test import TestCase, Client
from django.urls import reverse
from fighthealthinsurance.models import MailingListSubscriber


class TestExplainDenialView(TestCase):
    """Test the Explain Denial view."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """Set up test client."""
        self.client = Client()
        self.url = reverse("explain_denial")

    def test_explain_denial_page_loads(self):
        """Test that the explain denial page loads successfully."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Explain My Denial")
        self.assertContains(response, "denial_text")

    def test_explain_denial_post_missing_denial_text(self):
        """Test POST without denial text returns error."""
        form_data = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com",
            "tos_agreement": True,
            "privacy_policy": True,
            "denial_text": "",  # Empty denial text
        }
        response = self.client.post(self.url, form_data)
        # Should re-render the form with an error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter your denial letter text")

    def test_explain_denial_post_missing_tos(self):
        """Test POST without TOS agreement fails validation."""
        form_data = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com",
            "tos_agreement": False,  # Missing TOS
            "privacy_policy": True,
            "denial_text": "My claim was denied for not being medically necessary.",
        }
        response = self.client.post(self.url, form_data)
        # Should re-render the form with validation error
        self.assertEqual(response.status_code, 200)
        # TOS is a required field
        self.assertContains(response, "This field is required", count=1)

    def test_explain_denial_post_missing_privacy_policy(self):
        """Test POST without privacy policy agreement fails validation."""
        form_data = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com",
            "tos_agreement": True,
            "privacy_policy": False,  # Missing privacy policy
            "denial_text": "My claim was denied for not being medically necessary.",
        }
        response = self.client.post(self.url, form_data)
        # Should re-render the form with validation error
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required", count=1)

    def test_explain_denial_post_valid_data_redirects_to_chat(self):
        """Test POST with valid data renders chat redirect template."""
        denial_text = "Your claim has been denied because the treatment was not medically necessary."
        form_data = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com",
            "tos_agreement": True,
            "privacy_policy": True,
            "denial_text": denial_text,
        }
        response = self.client.post(self.url, form_data)

        # Should render the chat_redirect.html template
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "chat_redirect.html")
        self.assertContains(response, denial_text)
        self.assertContains(response, "Redirecting to chat")

        # Check session was updated
        session = self.client.session
        self.assertTrue(session.get("consent_completed"))
        self.assertEqual(session.get("email"), "john@example.com")

    def test_explain_denial_post_with_subscription(self):
        """Test POST with subscription creates mailing list entry."""
        form_data = {
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane@example.com",
            "phone": "555-1234",
            "tos_agreement": True,
            "privacy_policy": True,
            "subscribe": True,  # Opt in to mailing list
            "denial_text": "My claim was denied.",
            "referral_source": "Search Engine (Google, Bing, etc.)",
            "referral_source_details": "Google",
        }

        # Check no subscriber exists before
        initial_count = MailingListSubscriber.objects.filter(
            email="jane@example.com"
        ).count()
        self.assertEqual(initial_count, 0)

        response = self.client.post(self.url, form_data)
        self.assertEqual(response.status_code, 200)

        # Check subscriber was created
        subscribers = MailingListSubscriber.objects.filter(email="jane@example.com")
        self.assertEqual(subscribers.count(), 1)

        subscriber = subscribers.first()
        self.assertEqual(subscriber.name, "Jane Smith")
        self.assertEqual(subscriber.phone, "555-1234")
        self.assertEqual(
            subscriber.referral_source, "Search Engine (Google, Bing, etc.)"
        )
        self.assertEqual(subscriber.referral_source_details, "Google")
        self.assertIn("From explain denial page", subscriber.comments)

    def test_explain_denial_post_without_subscription(self):
        """Test POST without subscription does not create mailing list entry."""
        form_data = {
            "first_name": "Bob",
            "last_name": "Jones",
            "email": "bob@example.com",
            "tos_agreement": True,
            "privacy_policy": True,
            "subscribe": False,  # Don't subscribe
            "denial_text": "My claim was denied.",
        }

        response = self.client.post(self.url, form_data)
        self.assertEqual(response.status_code, 200)

        # Check no subscriber was created
        subscribers = MailingListSubscriber.objects.filter(email="bob@example.com")
        self.assertEqual(subscribers.count(), 0)

    def test_explain_denial_preserves_denial_text_on_error(self):
        """Test that denial text is preserved when form has errors."""
        denial_text = "This is my denial letter text that should be preserved."
        form_data = {
            "first_name": "Test",
            "last_name": "User",
            "email": "invalid-email",  # Invalid email to trigger error
            "tos_agreement": True,
            "privacy_policy": True,
            "denial_text": denial_text,
        }

        response = self.client.post(self.url, form_data)
        self.assertEqual(response.status_code, 200)
        # Denial text should be preserved in the context
        self.assertContains(response, denial_text)

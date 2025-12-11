"""Test mailing list subscription during chat leads flow."""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from fighthealthinsurance.models import ChatLeads, MailingListSubscriber


class ChatLeadsSubscriptionTest(TestCase):
    """Test that mailing list subscription works during chat leads flow."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("chat-leads-list")

    def test_chat_lead_submission_creates_mailing_list_subscriber(self):
        """Test that submitting with subscribe=True creates a MailingListSubscriber."""
        # Initial count
        initial_subscriber_count = MailingListSubscriber.objects.count()
        initial_chat_lead_count = ChatLeads.objects.count()

        # Submit chat lead data with subscribe=True
        data = {
            "name": "John Doe",
            "email": "test@example.com",
            "phone": "555-1234",
            "company": "Test Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
            "drug": "Test Drug",
            "subscribe": True,
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "success")
        self.assertIn("session_id", response.data)

        # Check that a new chat lead was created
        new_chat_lead_count = ChatLeads.objects.count()
        self.assertEqual(new_chat_lead_count, initial_chat_lead_count + 1)

        # Check that a new subscriber was created
        new_subscriber_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_subscriber_count, initial_subscriber_count + 1)

        # Verify the subscriber data
        subscriber = MailingListSubscriber.objects.latest("id")
        self.assertEqual(subscriber.email, "test@example.com")
        self.assertEqual(subscriber.name, "John Doe")
        self.assertEqual(subscriber.phone, "555-1234")
        self.assertEqual(subscriber.comments, "From chat leads form")

    def test_chat_lead_submission_without_subscribe_does_not_create_subscriber(self):
        """Test that submitting without subscribe does not create a MailingListSubscriber."""
        # Initial count
        initial_subscriber_count = MailingListSubscriber.objects.count()
        initial_chat_lead_count = ChatLeads.objects.count()

        # Submit chat lead data with subscribe=False
        data = {
            "name": "Jane Smith",
            "email": "nosubscribe@example.com",
            "phone": "555-5678",
            "company": "Another Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
            "subscribe": False,
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that a new chat lead was created
        new_chat_lead_count = ChatLeads.objects.count()
        self.assertEqual(new_chat_lead_count, initial_chat_lead_count + 1)

        # Check that no new subscriber was created
        new_subscriber_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_subscriber_count, initial_subscriber_count)

        # Verify no subscriber with this email was created
        self.assertFalse(
            MailingListSubscriber.objects.filter(
                email="nosubscribe@example.com"
            ).exists()
        )

    def test_chat_lead_without_subscribe_field_does_not_create_subscriber(self):
        """Test that omitting subscribe field does not create a MailingListSubscriber."""
        # Initial count
        initial_subscriber_count = MailingListSubscriber.objects.count()

        # Submit chat lead data without subscribe field
        data = {
            "name": "Default User",
            "email": "default@example.com",
            "phone": "555-9999",
            "company": "Default Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that no subscriber was created (since subscribe was not provided)
        new_subscriber_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_subscriber_count, initial_subscriber_count)

        # Verify no subscriber was created
        self.assertFalse(
            MailingListSubscriber.objects.filter(email="default@example.com").exists()
        )

    def test_duplicate_subscription_prevention(self):
        """Test that submitting multiple times with same email doesn't create duplicates."""
        # Create an initial subscriber
        MailingListSubscriber.objects.create(
            email="existing@example.com",
            name="Existing User",
            comments="Previously subscribed",
        )
        initial_subscriber_count = MailingListSubscriber.objects.count()

        # Submit chat lead data with the same email
        data = {
            "name": "New User",
            "email": "existing@example.com",
            "phone": "555-1111",
            "company": "New Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
            "subscribe": True,
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that no new subscriber was created (duplicate prevention)
        new_subscriber_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_subscriber_count, initial_subscriber_count)

        # Verify the original subscriber data is unchanged
        subscriber = MailingListSubscriber.objects.get(email="existing@example.com")
        self.assertEqual(subscriber.name, "Existing User")
        self.assertEqual(subscriber.comments, "Previously subscribed")

    def test_chat_lead_required_fields_validation(self):
        """Test that required fields are validated."""
        # Try to submit without required fields
        data = {
            "name": "Test User",
            "email": "test@example.com",
            # Missing consent_to_contact and agreed_to_terms
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request failed with validation errors
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["status"], "error")
        self.assertIn("errors", response.data)

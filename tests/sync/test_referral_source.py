"""Test referral_source field in mailing list subscription and chat leads."""

from django.test import TestCase, Client
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from fighthealthinsurance.models import MailingListSubscriber, ChatLeads, Denial


class ReferralSourceAppealFlowTest(TestCase):
    """Test that referral_source field works during appeal flow."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.client = Client()

    def test_subscription_with_referral_source(self):
        """Test that submitting with referral_source saves it to MailingListSubscriber and Denial."""
        # Submit form data with referral_source
        response = self.client.post(
            reverse("process"),
            {
                "email": "test@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "subscribe": "on",
                "fname": "John",
                "lname": "Doe",
                "referral_source": "Search Engine (Google, Bing, etc.)",
                "referral_source_details": "Google",
            },
            follow=True,
        )

        # Check that we're redirected to the next step
        self.assertEqual(response.status_code, 200)

        # Verify the subscriber data includes referral_source and details
        subscriber = MailingListSubscriber.objects.get(email="test@example.com")
        self.assertEqual(
            subscriber.referral_source, "Search Engine (Google, Bing, etc.)"
        )
        self.assertEqual(subscriber.referral_source_details, "Google")

        # Verify the denial object also has referral_source and details
        denial = Denial.objects.filter(
            hashed_email=Denial.get_hashed_email("test@example.com")
        ).first()
        self.assertIsNotNone(denial)
        self.assertEqual(denial.referral_source, "Search Engine (Google, Bing, etc.)")
        self.assertEqual(denial.referral_source_details, "Google")

    def test_submission_without_subscribe_still_captures_referral(self):
        """Test that referral source is captured on Denial even without subscribe."""
        # Submit form data with referral_source but without subscribe
        response = self.client.post(
            reverse("process"),
            {
                "email": "nosubscribe@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                # subscribe not included
                "referral_source": "Friend or Family",
                "referral_source_details": "My friend Sarah",
            },
            follow=True,
        )

        # Check that we're redirected to the next step
        self.assertEqual(response.status_code, 200)

        # No mailing list subscriber should be created
        self.assertFalse(
            MailingListSubscriber.objects.filter(
                email="nosubscribe@example.com"
            ).exists()
        )

        # But the denial should have the referral source
        denial = Denial.objects.filter(
            hashed_email=Denial.get_hashed_email("nosubscribe@example.com")
        ).first()
        self.assertIsNotNone(denial)
        self.assertEqual(denial.referral_source, "Friend or Family")
        self.assertEqual(denial.referral_source_details, "My friend Sarah")

    def test_subscription_without_referral_source(self):
        """Test that submitting without referral_source works fine."""
        # Submit form data without referral_source
        response = self.client.post(
            reverse("process"),
            {
                "email": "test2@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "subscribe": "on",
                "fname": "Jane",
                "lname": "Doe",
            },
            follow=True,
        )

        # Check that we're redirected to the next step
        self.assertEqual(response.status_code, 200)

        # Verify the subscriber data has empty referral_source and details
        subscriber = MailingListSubscriber.objects.get(email="test2@example.com")
        self.assertEqual(subscriber.referral_source, "")
        self.assertEqual(subscriber.referral_source_details, "")


class ReferralSourceChatLeadsTest(TestCase):
    """Test that referral_source field works during chat leads flow."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("chat-leads-list")

    def test_chat_lead_with_referral_source(self):
        """Test that submitting with referral_source saves it to ChatLeads."""
        # Submit chat lead data with referral_source
        data = {
            "name": "John Doe",
            "email": "test@example.com",
            "phone": "555-1234",
            "company": "Test Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
            "referral_source": "Friend or Family",
            "referral_source_details": "My friend Jane",
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the chat lead data includes referral_source and details
        chat_lead = ChatLeads.objects.get(email="test@example.com")
        self.assertEqual(chat_lead.referral_source, "Friend or Family")
        self.assertEqual(chat_lead.referral_source_details, "My friend Jane")

    def test_chat_lead_without_referral_source(self):
        """Test that submitting without referral_source works fine."""
        # Submit chat lead data without referral_source
        data = {
            "name": "Jane Smith",
            "email": "test2@example.com",
            "phone": "555-5678",
            "company": "Another Company",
            "consent_to_contact": True,
            "agreed_to_terms": True,
        }

        response = self.client.post(self.url, data, format="json")

        # Check that the request was successful
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the chat lead data has null referral_source and details
        chat_lead = ChatLeads.objects.get(email="test2@example.com")
        self.assertIsNone(chat_lead.referral_source)
        self.assertIsNone(chat_lead.referral_source_details)


class ReferralSourceChatConsentTest(TestCase):
    """Test that referral_source field works during chat consent flow."""

    def setUp(self):
        self.client = Client()

    def test_chat_consent_with_referral_source(self):
        """Test that chat consent form saves referral_source to mailing list."""
        # Submit chat consent form data with subscribe and referral_source
        response = self.client.post(
            reverse("chat_consent"),
            {
                "first_name": "John",
                "last_name": "Doe",
                "email": "chattest@example.com",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "subscribe": "on",
                "referral_source": "Healthcare Provider",
                "referral_source_details": "Dr. Smith's office",
            },
            follow=True,
        )

        # Check that we're redirected to chat interface
        self.assertEqual(response.status_code, 200)

        # Verify the subscriber data includes referral_source and details
        subscriber = MailingListSubscriber.objects.get(email="chattest@example.com")
        self.assertEqual(subscriber.referral_source, "Healthcare Provider")
        self.assertEqual(subscriber.referral_source_details, "Dr. Smith's office")

    def test_chat_consent_without_referral_source(self):
        """Test that chat consent form works without referral_source."""
        # Submit chat consent form data with subscribe but no referral_source
        response = self.client.post(
            reverse("chat_consent"),
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "chattest2@example.com",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "subscribe": "on",
            },
            follow=True,
        )

        # Check that we're redirected to chat interface
        self.assertEqual(response.status_code, 200)

        # Verify the subscriber data has empty referral_source and details
        subscriber = MailingListSubscriber.objects.get(email="chattest2@example.com")
        self.assertEqual(subscriber.referral_source, "")
        self.assertEqual(subscriber.referral_source_details, "")

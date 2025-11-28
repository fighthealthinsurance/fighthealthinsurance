"""Test the unsubscribe functionality"""

import pytest
from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance.models import MailingListSubscriber
from fighthealthinsurance.utils import get_unsubscribe_url


class UnsubscribeViewTest(TestCase):
    """Test the unsubscribe view functionality."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.subscriber = MailingListSubscriber.objects.create(
            email="test@example.com",
            name="Test User",
        )

    def test_unsubscribe_with_valid_token(self):
        """Test that a subscriber can unsubscribe with a valid token."""
        token = self.subscriber.unsubscribe_token
        url = reverse("unsubscribe", kwargs={"token": token})
        
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unsubscribed")
        self.assertContains(response, "test@example.com")
        
        # Verify the subscriber was deleted
        self.assertEqual(
            MailingListSubscriber.objects.filter(email="test@example.com").count(), 0
        )

    def test_unsubscribe_with_invalid_token(self):
        """Test that an invalid token shows an error message."""
        url = reverse("unsubscribe", kwargs={"token": "invalid-token-12345"})
        
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid or has already been used")
        
        # Verify the subscriber was NOT deleted
        self.assertEqual(
            MailingListSubscriber.objects.filter(email="test@example.com").count(), 1
        )

    def test_unsubscribe_twice(self):
        """Test that unsubscribing twice shows an error."""
        token = self.subscriber.unsubscribe_token
        url = reverse("unsubscribe", kwargs={"token": token})
        
        # First unsubscribe should succeed
        response1 = self.client.get(url)
        self.assertEqual(response1.status_code, 200)
        self.assertContains(response1, "test@example.com")
        
        # Second unsubscribe should show error
        response2 = self.client.get(url)
        self.assertEqual(response2.status_code, 200)
        self.assertContains(response2, "invalid or has already been used")


class UnsubscribeUrlTest(TestCase):
    """Test the unsubscribe URL generation."""

    def test_get_unsubscribe_url_with_existing_subscriber(self):
        """Test that get_unsubscribe_url returns URL for existing subscriber."""
        subscriber = MailingListSubscriber.objects.create(
            email="subscriber@example.com",
            name="Test Subscriber",
        )
        
        url = get_unsubscribe_url("subscriber@example.com")
        
        self.assertIsNotNone(url)
        self.assertIn(subscriber.unsubscribe_token, url)
        self.assertIn("unsubscribe", url)

    def test_get_unsubscribe_url_with_nonexistent_subscriber(self):
        """Test that get_unsubscribe_url returns None for non-existent subscriber."""
        url = get_unsubscribe_url("nonexistent@example.com")
        
        self.assertIsNone(url)

    def test_subscriber_get_unsubscribe_url_method(self):
        """Test the get_unsubscribe_url method on the model."""
        subscriber = MailingListSubscriber.objects.create(
            email="model-test@example.com",
            name="Model Test",
        )
        
        url = subscriber.get_unsubscribe_url()
        
        self.assertIn("https://www.fighthealthinsurance.com", url)
        self.assertIn("v0/unsubscribe", url)
        self.assertIn(subscriber.unsubscribe_token, url)


class MailingListSubscriberTokenTest(TestCase):
    """Test that unsubscribe tokens are generated correctly."""

    def test_token_is_generated_on_create(self):
        """Test that a token is automatically generated when creating a subscriber."""
        subscriber = MailingListSubscriber.objects.create(
            email="token-test@example.com",
        )
        
        self.assertIsNotNone(subscriber.unsubscribe_token)
        self.assertTrue(len(subscriber.unsubscribe_token) > 0)

    def test_tokens_are_unique(self):
        """Test that different subscribers get unique tokens."""
        subscriber1 = MailingListSubscriber.objects.create(
            email="unique1@example.com",
        )
        subscriber2 = MailingListSubscriber.objects.create(
            email="unique2@example.com",
        )
        
        self.assertNotEqual(
            subscriber1.unsubscribe_token, subscriber2.unsubscribe_token
        )

"""Tests for subscription helper with typed exception handling."""

from unittest.mock import patch

from django.db import DatabaseError, IntegrityError
from django.test import TestCase

from fighthealthinsurance.models import MailingListSubscriber
from fighthealthinsurance.helpers.subscription_helpers import subscribe_to_mailing_list


class SubscribeToMailingListTest(TestCase):
    """Tests for the subscribe_to_mailing_list helper function."""

    def test_creates_new_subscriber(self):
        """New email creates a MailingListSubscriber record."""
        subscribe_to_mailing_list(
            email="new@example.com",
            source="From test",
            name="Test User",
            phone="555-1234",
        )

        subscriber = MailingListSubscriber.objects.get(email="new@example.com")
        self.assertEqual(subscriber.name, "Test User")
        self.assertEqual(subscriber.phone, "555-1234")
        self.assertEqual(subscriber.comments, "From test")

    def test_existing_email_does_not_duplicate(self):
        """Subscribing with an existing email does not create a duplicate."""
        MailingListSubscriber.objects.create(
            email="existing@example.com",
            name="Original",
            comments="Original comment",
        )

        subscribe_to_mailing_list(
            email="existing@example.com",
            source="From test retry",
            name="Updated",
        )

        # Should still have exactly one record
        self.assertEqual(
            MailingListSubscriber.objects.filter(email="existing@example.com").count(),
            1,
        )

    def test_integrity_error_falls_back_to_update(self):
        """IntegrityError during get_or_create falls back to filter().update()."""
        MailingListSubscriber.objects.create(
            email="race@example.com",
            name="Original",
            comments="Original",
        )

        with patch.object(
            MailingListSubscriber.objects,
            "get_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            subscribe_to_mailing_list(
                email="race@example.com",
                source="From race condition",
                name="Updated Name",
            )

        subscriber = MailingListSubscriber.objects.get(email="race@example.com")
        self.assertEqual(subscriber.name, "Updated Name")
        self.assertEqual(subscriber.comments, "From race condition")

    def test_database_error_is_reraised(self):
        """DatabaseError is logged and re-raised, not swallowed."""
        with patch.object(
            MailingListSubscriber.objects,
            "get_or_create",
            side_effect=DatabaseError("connection refused"),
        ):
            with self.assertRaises(DatabaseError):
                subscribe_to_mailing_list(
                    email="dbfail@example.com",
                    source="From test",
                )

    def test_referral_fields_are_stored(self):
        """Referral source and details are passed through to the subscriber."""
        subscribe_to_mailing_list(
            email="referral@example.com",
            source="From referral test",
            referral_source="google",
            referral_source_details="searched for health insurance appeal",
        )

        subscriber = MailingListSubscriber.objects.get(email="referral@example.com")
        self.assertEqual(subscriber.referral_source, "google")
        self.assertEqual(
            subscriber.referral_source_details,
            "searched for health insurance appeal",
        )

    def test_empty_optional_fields_not_stored(self):
        """Empty optional fields are not included in defaults dict."""
        subscribe_to_mailing_list(
            email="minimal@example.com",
            source="From minimal test",
        )

        subscriber = MailingListSubscriber.objects.get(email="minimal@example.com")
        self.assertEqual(subscriber.name, "")
        self.assertEqual(subscriber.phone, "")
        self.assertEqual(subscriber.comments, "From minimal test")

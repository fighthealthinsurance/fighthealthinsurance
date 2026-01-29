import unittest
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

from fighthealthinsurance.models import StripeWebhookEvents, LostStripeSession
from fighthealthinsurance.helpers.stripe_helpers import StripeWebhookHelper

User = get_user_model()


class StripeWebhookTests(TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.event_id = "evt_test123"
        self.session_id = "cs_test123"
        self.email = "test@example.com"

    @patch("fighthealthinsurance.helpers.stripe_helpers.logger")
    def test_duplicate_webhook_event_skipped(self, mock_logger):
        # Create a record of an already processed event
        StripeWebhookEvents.objects.create(event_stripe_id=self.event_id, success=True)

        # Mock a Stripe event
        mock_event = MagicMock()
        mock_event.id = self.event_id

        # Call the handler
        StripeWebhookHelper.handle_stripe_webhook(self.client, mock_event)

        # Verify the logger was called with the expected message
        mock_logger.debug.assert_called_with(
            f"Skipping duplicate stripe event {self.event_id}"
        )

        # Verify no additional event record was created
        self.assertEqual(StripeWebhookEvents.objects.count(), 1)

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.StripeWebhookHelper.handle_checkout_session_completed"
    )
    @patch("fighthealthinsurance.helpers.stripe_helpers.logger")
    def test_webhook_event_created_and_updated_on_success(
        self, mock_logger, mock_handler
    ):
        # Mock a Stripe event
        mock_event = MagicMock()
        mock_event.id = f"{self.event_id}_new"
        mock_event.type = "checkout.session.completed"
        mock_event.data.object = "session_data"

        # Call the handler
        StripeWebhookHelper.handle_stripe_webhook(self.client, mock_event)

        # Verify the handler was called
        mock_handler.assert_called_once_with(self.client, "session_data")

        # Verify an event record was created
        event_record = StripeWebhookEvents.objects.get(
            event_stripe_id=f"{self.event_id}_new"
        )
        self.assertTrue(event_record.success)
        self.assertIsNone(event_record.error)

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.StripeWebhookHelper.handle_checkout_session_completed"
    )
    @patch("fighthealthinsurance.helpers.stripe_helpers.logger")
    def test_webhook_event_records_error_on_failure(self, mock_logger, mock_handler):
        # Set up handler to raise an exception
        mock_handler.side_effect = Exception("Test error")

        # Mock a Stripe event
        mock_event = MagicMock()
        mock_event.id = f"{self.event_id}_error"
        mock_event.type = "checkout.session.completed"
        mock_event.data.object = "session_data"

        # Call the handler and expect it to raise the exception
        with self.assertRaises(Exception):
            StripeWebhookHelper.handle_stripe_webhook(self.client, mock_event)

        # Verify an event record was created with error information
        event_record = StripeWebhookEvents.objects.get(
            event_stripe_id=f"{self.event_id}_error"
        )
        self.assertFalse(event_record.success)
        self.assertEqual(event_record.error, "Test error")

    @patch("fhi_users.emails.send_checkout_session_expired")
    @patch("fighthealthinsurance.helpers.stripe_helpers.logger")
    def test_duplicate_checkout_session_expired_email_skipped(
        self, mock_logger, mock_send_email
    ):
        # Create a record of an already processed lost session
        LostStripeSession.objects.create(
            session_id=self.session_id, email=self.email, payment_type="fax"
        )

        # Mock a session object
        mock_session = MagicMock()
        mock_session.id = self.session_id
        mock_session.metadata = {"payment_type": "fax"}
        # Set up the customer_email attribute (not costumer_email)
        mock_session.customer_email = self.email

        # Call the handler
        StripeWebhookHelper.handle_checkout_session_expired(self.client, mock_session)

        # Verify the logger was called with the expected message
        mock_logger.debug.assert_called_with(
            f"Skipping duplicate lost stripe session notification for {self.email}"
        )

        # Verify the email was not sent
        mock_send_email.assert_not_called()

        # Verify no additional session record was created
        self.assertEqual(LostStripeSession.objects.count(), 1)

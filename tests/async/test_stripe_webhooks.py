import unittest
from unittest.mock import patch, MagicMock

from django.conf import settings
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

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.fhi_emails.send_checkout_session_expired"
    )
    def test_checkout_session_expired_email_uses_absolute_link_and_token(
        self, mock_send_email
    ):
        mock_session = MagicMock()
        mock_session.id = "cs_unique_for_token_test"
        mock_session.metadata = {
            "payment_type": "non_professional_item",
            "line_items": "[]",
        }
        mock_session.customer_email = "buyer@example.com"

        StripeWebhookHelper.handle_checkout_session_expired(self.client, mock_session)

        mock_send_email.assert_called_once()
        link = mock_send_email.call_args.kwargs["link"]

        lost_session = LostStripeSession.objects.get(session_id=mock_session.id)
        # Non-professional recovery is completed by the Django `complete_payment`
        # view, which is served on the Fight Health Insurance domain. The link
        # must be absolute, target that host (so non-prod environments don't
        # email production links and so customers don't hit the Fight Paperwork
        # SPA's "Invalid Demo Type" catch-all), and carry the unguessable token
        # rather than the row id.
        self.assertTrue(
            link.startswith(f"https://{settings.FIGHT_HEALTH_INSURANCE_DOMAIN}/")
        )
        self.assertIn("/stripe/finish?", link)
        self.assertIn(f"token={lost_session.secure_token}", link)
        self.assertNotIn(f"session_id={lost_session.id}", link)
        self.assertNotIn(f"session_id={lost_session.pk}", link)

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.fhi_emails.send_checkout_session_expired"
    )
    def test_professional_subscription_recovery_link_targets_fpw_spa(
        self, mock_send_email
    ):
        """Professional domain subscriptions resume in the Fight Paperwork SPA."""
        mock_session = MagicMock()
        mock_session.id = "cs_unique_for_pro_test"
        mock_session.metadata = {
            "payment_type": "professional_domain_subscription",
            "domain_id": "123",
            "professional_id": "456",
        }
        mock_session.customer_email = "pro@example.com"

        StripeWebhookHelper.handle_checkout_session_expired(self.client, mock_session)

        mock_send_email.assert_called_once()
        link = mock_send_email.call_args.kwargs["link"]
        self.assertTrue(link.startswith(f"https://{settings.FIGHT_PAPERWORK_DOMAIN}/"))
        self.assertIn("/stripe/finish-checkout?", link)
        self.assertIn("domain_id=123", link)
        self.assertIn("professional_id=456", link)

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.fhi_emails.send_checkout_session_expired"
    )
    def test_no_recovery_email_for_unrebuildable_payment(self, mock_send_email):
        """Payment types with no line_items/recovery_info don't get a dead link.

        Pay-what-you-want donations only store payment_type/source metadata, so
        the complete_payment view can't recreate the checkout. We must skip the
        recovery email rather than send a link that would only error.
        """
        mock_session = MagicMock()
        mock_session.id = "cs_unique_for_donation_test"
        mock_session.metadata = {
            "payment_type": "donation",
            "donation_type": "pwyw",
            "source": "checkout",
        }
        mock_session.customer_email = "donor@example.com"

        StripeWebhookHelper.handle_checkout_session_expired(self.client, mock_session)

        mock_send_email.assert_not_called()

    @patch(
        "fighthealthinsurance.helpers.stripe_helpers.fhi_emails.send_checkout_session_expired"
    )
    def test_no_recovery_email_when_professional_ids_missing(self, mock_send_email):
        """A professional subscription without domain/professional ids is skipped."""
        mock_session = MagicMock()
        mock_session.id = "cs_unique_for_pro_missing_ids"
        mock_session.metadata = {
            "payment_type": "professional_domain_subscription",
        }
        mock_session.customer_email = "pro@example.com"

        StripeWebhookHelper.handle_checkout_session_expired(self.client, mock_session)

        mock_send_email.assert_not_called()

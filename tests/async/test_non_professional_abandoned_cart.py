import json
import pytest
from unittest.mock import patch
from django.urls import reverse
from django.test import Client
from fighthealthinsurance.models import LostStripeSession, StripeRecoveryInfo
from fighthealthinsurance.common_view_logic import StripeWebhookHelper


@pytest.mark.django_db
def test_non_professional_abandoned_cart():
    client = Client()
    session_id = "test_session_id"
    email = "test@example.com"
    line_items = [{"price": "price_123", "quantity": 1}]
    metadata = {"line_items": json.dumps(line_items)}

    # Create a LostStripeSession
    lost_session = LostStripeSession.objects.create(
        session_id=session_id,
        payment_type="non_professional_item",
        email=email,
        metadata=metadata,
    )

    # Mock the Stripe API call
    with patch("stripe.checkout.Session.create") as mock_create:
        mock_create.return_value.url = "https://checkout.stripe.com/test"

        # Call the CompletePaymentView
        response = client.post(
            reverse("complete_payment"),
            data=json.dumps(
                {
                    "session_id": session_id,
                    "continue_url": "https://example.com/success",
                    "cancel_url": "https://example.com/cancel",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert "next_url" in response.json()
        assert response.json()["next_url"] == "https://checkout.stripe.com/test"

    # Simulate Stripe webhook call to trigger the email
    stripe_event = {
        "type": "checkout.session.expired",
        "data": {
            "object": {
                "metadata": {
                    "payment_type": "non_professional_item",
                    "session_id": session_id,
                },
                "customer_details": {
                    "email": email,
                },
            }
        },
    }
    StripeWebhookHelper.handle_checkout_session_expired(
        None, stripe_event["data"]["object"]
    )

    # Check that the LostStripeSession was created
    assert LostStripeSession.objects.filter(session_id=session_id).exists()
    lost_session = LostStripeSession.objects.get(session_id=session_id)
    assert lost_session.email == email
    assert lost_session.payment_type == "non_professional_item"
    assert lost_session.metadata == metadata

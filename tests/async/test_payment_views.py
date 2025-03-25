import json
import pytest
from django.urls import reverse
from django.test import Client
from fighthealthinsurance.models import LostStripeSession

@pytest.mark.django_db
def test_complete_payment_view_for_non_professional(client: Client):
    session_id = "test_session_id"
    LostStripeSession.objects.create(
        session_id=session_id,
        payment_type="non_professional_item",
        email="test@example.com",
        metadata={"line_items": [{"price": "price_123", "quantity": 1}]},
    )
    response = client.post(
        reverse("complete_payment"),
        data=json.dumps({
            "session_id": session_id,
            "continue_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        }),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert "next_url" in response.json()

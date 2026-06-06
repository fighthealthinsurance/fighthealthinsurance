import datetime
import json
import pytest
from urllib.parse import urlencode
from unittest.mock import patch
from django.urls import reverse
from django.test import Client
from fighthealthinsurance.models import LostStripeSession
from fighthealthinsurance.helpers.stripe_helpers import StripeWebhookHelper

EMAIL = "test@example.com"
LINE_ITEMS_METADATA = {
    "line_items": json.dumps([{"price": "price_123", "quantity": 1}])
}


def _make_lost_session(**overrides) -> LostStripeSession:
    """Create a non-professional LostStripeSession with rebuildable metadata."""
    defaults = dict(
        session_id="test_session_id",
        payment_type="non_professional_item",
        email=EMAIL,
        metadata=dict(LINE_ITEMS_METADATA),
    )
    defaults.update(overrides)
    return LostStripeSession.objects.create(**defaults)


@pytest.mark.django_db
def test_post_token_returns_stripe_url():
    """POST with a valid token returns the fresh Stripe checkout URL as JSON."""
    lost_session = _make_lost_session()
    with patch("stripe.checkout.Session.create") as mock_create:
        mock_create.return_value.url = "https://checkout.stripe.com/test"
        response = Client().post(
            reverse("complete_payment"),
            data=json.dumps({"token": lost_session.secure_token}),
            content_type="application/json",
        )
    assert response.status_code == 200
    assert response.json()["next_url"] == "https://checkout.stripe.com/test"


@pytest.mark.django_db
def test_get_token_with_format_json_returns_stripe_url():
    """GET ?format=json lets API callers opt into the JSON payload."""
    lost_session = _make_lost_session()
    with patch("stripe.checkout.Session.create") as mock_create:
        mock_create.return_value.url = "https://checkout.stripe.com/test"
        query = urlencode({"token": lost_session.secure_token, "format": "json"})
        response = Client().get(f"{reverse('complete_payment')}?{query}")
    assert response.status_code == 200
    assert response.json()["next_url"] == "https://checkout.stripe.com/test"


@pytest.mark.django_db
def test_get_token_from_browser_redirects_to_stripe():
    """A browser opening the recovery email link is 302-redirected to Stripe."""
    lost_session = _make_lost_session()
    with patch("stripe.checkout.Session.create") as mock_create:
        mock_create.return_value.url = "https://checkout.stripe.com/test"
        query = urlencode({"token": lost_session.secure_token})
        response = Client().get(f"{reverse('complete_payment')}?{query}")
    assert response.status_code == 302
    assert response["Location"] == "https://checkout.stripe.com/test"


@pytest.mark.django_db
def test_post_invalid_json_body_returns_400_json():
    """A malformed POST body still gets a JSON error, not an empty 500."""
    response = Client().post(
        reverse("complete_payment"),
        data="not-json",
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.django_db
def test_high_id_session_id_lookup_rejected():
    """Post-rollout row ids can't be used for lookup; only the token grants access."""
    high_id_session = _make_lost_session(pk=9999, session_id="high_id_stripe_session")
    response = Client().get(
        f"{reverse('complete_payment')}?session_id={high_id_session.pk}"
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_forged_token_rejected():
    """An unknown / forged token is rejected."""
    response = Client().get(f"{reverse('complete_payment')}?token=not-a-real-token")
    assert response.status_code == 400


@pytest.mark.django_db
def test_raw_stripe_session_id_string_rejected():
    """The raw Stripe session_id string is not a valid post-cutoff lookup."""
    high_id_session = _make_lost_session(pk=9999, session_id="high_id_stripe_session")
    response = Client().get(
        f"{reverse('complete_payment')}?session_id={high_id_session.session_id}"
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_legacy_session_id_link_still_works():
    """Pre-rollout emails used ?session_id=<row_id>; those old links still work."""
    legacy_session = _make_lost_session(pk=42, session_id="legacy_stripe_session")
    # auto_now_add ignores constructor values, so backdate created_at via UPDATE
    # into the pre-rollout window.
    LostStripeSession.objects.filter(pk=legacy_session.pk).update(
        created_at=datetime.datetime(2026, 5, 3, tzinfo=datetime.UTC)
    )
    with patch("stripe.checkout.Session.create") as mock_create:
        mock_create.return_value.url = "https://checkout.stripe.com/legacy"
        query = urlencode({"session_id": legacy_session.pk, "format": "json"})
        response = Client().get(f"{reverse('complete_payment')}?{query}")
    assert response.status_code == 200
    assert response.json()["next_url"] == "https://checkout.stripe.com/legacy"


@pytest.mark.django_db
@patch(
    "fighthealthinsurance.helpers.stripe_helpers.fhi_emails.send_checkout_session_expired"
)
def test_expired_checkout_webhook_persists_lost_session(mock_send_email):
    """An expired non-professional checkout webhook records a LostStripeSession."""
    session = {
        "metadata": {
            "payment_type": "non_professional_item",
            "line_items": LINE_ITEMS_METADATA["line_items"],
        },
        "customer_details": {"email": EMAIL},
    }
    StripeWebhookHelper.handle_checkout_session_expired(None, session)

    lost_session = LostStripeSession.objects.get(email=EMAIL)
    assert lost_session.payment_type == "non_professional_item"
    mock_send_email.assert_called_once()

"""Tests for fax_send_core precheck/resend semantics and the paid-fax dispatch.

These cover regressions found in the adversarial review of the Temporal fax
integration:

1. An explicit resend must clear the vendor idempotency marker, or the vendor
   step short-circuits and reports success without transmitting.
2. The precheck already-sent guard must only skip *successful* sends; a failed
   attempt (sent=True, fax_success=False) stays retryable, matching the
   original FaxActor behavior.
3. A paid fax whose Temporal dispatch fails must fall back to a Ray send when
   Temporal owns sending (the polling sweep is gated off), and must NOT fall
   back when Temporal is disabled (the sweep still owns the ~1h delay).
"""

import pytest
from unittest.mock import MagicMock, patch

from django.test import override_settings

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.fax_status import STATUS_ALREADY_SENT, STATUS_OK
from fighthealthinsurance.helpers.fax_helpers import SendFaxHelper
from fighthealthinsurance.helpers.stripe_helpers import StripeWebhookHelper
from fighthealthinsurance.models import Denial, FaxesToSend


@pytest.fixture
def test_denial(db):
    email = "fax_send_core_test@example.com"
    return Denial.objects.create(
        denial_text="Test denial for fax send core",
        hashed_email=Denial.get_hashed_email(email),
        raw_email=email,
        health_history="",
    )


def _make_fax(test_denial, **overrides):
    defaults = dict(
        hashed_email=test_denial.hashed_email,
        paid=True,
        email=test_denial.raw_email,
        name="Test User",
        appeal_text="Test appeal text",
        denial_id=test_denial,
        destination="4255551234",
        sent=False,
    )
    defaults.update(overrides)
    return FaxesToSend.objects.create(**defaults)


@pytest.mark.django_db
class TestResendClearsIdempotencyMarker:
    def test_resend_clears_vendor_send_completed(self, test_denial):
        """resend() to a new number must allow the vendor step to re-transmit."""
        fax = _make_fax(
            test_denial, sent=True, fax_success=True, vendor_send_completed=True
        )
        with patch(
            "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"
        ) as dispatch:
            SendFaxHelper.resend(
                fax_phone="8005559999",
                uuid=str(fax.uuid),
                hashed_email=fax.hashed_email,
            )
        fax.refresh_from_db()
        assert fax.vendor_send_completed is False
        assert fax.sent is False
        assert fax.destination == "8005559999"
        dispatch.assert_called_once()


@pytest.mark.django_db
class TestPrecheckAlreadySentGuard:
    def test_precheck_allows_retry_of_failed_send(self, test_denial):
        """A failed attempt (sent=True, fax_success=False) stays retryable."""
        fax = _make_fax(test_denial, sent=True, fax_success=False)
        assert fax_send_core.precheck_fax(fax) == STATUS_OK
        fax.refresh_from_db()
        assert fax.attempting_to_send_as_of is not None

    def test_precheck_skips_already_successful_send(self, test_denial):
        """A successful send is never re-attempted (duplicate-dispatch guard)."""
        fax = _make_fax(test_denial, sent=True, fax_success=True)
        assert fax_send_core.precheck_fax(fax) == STATUS_ALREADY_SENT


@pytest.mark.django_db
class TestPaidFaxDispatchFallback:
    @override_settings(TEMPORAL_ENABLED=True)
    @patch("fighthealthinsurance.fax_actor_ref.fax_actor_ref")
    @patch("fighthealthinsurance.temporal_client.dispatch_fax_send")
    def test_failed_temporal_dispatch_falls_back_to_ray(
        self, mock_dispatch, mock_actor_ref, test_denial
    ):
        """Temporal on + dispatch failure must not orphan a paid fax."""
        mock_dispatch.return_value = False
        mock_actor_ref.get = MagicMock()
        fax = _make_fax(test_denial)

        StripeWebhookHelper._handle_fax_payment(str(fax.uuid))

        mock_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            fax.hashed_email, str(fax.uuid)
        )

    @override_settings(TEMPORAL_ENABLED=False)
    @patch("fighthealthinsurance.fax_actor_ref.fax_actor_ref")
    @patch("fighthealthinsurance.temporal_client.dispatch_fax_send")
    def test_no_ray_fallback_when_temporal_disabled(
        self, mock_dispatch, mock_actor_ref, test_denial
    ):
        """Temporal off: the polling sweep owns the delayed send, no Ray call."""
        mock_dispatch.return_value = False
        mock_actor_ref.get = MagicMock()
        fax = _make_fax(test_denial)

        StripeWebhookHelper._handle_fax_payment(str(fax.uuid))

        mock_actor_ref.get.do_send_fax.remote.assert_not_called()

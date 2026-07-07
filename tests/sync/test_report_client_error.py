"""Tests for the ReportClientError diagnostic endpoint (rest_views).

The endpoint is a fire-and-forget client error sink: it must always return
204 and must never let the (best-effort) token-size annotation break the
request. The denial-context token breakdown is PHI-derived, so it is only
computed when the caller proves ownership with the (denial_id, email,
semi_sekret) triple -- an unauthenticated caller can still file an error
report but never causes an arbitrary denial to be loaded (IDOR).
"""

from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import Denial


class ReportClientErrorTest(APITestCase):
    """The unauthenticated client-error report endpoint."""

    EMAIL = "reporter@example.com"
    SEKRET = "sekret"

    def _url(self):
        return reverse("report_client_error")

    def _make_denial(self, **kwargs):
        """A denial owned by the (EMAIL, SEKRET) triple."""
        return Denial.objects.create(
            semi_sekret=self.SEKRET,
            hashed_email=Denial.get_hashed_email(self.EMAIL),
            **kwargs,
        )

    def _owner_payload(self, denial, **extra):
        payload = {
            "denial_id": str(denial.denial_id),
            "email": self.EMAIL,
            "semi_sekret": self.SEKRET,
            "error": "e",
        }
        payload.update(extra)
        return payload

    def _logged_error(self, mock_logger):
        """Join the positional args of the single logger.error call."""
        return " ".join(str(a) for a in mock_logger.error.call_args[0])

    def test_returns_204_for_unknown_denial(self):
        resp = self.client.post(
            self._url(),
            {"denial_id": "999999", "error": "boom"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_logs_context_tokens_for_owned_denial(self):
        denial = self._make_denial(denial_text="x" * 400, qa_context="y" * 80)
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            resp = self.client.post(
                self._url(), self._owner_payload(denial), format="json"
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        logged = self._logged_error(mock_logger)
        self.assertIn("context_tokens:", logged)
        self.assertIn("est_total=", logged)
        self.assertIn("denial_text=", logged)

    def test_owned_denial_with_no_context_reports_none_breakdown(self):
        denial = self._make_denial(denial_text="")
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            self.client.post(self._url(), self._owner_payload(denial), format="json")
        self.assertIn("est_total=0tok (none)", self._logged_error(mock_logger))

    def test_never_500_when_token_computation_raises(self):
        denial = self._make_denial(denial_text="x")
        with patch(
            "fighthealthinsurance.rest_views.context_utils.summarize_denial_context_tokens",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(
                self._url(), self._owner_payload(denial), format="json"
            )
        # The diagnostic failure is swallowed; the endpoint still 204s.
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_invalid_denial_id_reports_unauthorized(self):
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            resp = self.client.post(
                self._url(),
                {"denial_id": "not-an-int", "error": "e"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn("context_tokens: unauthorized", self._logged_error(mock_logger))

    # --- IDOR regression tests ------------------------------------------------

    def test_no_triple_does_not_load_denial(self):
        # A caller who supplies only a (sequential) denial_id -- no email /
        # semi_sekret -- must NOT cause the denial to be loaded or summarized.
        denial = self._make_denial(denial_text="x" * 400)
        with patch(
            "fighthealthinsurance.rest_views.context_utils.summarize_denial_context_tokens"
        ) as mock_summarize, patch(
            "fighthealthinsurance.rest_views.logger"
        ) as mock_logger:
            resp = self.client.post(
                self._url(),
                {"denial_id": str(denial.denial_id), "error": "e"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        mock_summarize.assert_not_called()
        self.assertIn(
            "context_tokens: unauthorized", self._logged_error(mock_logger)
        )

    def test_wrong_semi_sekret_is_unauthorized(self):
        denial = self._make_denial(denial_text="x" * 400)
        with patch(
            "fighthealthinsurance.rest_views.context_utils.summarize_denial_context_tokens"
        ) as mock_summarize, patch(
            "fighthealthinsurance.rest_views.logger"
        ) as mock_logger:
            resp = self.client.post(
                self._url(),
                self._owner_payload(denial, semi_sekret="wrong"),
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        mock_summarize.assert_not_called()
        self.assertIn(
            "context_tokens: unauthorized", self._logged_error(mock_logger)
        )

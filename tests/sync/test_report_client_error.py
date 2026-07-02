"""Tests for the ReportClientError diagnostic endpoint (rest_views).

The endpoint is a fire-and-forget client error sink: it must always return
204 and must never let the (best-effort) token-size annotation break the
request. These tests pin that invariant plus the happy-path logging.
"""

from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import Denial


class ReportClientErrorTest(APITestCase):
    """The unauthenticated client-error report endpoint."""

    def _url(self):
        return reverse("report_client_error")

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

    def test_logs_context_tokens_for_existing_denial(self):
        denial = Denial.objects.create(denial_text="x" * 400, qa_context="y" * 80)
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            resp = self.client.post(
                self._url(),
                {"denial_id": str(denial.denial_id), "error": "e"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        logged = self._logged_error(mock_logger)
        self.assertIn("context_tokens:", logged)
        self.assertIn("est_total=", logged)
        self.assertIn("denial_text=", logged)

    def test_existing_denial_with_no_context_reports_none_breakdown(self):
        denial = Denial.objects.create(denial_text="")
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            self.client.post(
                self._url(),
                {"denial_id": str(denial.denial_id), "error": "e"},
                format="json",
            )
        self.assertIn("est_total=0tok (none)", self._logged_error(mock_logger))

    def test_never_500_when_token_computation_raises(self):
        denial = Denial.objects.create(denial_text="x")
        with patch(
            "fighthealthinsurance.rest_views.context_utils.summarize_denial_context_tokens",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(
                self._url(),
                {"denial_id": str(denial.denial_id), "error": "e"},
                format="json",
            )
        # The diagnostic failure is swallowed; the endpoint still 204s.
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_invalid_denial_id_reports_unavailable(self):
        with patch("fighthealthinsurance.rest_views.logger") as mock_logger:
            resp = self.client.post(
                self._url(),
                {"denial_id": "not-an-int", "error": "e"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn("context_tokens: unavailable", self._logged_error(mock_logger))

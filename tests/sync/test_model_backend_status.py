"""Tests for the staff-only Model Backend Status page."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import (
    Denial,
    ModelBackendHealthCheckResult,
    ProposedAppeal,
)

User = get_user_model()


class ModelBackendStatusAccessTest(TestCase):
    def test_non_staff_redirected(self):
        response = self.client.get(reverse("model_backend_status"))
        self.assertEqual(response.status_code, 302)

    def test_staff_user_gets_200(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        response = self.client.get(reverse("model_backend_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Model Backend Status")


class ModelBackendStatusContentTest(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    def test_lists_catalog_models_even_when_unconfigured(self):
        """Every provider's catalog shows up (as not configured/disabled) so a
        missing key is visible instead of the model silently vanishing."""
        response = self.client.get(reverse("model_backend_status"))
        self.assertContains(response, "anthropic/claude-sonnet-4-6")
        self.assertContains(response, "azure-anthropic/claude-opus-4-8")

    def test_shows_latest_health_check_result(self):
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-old",
            model_name="anthropic/claude-sonnet-4-6",
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="FAIL_AUTH",
            ok=False,
            error="HTTP 401 [REDACTED]",
            started_at=timezone.now(),
        )
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-new",
            model_name="anthropic/claude-sonnet-4-6",
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="PASS",
            ok=True,
            latency_ms=842,
            started_at=timezone.now(),
        )
        response = self.client.get(reverse("model_backend_status"))
        # Latest row wins: PASS with latency, not the older auth failure.
        self.assertContains(response, "842 ms")
        self.assertContains(response, "PASS")

    def test_zero_latency_rendered_not_hidden(self):
        # int(ms) rounding can legitimately yield 0 for a very fast local
        # backend; the template must not collapse it to the em-dash.
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-zero",
            model_name="anthropic/claude-haiku-4-5",
            internal_name="claude-haiku-4-5-20251001",
            provider="Anthropic",
            category="PASS",
            ok=True,
            latency_ms=0,
            started_at=timezone.now(),
        )
        response = self.client.get(reverse("model_backend_status"))
        self.assertContains(response, "0 ms")

    def test_shows_last_stored_generation(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="generated appeal",
            model_name="anthropic/claude-sonnet-4-6",
        )
        response = self.client.get(reverse("model_backend_status"))
        # The page loads and the row for the model no longer reads
        # "none recorded" — a stored generation timestamp is present.
        self.assertEqual(response.status_code, 200)
        rows = [
            r
            for r in response.context["rows"]
            if r["model_name"] == "anthropic/claude-sonnet-4-6"
        ]
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["last_generation"])

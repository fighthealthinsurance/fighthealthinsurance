"""Tests for the staff-only Model Backend Status page."""

import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import (
    Denial,
    ModelBackendHealthCheckResult,
    ProposedAppeal,
)
from fighthealthinsurance.staff_views import ModelBackendStatusView

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

    # The Azure deployment lists honour an environment override; pin them to
    # the defaults so a developer's shell cannot change what the page lists.
    @patch.dict(os.environ, {"AZURE_ANTHROPIC_MODELS": "", "AZURE_OPENAI_MODELS": ""})
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


    def test_a_pick_does_not_count_as_a_stored_generation(self):
        """Choosing a draft inserts a chosen=True copy stamped with the
        original's model name and the pick time. Only the draft itself is
        evidence the backend generated something."""
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="the picked copy",
            model_name="anthropic/claude-sonnet-4-6",
            chosen=True,
        )
        response = self.client.get(reverse("model_backend_status"))
        rows = [
            r
            for r in response.context["rows"]
            if r["model_name"] == "anthropic/claude-sonnet-4-6"
        ]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["last_generation"])

    def test_a_persisted_not_configured_row_is_not_a_failure(self):
        """The deploy check persists its static classifications too. An
        unconfigured backend's NOT_CONFIGURED row used to render as a red
        failed health check."""
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-static",
            model_name="anthropic/claude-sonnet-4-6",
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="NOT_CONFIGURED",
            enabled=False,
            ok=False,
            error="ANTHROPIC_API_KEY not set",
            started_at=timezone.now(),
        )
        response = self.client.get(reverse("model_backend_status"))
        self.assertNotContains(response, 'pill-fail">NOT_CONFIGURED')
        self.assertContains(response, "not checked")

    def test_latest_check_is_found_for_every_model(self):
        """Per-model lookup: one backend's many rows cannot push another's
        only row out of a newest-N window."""
        for i in range(5):
            ModelBackendHealthCheckResult.objects.create(
                run_id=f"run-{i}",
                model_name="anthropic/claude-sonnet-4-6",
                internal_name="claude-sonnet-4-6",
                provider="Anthropic",
                category="PASS",
                ok=True,
                started_at=timezone.now(),
            )
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-old",
            model_name="anthropic/claude-haiku-4-5",
            internal_name="claude-haiku-4-5-20251001",
            provider="Anthropic",
            category="FAIL_AUTH",
            ok=False,
            started_at=timezone.now(),
        )
        latest = ModelBackendStatusView._latest_check_by_model(
            ["anthropic/claude-sonnet-4-6", "anthropic/claude-haiku-4-5"]
        )
        self.assertEqual(latest["anthropic/claude-haiku-4-5"].category, "FAIL_AUTH")
        self.assertEqual(latest["anthropic/claude-sonnet-4-6"].run_id, "run-4")

    def test_summary_counts_enabled_backends_that_passed(self):
        response = self.client.get(reverse("model_backend_status"))
        self.assertIn("enabled_count", response.context)
        self.assertIn("healthy_count", response.context)
        self.assertContains(response, "passed the most recent health check")


class ModelBackendStatusRowTest(SimpleTestCase):
    """Row assembly for the states the table has to tell apart."""

    @staticmethod
    def _entry(**overrides):
        fields = dict(
            provider="Perplexity",
            model_name="sonar",
            internal_name="sonar",
            category="FAIL_OTHER",
            enabled=True,
            error="",
            ui_registered=True,
            reporting_registered=True,
            context_only=False,
        )
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_a_context_only_backend_is_marked(self):
        row = ModelBackendStatusView._row(
            self._entry(context_only=True), None, None, None
        )
        self.assertTrue(row["context_only"])
        self.assertIsNone(row["in_default_fanout"])

    def test_an_external_outside_the_fanout_is_marked(self):
        row = ModelBackendStatusView._row(
            self._entry(model_name="google/gemma-4-26B-A4B-it", provider="DeepInfra"),
            None,
            None,
            False,
        )
        self.assertIs(row["in_default_fanout"], False)

    def test_the_fanout_annotation_only_covers_external_generation_models(self):
        internal = SimpleNamespace(external=False, context_only=False)
        external = SimpleNamespace(external=True, context_only=False)
        citations = SimpleNamespace(external=True, context_only=True)
        checkable = [
            (self._entry(model_name="fhi-legacy"), internal),
            (self._entry(model_name="azure-openai/gpt-5.5"), external),
            (self._entry(model_name="sonar", context_only=True), citations),
        ]
        with patch(
            "fighthealthinsurance.ml.ml_router.ml_router.best_external_models",
            return_value=[external],
        ):
            fanout = ModelBackendStatusView._default_fanout_by_name(checkable)
        self.assertEqual(fanout, {"azure-openai/gpt-5.5": True})

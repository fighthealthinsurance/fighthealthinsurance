"""Tests for the staff-only Model Backend Status page."""

import os
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.ml import health_status as health_status_module
from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml import model_health_check as mhc
from fighthealthinsurance.ml.health_status import _model_key, health_status
from fighthealthinsurance.ml.ml_models import (
    AlphaRemoteInternal,
    RateLimitedRemoteOpenLike,
    RemoteModelLike,
    RemoteOpenLike,
)
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.models import (
    Denial,
    ModelBackendHealthCheckResult,
    ProposedAppeal,
)
from fighthealthinsurance.staff_views import ModelBackendStatusView

User = get_user_model()

# Settings for each provider, with hosts that can never resolve, so a test
# that did reach the network would fail rather than call something real.
ANTHROPIC = {"ANTHROPIC_API_KEY": "test-anthropic-key"}
AZURE_CLAUDE = {
    "AZURE_ANTHROPIC_API_KEY": "test-azure-claude-key",
    "AZURE_ANTHROPIC_ENDPOINT": "https://example.invalid/anthropic",
}
AZURE_OPENAI = {
    "AZURE_OPENAI_API_KEY": "test-azure-openai-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.invalid/openai/v1",
}
DEEPINFRA = {"DEEPINFRA_API": "test-deepinfra-key"}
PERPLEXITY = {"PERPLEXITY_API": "test-perplexity-key"}
ALPHA = {
    "ALPHA_HEALTH_BACKEND_HOST": "alpha.example.invalid",
    "ALPHA_HEALTH_BACKEND_MODEL": "/models/fhi-local",
}
LEGACY = {"HEALTH_BACKEND_HOST": "legacy.example.invalid"}
# A second internal server set to the same model path as ALPHA, so both
# register under the one name "fhi-local".
NEW_SAME_PATH = {
    "NEW_HEALTH_BACKEND_HOST": "new.example.invalid",
    "NEW_HEALTH_BACKEND_MODEL": "/models/fhi-local",
}

GEMMA = "google/gemma-4-26B-A4B-it"
DEEPSEEK = "deepseek-ai/DeepSeek-V4-Pro"


def _is_routing_setting(name: str) -> bool:
    """Whether an environment variable changes which backends register, how
    they rank, or which deploy the page thinks it is."""
    return (
        name.startswith(("ANTHROPIC_", "AZURE_", "DEEPINFRA_", "PERPLEXITY_"))
        or name.endswith(("_BACKEND_HOST", "_BACKEND_PORT", "_BACKEND_MODEL"))
        or name
        in (
            "ENABLED_REMOTE_MODELS",
            "FORCE_MODEL",
            "FHI_DEPLOYMENT_ID",
            "FHI_RELEASE",
            "FHI_VERSION",
        )
    )


class StatusPageTestCase(TestCase):
    """A staff login and a router built only from the settings a test names.

    tox passes the developer's shell through (passenv = *), so a provider
    key or backend host in it would otherwise reshape the catalog and the
    routing these tests assert on.
    """

    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in [n for n in os.environ if _is_routing_setting(n)]:
            del os.environ[name]
        old_router = ml_router_module._ml_router_instance
        ml_router_module._ml_router_instance = None
        self.addCleanup(setattr, ml_router_module, "_ml_router_instance", old_router)
        # Sweep results are keyed by object id, and a new backend can reuse a
        # dead one's id, so start every test with no sweep results at all.
        sweep = patch.object(health_status, "_health_map", {})
        sweep.start()
        self.addCleanup(sweep.stop)

    def configure(self, **env):
        os.environ.update(env)
        # Rebuilt on the next request, from these settings.
        ml_router_module._ml_router_instance = None

    def get_page(self):
        response = self.client.get(reverse("model_backend_status"))
        self.assertEqual(response.status_code, 200)
        return response

    def row(self, response, name):
        rows = [r for r in response.context["rows"] if r["model_name"] == name]
        self.assertEqual(len(rows), 1, name)
        return rows[0]

    # The table's columns after Model, where a row search starts.
    COLUMNS = (
        "Kind",
        "Routing on this pod",
        "Config",
        "Last health check",
        "Last stored generation",
    )

    def cell(self, response, name, column):
        """The HTML of one cell in the table row of the model ``name``."""
        html = response.content.decode()
        row = html[html.index(f'<div class="mono">{name}</div>') :]
        row = row[: row.index("</tr>")]
        return row.split("<td>")[1 + self.COLUMNS.index(column)]

    @staticmethod
    def labels(row):
        return [role.label for role in row["roles"]]

    @staticmethod
    def plan(response, title):
        for plan in response.context["routing"].paths:
            if plan.title == title:
                return plan
        raise AssertionError(f"no path called {title}")

    @staticmethod
    def names(entries):
        return [e.name for e in entries]


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


class ModelBackendStatusContentTest(StatusPageTestCase):
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
        response = self.get_page()
        self.assertIsNone(
            self.row(response, "anthropic/claude-sonnet-4-6")["last_generation"]
        )

    def test_a_still_unconfigured_backends_classification_is_not_a_check(self):
        """The deploy check persists its static classifications too. While the
        backend is still unconfigured, its NOT_CONFIGURED row is not a failed
        health check, and the Config column keeps the detail."""
        name = "anthropic/claude-sonnet-4-6"
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-static",
            model_name=name,
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="NOT_CONFIGURED",
            enabled=False,
            ok=False,
            error="ANTHROPIC_API_KEY not set",
            started_at=timezone.now(),
        )
        response = self.get_page()
        self.assertFalse(self.row(response, name)["show_check"])
        health = self.cell(response, name, "Last health check")
        self.assertIn("not checked", health)
        self.assertNotIn("NOT_CONFIGURED", health)
        self.assertIn(
            "ANTHROPIC_API_KEY not set", self.cell(response, name, "Config")
        )


class ModelBackendStatusRoutingTest(StatusPageTestCase):
    """The routing panel and columns match what the router would pick."""

    def test_azure_and_anthropic_top_three(self):
        self.configure(**ANTHROPIC, **AZURE_CLAUDE, **AZURE_OPENAI)
        response = self.get_page()
        routing = response.context["routing"]
        self.assertEqual(
            [name for name, _quality in routing.top_external],
            [
                "azure-openai/gpt-5.5",
                "azure-anthropic/claude-fable-5",
                "anthropic/claude-opus-4-8",
            ],
        )
        for name, rank in (
            ("azure-openai/gpt-5.5", 1),
            ("azure-anthropic/claude-fable-5", 2),
        ):
            row = self.row(response, name)
            self.assertEqual(
                (row["quality"], row["tier"], row["kind"], row["top_external_rank"]),
                (100, "frontier", "external", rank),
            )
            self.assertIn("Appeals: backup (external allowed)", self.labels(row))
            self.assertIn("Chat: fan-out (external allowed)", self.labels(row))
        # Azure's Opus ties Anthropic's at 98 and loses on cost (135 vs 130),
        # so it is registered but no path picks it.
        azure_opus = self.row(response, "azure-anthropic/claude-opus-4-8")
        self.assertTrue(azure_opus["routed"])
        self.assertEqual((azure_opus["quality"], azure_opus["tier"]), (98, "premium"))
        self.assertIsNone(azure_opus["top_external_rank"])
        self.assertEqual(azure_opus["roles"], [])
        self.assertContains(response, "registered, not picked by any path")
        self.assertContains(response, "top external #1")
        self.assertContains(response, "Top 3 external models")

    def test_deepinfra_models_with_quality_and_roles(self):
        self.configure(**DEEPINFRA)
        response = self.get_page()
        routing = response.context["routing"]
        self.assertEqual(
            [name for name, _quality in routing.top_external], [DEEPSEEK, GEMMA]
        )
        gemma = self.row(response, GEMMA)
        self.assertEqual((gemma["quality"], gemma["kind"]), (80, "external"))
        self.assertEqual(gemma["tier"], "")
        # No internal backend, so the hosted generalist is the whole summary
        # list, and only when external models are allowed.
        self.assertIn("Summaries: 1st (external allowed)", self.labels(gemma))
        self.assertIn("Questions: fan-out (external allowed)", self.labels(gemma))
        deepseek = self.row(response, DEEPSEEK)
        self.assertEqual(deepseek["quality"], 92)
        self.assertFalse(
            [label for label in self.labels(deepseek) if "Summaries" in label]
        )
        summaries = self.plan(response, "Summaries")
        self.assertEqual(summaries.internal_only, [])
        self.assertEqual(self.names(summaries.external_allowed), [GEMMA])

    def test_sonar_is_context_only(self):
        self.configure(**PERPLEXITY, **DEEPINFRA)
        response = self.get_page()
        sonar = self.row(response, "sonar")
        self.assertEqual(sonar["kind"], "context only")
        self.assertTrue(sonar["routed"])
        self.assertIsNone(sonar["top_external_rank"])
        self.assertNotIn(
            "sonar", [name for name, _q in response.context["routing"].top_external]
        )
        self.assertEqual(self.labels(sonar), ["Questions: fan-out (external allowed)"])

    def test_a_context_only_backend_has_no_generations_to_record(self):
        """sonar builds citations and never drafts, so its empty "Last stored
        generation" says so instead of reading "none recorded"."""
        self.configure(**PERPLEXITY)
        response = self.get_page()
        self.assertTrue(self.row(response, "sonar")["context_only"])
        self.assertIn(
            "n/a (citations only)",
            self.cell(response, "sonar", "Last stored generation"),
        )

    def test_context_only_is_known_when_routing_fails(self):
        """Read off the registered instance when the traits are unavailable."""
        self.configure(**PERPLEXITY)
        with patch.object(
            MLRouter,
            "get_chat_backends_with_fallback",
            side_effect=RuntimeError("router broke"),
        ):
            response = self.get_page()
        sonar = self.row(response, "sonar")
        self.assertFalse(sonar["has_traits"])
        self.assertTrue(sonar["context_only"])

    def test_internal_backends(self):
        self.configure(**ALPHA, **LEGACY)
        response = self.get_page()
        alpha = self.row(response, "fhi-local")
        self.assertEqual((alpha["quality"], alpha["kind"]), (210, "internal"))
        self.assertEqual(alpha["tier"], "")
        for label in (
            "Appeals: primary",
            "Appeals: best-internal hint",
            "Chat: lead, 3 calls",
            "Questions: fan-out",
            "Summaries: 1st",
        ):
            self.assertIn(label, self.labels(alpha))
        # The strongest internal leads chat, questions and summaries either way.
        for title in ("Chat", "Appeal questions", "Summaries"):
            plan = self.plan(response, title)
            self.assertEqual(self.names(plan.internal_only)[:1], ["fhi-local"])
            self.assertEqual(self.names(plan.external_allowed)[:1], ["fhi-local"])
        # The lead is listed twice up front and again among the internals,
        # and the page says how many calls that makes.
        lead = self.plan(response, "Chat").internal_only[0]
        self.assertEqual((lead.note, lead.calls), ("lead", 3))
        self.assertContains(response, "lead, 3 calls")
        self.assertContains(response, "so it usually gets three calls")
        self.assertNotContains(response, "doubled lead gets two calls")
        legacy = self.row(response, "fhi-legacy")
        self.assertEqual(
            (legacy["quality"], legacy["kind"]), (101, "appeal-only fine-tune")
        )
        # The backup pass never repeats a model the primary pass asked.
        self.assertEqual(self.labels(legacy), ["Appeals: primary"])
        self.assertContains(response, "appeal-only fine-tune")

    def test_backends_sharing_a_name_show_only_their_own_roles(self):
        """ALPHA and NEW set to the same model path both register as
        fhi-local. With ALPHA marked down by the sweep, the router picks only
        NEW for chat, questions and summaries, so only NEW's row may say so.
        Appeals go by name and try both in turn, so both rows keep those."""
        self.configure(**ALPHA, **NEW_SAME_PATH)
        router = ml_router_module._get_ml_router()
        backends = router.models_by_name["fhi-local"]
        self.assertEqual(len(backends), 2)
        alpha = next(m for m in backends if isinstance(m, AlphaRemoteInternal))
        health_status._health_map[_model_key(alpha)] = False

        response = self.get_page()
        rows = {
            r["provider"]: r
            for r in response.context["rows"]
            if r["model_name"] == "fhi-local"
        }
        self.assertEqual(set(rows), {"FHI Internal", "FHI Internal (alpha)"})
        picked = self.labels(rows["FHI Internal"])
        skipped = self.labels(rows["FHI Internal (alpha)"])
        for label in ("Chat: lead, 3 calls", "Questions: fan-out", "Summaries: 1st"):
            self.assertIn(label, picked)
        self.assertFalse(
            [
                label
                for label in skipped
                if label.startswith(("Chat", "Questions", "Summaries"))
            ]
        )
        for label in ("Appeals: primary", "Appeals: best-internal hint"):
            self.assertIn(label, picked)
            self.assertIn(label, skipped)
        self.assertEqual(
            self.names(self.plan(response, "Summaries").internal_only),
            ["fhi-local on FHI Internal"],
        )
        self.assertContains(response, "2 backends, tried in turn")

    def test_unregistered_rows_read_traits_without_being_routed(self):
        response = self.get_page()
        fable = self.row(response, "azure-anthropic/claude-fable-5")
        self.assertFalse(fable["routed"])
        self.assertEqual((fable["quality"], fable["tier"]), (100, "frontier"))
        self.assertEqual(fable["roles"], [])
        self.assertContains(response, "not routed on this pod")

    def test_rows_are_ordered_by_how_this_pod_uses_them(self):
        self.configure(
            **ALPHA,
            **LEGACY,
            **ANTHROPIC,
            **AZURE_CLAUDE,
            **DEEPINFRA,
            **PERPLEXITY,
            # A key without its endpoint: a failing configuration.
            AZURE_OPENAI_API_KEY="test-azure-openai-key",
        )
        response = self.get_page()
        self.assertEqual(
            [r["model_name"] for r in response.context["rows"]],
            [
                # Registered internal, strongest first.
                "fhi-local",
                "fhi-legacy",
                # The top 3 external, by rank.
                "azure-anthropic/claude-fable-5",
                "anthropic/claude-opus-4-8",
                "azure-anthropic/claude-opus-4-8",
                # Other registered externals: quality, then provider.
                "anthropic/claude-sonnet-4-6",
                DEEPSEEK,
                "anthropic/claude-haiku-4-5",
                GEMMA,
                # Context only.
                "sonar",
                # Failing configuration.
                "azure-openai/gpt-5.5",
                # Not configured.
                "fhi-2025-may-0.3-float16-q8-vllm-compressed",
            ],
        )

    def test_failing_config_is_not_called_enabled(self):
        self.configure(AZURE_OPENAI_API_KEY="test-azure-openai-key")
        response = self.get_page()
        gpt = self.row(response, "azure-openai/gpt-5.5")
        self.assertTrue(gpt["enabled"])
        self.assertTrue(gpt["config_failing"])
        self.assertContains(
            response, '<span class="pill pill-fail">FAIL_MISSING_CREDENTIALS</span>'
        )
        self.assertNotContains(response, '<span class="pill pill-ok">enabled</span>')

    def test_force_model_banner(self):
        self.configure(**ALPHA, FORCE_MODEL="fhi-local")
        response = self.get_page()
        routing = response.context["routing"]
        self.assertEqual(routing.force_model, "fhi-local")
        self.assertTrue(routing.force_model_registered)
        self.assertFalse(routing.force_model_external)
        self.assertContains(response, "<strong>FORCE_MODEL</strong> is set to")
        self.assertContains(response, "use only it, whether or")
        self.assertNotContains(response, "No model by that name")
        self.assertNotContains(response, "It is an external model.")

    def test_a_forced_internal_model_leaves_the_backup_pass_empty(self):
        """The primary pass already asks the forced model, and the backup
        pass never repeats one, so the banner and the lists say it gets
        nothing."""
        self.configure(**ALPHA, FORCE_MODEL="fhi-local")
        response = self.get_page()
        self.assertContains(response, "The backup pass gets no model")
        backup = self.plan(response, "Appeals, backup pass")
        self.assertEqual((backup.internal_only, backup.external_allowed), ([], []))
        self.assertEqual(
            self.names(self.plan(response, "Appeals, primary pass").internal_only),
            ["fhi-local"],
        )

    def test_force_model_banner_names_an_unregistered_model(self):
        self.configure(**ALPHA, FORCE_MODEL="no-such-model")
        response = self.get_page()
        self.assertContains(response, "No model by that name is registered here")
        # generate_text_backend_names returns nothing for an unknown forced name.
        self.assertEqual(self.plan(response, "Appeals, primary pass").internal_only, [])
        # _get_forced_models finds nothing, so chat and questions route as normal.
        self.assertContains(response, "chat and appeal questions ignore")
        self.assertEqual(
            self.names(self.plan(response, "Chat").internal_only), ["fhi-local"]
        )

    def test_a_forced_external_model_is_skipped_with_external_off(self):
        """The banner and the lists agree on what FORCE_MODEL does to a
        person who has not allowed external models."""
        sonnet = "anthropic/claude-sonnet-4-6"
        self.configure(**ALPHA, **ANTHROPIC, FORCE_MODEL=sonnet)
        response = self.get_page()
        self.assertTrue(response.context["routing"].force_model_external)
        self.assertContains(response, "It is an external model.")
        self.assertContains(response, "chat and appeal questions skip it and route")
        self.assertContains(response, "the backup pass gets no model either")
        self.assertNotContains(response, "use only it, whether or")

        # External off: chat and questions use their normal internal lists.
        chat = self.plan(response, "Chat")
        self.assertEqual(self.names(chat.internal_only), ["fhi-local"])
        self.assertEqual(chat.internal_only[0].calls, 3)
        questions = self.plan(response, "Appeal questions")
        self.assertEqual(self.names(questions.internal_only), ["fhi-local"])
        # External on: only the forced model.
        self.assertEqual(self.names(chat.external_allowed), [sonnet])
        self.assertEqual(self.names(questions.external_allowed), [sonnet])
        # Appeals: the internal-only primary pass gets nothing, and so does
        # the backup pass with external off.
        primary = self.plan(response, "Appeals, primary pass")
        self.assertEqual((primary.internal_only, primary.external_allowed), ([], []))
        backup = self.plan(response, "Appeals, backup pass")
        self.assertEqual(backup.internal_only, [])
        self.assertEqual(self.names(backup.external_allowed), [sonnet])
        # Summaries ignore it.
        self.assertEqual(
            self.names(self.plan(response, "Summaries").internal_only), ["fhi-local"]
        )

    def test_enabled_remote_models_banner(self):
        self.configure(**ANTHROPIC, ENABLED_REMOTE_MODELS="anthropic/claude-haiku-4-5")
        response = self.get_page()
        self.assertEqual(
            response.context["routing"].enabled_remote_models,
            ["anthropic/claude-haiku-4-5"],
        )
        self.assertContains(response, "<strong>ENABLED_REMOTE_MODELS</strong>")
        self.assertEqual(
            self.row(response, "anthropic/claude-sonnet-4-6")["config_category"],
            "DISABLED",
        )

    def test_no_banners_when_unset(self):
        response = self.get_page()
        self.assertNotContains(response, "<strong>FORCE_MODEL</strong>")
        self.assertNotContains(response, "<strong>ENABLED_REMOTE_MODELS</strong>")

    def test_page_never_calls_a_model_the_network_or_the_sweep(self):
        self.configure(**ALPHA, **LEGACY, **DEEPINFRA, **PERPLEXITY, **ANTHROPIC)
        forbidden = {
            "_infer_no_context": patch.object(RemoteModelLike, "_infer_no_context"),
            "probe": patch.object(RemoteModelLike, "probe"),
            "RemoteOpenLike._infer": patch.object(RemoteOpenLike, "_infer"),
            "RateLimitedRemoteOpenLike._infer": patch.object(
                RateLimitedRemoteOpenLike, "_infer"
            ),
            # The /models probe that internal and DeepInfra backends inherit.
            "model_is_ok": patch.object(RemoteOpenLike, "model_is_ok"),
            "summarize": patch.object(MLRouter, "summarize"),
            "get_snapshot": patch.object(health_status, "get_snapshot"),
            "compute_model_health_details": patch.object(
                health_status_module, "compute_model_health_details"
            ),
            "requests.get": patch("requests.get"),
        }
        mocks = {}
        for label, patcher in forbidden.items():
            mocks[label] = patcher.start()
            mocks[label].side_effect = AssertionError(f"the page called {label}")
            self.addCleanup(patcher.stop)

        # The Test settings turn the background health sweep off, so this
        # covers the page's own work. On a pod, reading cached health can
        # start the sweep, which probes in a background thread.
        response = self.get_page()

        called = [label for label, mock in mocks.items() if mock.called]
        self.assertEqual(called, [])
        # And routing really ran; it wasn't swallowed by the fallback.
        self.assertNotContains(response, "Routing unavailable")
        self.assertIn(
            "Chat: lead, 3 calls", self.labels(self.row(response, "fhi-local"))
        )

    def test_routing_failure_degrades_to_a_note(self):
        self.configure(**ALPHA)
        with patch.object(
            MLRouter,
            "get_chat_backends_with_fallback",
            side_effect=RuntimeError("router broke"),
        ):
            response = self.get_page()
        self.assertContains(response, "Routing unavailable")
        self.assertIsNone(response.context["routing"])
        alpha = self.row(response, "fhi-local")
        self.assertFalse(alpha["has_traits"])
        self.assertEqual(alpha["roles"], [])
        # The health rows still render.
        self.assertContains(response, "azure-anthropic/claude-fable-5")


class ModelBackendStatusFreshnessTest(StatusPageTestCase):
    """Each health row says which deploy it came from, and whether it still
    describes this one."""

    MODEL = "anthropic/claude-sonnet-4-6"

    def check_row(self, **fields):
        values = dict(
            run_id="run-1",
            model_name=self.MODEL,
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="PASS",
            ok=True,
            latency_ms=120,
            deployment_id="v-old",
            environment=mhc.environment_name(),
            enabled=True,
            started_at=timezone.now(),
        )
        values.update(fields)
        return ModelBackendHealthCheckResult.objects.create(**values)

    def test_row_from_an_older_deploy_is_flagged(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-new")
        self.check_row(deployment_id="v-old")
        response = self.get_page()
        row = self.row(response, self.MODEL)
        self.assertTrue(row["stale_deployment"])
        self.assertFalse(row["stale_environment"])
        self.assertContains(response, "older than current deploy")

    def test_row_from_this_deploy_is_not_flagged(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-new")
        self.check_row(deployment_id="v-new")
        response = self.get_page()
        self.assertFalse(self.row(response, self.MODEL)["stale_deployment"])
        self.assertNotContains(response, "older than current deploy")

    def test_unversioned_deploy_flags_nothing(self):
        # No release variable: the id is the hourly fallback stamp, which says
        # nothing about which deploy a row came from.
        self.configure(**ANTHROPIC)
        self.check_row(deployment_id="v-old")
        response = self.get_page()
        self.assertFalse(response.context["deployment_is_versioned"])
        self.assertFalse(self.row(response, self.MODEL)["stale_deployment"])
        self.assertNotContains(response, "older than current deploy")
        self.assertContains(response, "This pod has no release id")

    def test_row_from_another_environment_is_flagged(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-new")
        self.check_row(deployment_id="v-new", environment="SomewhereElse")
        response = self.get_page()
        row = self.row(response, self.MODEL)
        self.assertTrue(row["stale_environment"])
        self.assertFalse(row["stale_deployment"])
        self.assertContains(response, "other environment")

    def test_deploy_and_environment_are_rendered(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-current-7")
        self.check_row(deployment_id="v-row-3", environment="RowEnv")
        response = self.get_page()
        self.assertContains(response, "v-row-3")
        self.assertContains(response, "RowEnv")
        self.assertContains(response, "v-current-7")
        self.assertEqual(response.context["current_deployment_id"], "v-current-7")

    def test_enabled_state_change_is_flagged(self):
        # Checked while not configured; configured now.
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-old")
        self.check_row(enabled=False, ok=False, category="NOT_CONFIGURED")
        response = self.get_page()
        self.assertTrue(self.row(response, self.MODEL)["config_changed"])
        self.assertContains(response, "config changed since")

    def test_a_classification_row_is_not_shown_as_a_failure(self):
        """Checked while not configured, configured now: the row still shows,
        flagged, as the classification it was rather than a failed check."""
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-old")
        self.check_row(enabled=False, ok=False, category="NOT_CONFIGURED")
        health = self.cell(self.get_page(), self.MODEL, "Last health check")
        self.assertIn('<span class="pill pill-off">NOT_CONFIGURED</span>', health)
        self.assertNotIn("pill-fail", health)
        self.assertIn("config changed since", health)

    def test_matching_enabled_state_is_not_flagged(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-old")
        self.check_row()
        response = self.get_page()
        self.assertFalse(self.row(response, self.MODEL)["config_changed"])
        self.assertNotContains(response, "config changed since")

    def test_an_older_failure_survives_many_newer_rows_of_another_model(self):
        """Each model's latest row is read on its own. A cap on the newest
        rows across all models let a model checked often push another
        model's failing check off the page."""
        self.configure(**ANTHROPIC)
        self.check_row(category="FAIL_AUTH", ok=False, latency_ms=None)
        other = "anthropic/claude-haiku-4-5"
        ModelBackendHealthCheckResult.objects.bulk_create(
            ModelBackendHealthCheckResult(
                run_id=f"run-{i}",
                model_name=other,
                internal_name="claude-haiku-4-5-20251001",
                provider="Anthropic",
                category="PASS",
                ok=True,
                latency_ms=i,
                started_at=timezone.now(),
            )
            for i in range(2001)
        )
        response = self.get_page()
        self.assertEqual(
            self.row(response, self.MODEL)["last_check"].category, "FAIL_AUTH"
        )
        self.assertEqual(self.row(response, other)["last_check"].latency_ms, 2000)

    def test_latest_checks_take_two_queries(self):
        self.check_row()
        with self.assertNumQueries(2):
            latest = ModelBackendStatusView._latest_check_by_model([self.MODEL])
        self.assertEqual(list(latest), [self.MODEL])

    def test_healthy_count_is_rendered(self):
        self.configure(**ANTHROPIC)
        self.check_row()
        response = self.get_page()
        self.assertEqual(response.context["healthy_count"], 1)
        enabled = response.context["enabled_count"]
        self.assertEqual(
            enabled, sum(1 for r in response.context["rows"] if r["enabled"])
        )
        self.assertGreater(enabled, 1)
        self.assertContains(
            response, f"1 of {enabled} enabled backends passed their latest check"
        )

    def test_a_pass_from_before_a_backend_was_turned_off_is_not_healthy(self):
        """The count is of the backends this pod has enabled: an old PASS for
        one that is now unconfigured is not health."""
        self.check_row()
        response = self.get_page()
        self.assertFalse(self.row(response, self.MODEL)["enabled"])
        self.assertEqual(response.context["healthy_count"], 0)


class ModelBackendStatusLayoutTest(StatusPageTestCase):
    """Six columns that fit a 1280px window, with every fact still shown."""

    TEMPLATE = (
        Path(__file__).resolve().parents[2]
        / "fighthealthinsurance/templates/model_backend_status.html"
    )

    def test_six_columns_inside_a_scroll_box(self):
        response = self.get_page()
        html = response.content.decode()
        table = html[html.index('<div class="status-wrap">') :]
        table = table[: table.index("</thead>")]
        self.assertEqual(
            re.findall(r"<th>([^<]*)</th>", table),
            [
                "Model",
                "Kind",
                "Routing on this pod",
                "Config",
                "Last health check",
                "Last stored generation",
            ],
        )
        self.assertIn(".status-wrap { overflow-x: auto;", html)

    def test_registry_state_is_a_note_under_config(self):
        self.configure(**ANTHROPIC)
        response = self.get_page()
        # Not configured: neither registered in router nor in reporting.
        self.assertContains(response, "not registered in router or in reporting")
        # Registered and reported: no note.
        sonnet = self.row(response, "anthropic/claude-sonnet-4-6")
        self.assertTrue(sonnet["ui_registered"] and sonnet["reporting_registered"])

    def test_health_facts_share_one_cell(self):
        self.configure(**ANTHROPIC, FHI_DEPLOYMENT_ID="v-new")
        ModelBackendHealthCheckResult.objects.create(
            run_id="run-1",
            model_name="anthropic/claude-sonnet-4-6",
            internal_name="claude-sonnet-4-6",
            provider="Anthropic",
            category="FAIL_TIMEOUT",
            ok=False,
            latency_ms=842,
            error="timed out after 8s",
            deployment_id="v-old",
            environment="RowEnv",
            enabled=True,
            started_at=timezone.now(),
        )
        cell = self.cell(
            self.get_page(), "anthropic/claude-sonnet-4-6", "Last health check"
        )
        for fact in (
            "FAIL_TIMEOUT",
            "842 ms",
            "UTC",
            "v-old",
            "RowEnv",
            "older than current deploy",
            "other environment",
            "timed out after 8s",
        ):
            self.assertIn(fact, cell)

    def test_font_sizes_do_not_compound_below_12px(self):
        """An em size inside a cell that is itself sized in em compounds:
        0.8em chips in 0.92em cells came out at 11.8px. Every size on this
        page is in rem, so each one is what it says wherever it sits."""
        style = self.TEMPLATE.read_text()
        style = style[style.index("<style>") : style.index("</style>")]
        sizes = re.findall(r"font-size:\s*([^;}]+)", style)
        self.assertTrue(sizes)
        for size in sizes:
            match = re.fullmatch(r"([\d.]+)rem", size.strip())
            self.assertIsNotNone(match, f"font-size {size} is not in rem")
            self.assertGreaterEqual(float(match.group(1)) * 16, 12, size)
        # A lone generic monospace family shrinks to the 13px monospace
        # default, which takes code in a 14.4px note down to 11.7px.
        self.assertIn("code, .mono { font-family: monospace, monospace; }", style)


class ModelBackendStatusWordingTest(StatusPageTestCase):
    def test_old_wording_is_gone(self):
        response = self.get_page()
        for old in (
            "In selection UI",
            "Every configured model backend",
            "No model backends are configured in this environment",
        ):
            self.assertNotContains(response, old)
        self.assertIn("configured or not", ModelBackendStatusView.__doc__)
        self.assertContains(response, "Registered in router")
        self.assertContains(response, "configured or not")
        self.assertContains(response, "Routing as seen by this pod")
        self.assertContains(response, "does not mean any request path picks it")

"""Tests for the deployment-time model-backend health check.

Covers: backend enumeration (enabled/disabled/not-configured/missing
credentials), invocation categorization (auth / model-not-found / rate-limit /
timeout / network / malformed / success), error sanitization, once-per-
deployment leader election, the single consolidated alert email (and its
suppression in test/dev environments and on healthy runs), result
persistence, registry cross-checks (Claude registered for UI + reporting),
and the check_model_backends management command exit codes.
"""

import asyncio
import os
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from django.core.management import call_command

from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml import model_health_check as mhc


@pytest.fixture
def fresh_router(monkeypatch):
    """Reset the lazy MLRouter singleton so env changes take effect, and
    restore the previous instance afterwards."""
    old = ml_router_module._ml_router_instance
    ml_router_module._ml_router_instance = None
    yield
    ml_router_module._ml_router_instance = old


def _clear_provider_env(monkeypatch):
    """Remove every provider env var so enumeration starts from a clean slate."""
    for var in (
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "DEEPINFRA_API",
        "PERPLEXITY_API",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_ANTHROPIC_API_KEY",
        "AZURE_ANTHROPIC_ENDPOINT",
        "HEALTH_BACKEND_HOST",
        "HEALTH_BACKUP_BACKEND_HOST",
        "NEW_HEALTH_BACKEND_HOST",
        "ALPHA_HEALTH_BACKEND_HOST",
        "ENABLED_REMOTE_MODELS",
        "FORCE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


class _StubBackend:
    """Minimal RemoteModelLike stand-in for check_backend categorization."""

    external = True
    context_only = False

    def __init__(self, behavior):
        self._behavior = behavior

    async def _infer_no_context(self, **kwargs):
        assert kwargs.get("raise_http_errors") is True
        behavior = self._behavior
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            return await behavior()
        return behavior


def _result(**overrides):
    base = dict(
        provider="TestProvider",
        model_name="test/model",
        internal_name="model",
        category=mhc.CATEGORY_OTHER,
        ui_registered=True,
        reporting_registered=True,
    )
    base.update(overrides)
    return mhc.BackendCheckResult(**base)


def _http_error(status, message="err", headers=None):
    return aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


class TestSanitizeError:
    def test_redacts_secret_env_values(self, monkeypatch):
        monkeypatch.setenv("SOME_PROVIDER_API_KEY", "super-secret-value-123")
        out = mhc.sanitize_error("failed with key super-secret-value-123 present")
        assert "super-secret-value-123" not in out
        assert "[REDACTED]" in out

    def test_redacts_authorization_headers_and_keys(self):
        out = mhc.sanitize_error(
            "Authorization: Bearer abc.def.ghi and x-api-key: sk-abcdef123456789"
        )
        assert "sk-abcdef123456789" not in out
        assert "abc.def.ghi" not in out

    def test_redacts_long_opaque_tokens(self):
        token = "A" * 48
        out = mhc.sanitize_error(f"request with token {token} rejected")
        assert token not in out

    def test_caps_length_and_collapses_whitespace(self):
        out = mhc.sanitize_error("word  \n\n  word " + "x. " * 500)
        assert len(out) <= mhc.MAX_SANITIZED_ERROR_LEN
        assert "\n" not in out

    def test_handles_none(self):
        assert mhc.sanitize_error(None) == ""


class TestCheckBackendCategorization:
    def _check(self, behavior, timeout=5.0, **result_overrides):
        return asyncio.run(
            mhc.check_backend(
                _result(**result_overrides), _StubBackend(behavior), timeout=timeout
            )
        )

    def test_success_is_pass_with_latency(self):
        res = self._check("OK")
        assert res.category == mhc.CATEGORY_PASS
        assert res.ok is True
        assert res.latency_ms is not None
        assert res.started_at is not None

    def test_success_unregistered_flagged_distinctly(self):
        res = self._check("OK", ui_registered=False, reporting_registered=False)
        assert res.category == mhc.CATEGORY_PASS_UNREGISTERED
        assert res.ok is True
        assert "selection UI" in res.error
        assert "reporting registry" in res.error

    def test_401_is_auth_failure(self):
        res = self._check(_http_error(401, "invalid api key"))
        assert res.category == mhc.CATEGORY_AUTH
        assert res.failed

    def test_403_is_auth_failure(self):
        assert self._check(_http_error(403)).category == mhc.CATEGORY_AUTH

    def test_404_is_model_not_found(self):
        res = self._check(_http_error(404, "model not found"))
        assert res.category == mhc.CATEGORY_MODEL_NOT_FOUND

    def test_400_mentioning_model_is_model_not_found(self):
        res = self._check(_http_error(400, "unknown model 'claude-nope'"))
        assert res.category == mhc.CATEGORY_MODEL_NOT_FOUND

    def test_429_is_rate_limited(self):
        assert self._check(_http_error(429)).category == mhc.CATEGORY_RATE_LIMITED

    def test_402_quota_is_rate_limited(self):
        assert self._check(_http_error(402)).category == mhc.CATEGORY_RATE_LIMITED

    def test_5xx_is_network(self):
        assert self._check(_http_error(503)).category == mhc.CATEGORY_NETWORK

    def test_connection_error_is_network(self):
        res = self._check(aiohttp.ClientConnectionError("dns fail"))
        assert res.category == mhc.CATEGORY_NETWORK

    def test_timeout_categorized(self):
        async def hang():
            await asyncio.sleep(5)

        res = self._check(hang, timeout=0.05)
        assert res.category == mhc.CATEGORY_TIMEOUT
        assert res.failed

    def test_empty_response_is_malformed(self):
        res = self._check(None)
        assert res.category == mhc.CATEGORY_MALFORMED_RESPONSE
        assert res.failed

    def test_unexpected_exception_is_other(self):
        res = self._check(RuntimeError("boom"))
        assert res.category == mhc.CATEGORY_OTHER

    def test_provider_error_text_is_sanitized(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_TEST_TOKEN", "leaky-secret-98765")
        res = self._check(_http_error(401, "denied for key leaky-secret-98765"))
        assert "leaky-secret-98765" not in res.error
        assert "[REDACTED]" in res.error


class TestEnumeration:
    def test_unconfigured_providers_reported_not_failed(
        self, monkeypatch, fresh_router
    ):
        _clear_provider_env(monkeypatch)
        static, checkable = mhc.enumerate_backend_checks()
        assert checkable == []
        assert static, "expected provider catalog rows even with nothing configured"
        assert {r.category for r in static} == {mhc.CATEGORY_NOT_CONFIGURED}
        assert all(not r.enabled and not r.failed for r in static)

    def test_claude_enumerated_and_registered_when_key_set(
        self, monkeypatch, fresh_router
    ):
        """Claude appears in the registry, selection-UI pool, and reporting
        registry as soon as ANTHROPIC_API_KEY is configured."""
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        static, checkable = mhc.enumerate_backend_checks()
        names = {r.model_name for r, _ in checkable}
        assert "anthropic/claude-sonnet-4-6" in names
        assert "anthropic/claude-opus-4-8" in names
        assert "anthropic/claude-haiku-4-5" in names
        for result, instance in checkable:
            assert result.ui_registered, f"{result.model_name} missing from UI pool"
            assert (
                result.reporting_registered
            ), f"{result.model_name} missing from reporting registry"
        # And the router itself agrees (registry cross-check).
        router = ml_router_module.ml_router
        assert "anthropic/claude-sonnet-4-6" in router.models_by_name
        assert any(
            getattr(m, "name", None) == "anthropic/claude-sonnet-4-6"
            for m in router.external_models_by_cost
        )

    def test_allowlist_disabled_models_are_not_checkable(
        self, monkeypatch, fresh_router
    ):
        """Backends excluded by ENABLED_REMOTE_MODELS are reported as DISABLED
        and never invoked."""
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        monkeypatch.setenv("ENABLED_REMOTE_MODELS", "anthropic/claude-opus-4-8")
        static, checkable = mhc.enumerate_backend_checks()
        checkable_names = {r.model_name for r, _ in checkable}
        assert checkable_names == {"anthropic/claude-opus-4-8"}
        disabled = {r.model_name for r in static if r.category == mhc.CATEGORY_DISABLED}
        assert "anthropic/claude-sonnet-4-6" in disabled
        assert "anthropic/claude-haiku-4-5" in disabled
        assert all(not r.enabled for r in static if r.category == mhc.CATEGORY_DISABLED)

    def test_partial_azure_config_is_missing_credentials(
        self, monkeypatch, fresh_router
    ):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv(
            "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/openai/v1"
        )
        static, checkable = mhc.enumerate_backend_checks()
        azure = [r for r in static if r.model_name.startswith("azure-openai/")]
        assert azure
        assert {r.category for r in azure} == {mhc.CATEGORY_MISSING_CREDENTIALS}
        assert all(r.failed for r in azure)
        assert not any(r.model_name.startswith("azure-openai/") for r, _ in checkable)

    def test_only_models_filter_by_friendly_name(self, monkeypatch, fresh_router):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        static, checkable = mhc.enumerate_backend_checks(
            only_models=["anthropic/claude-sonnet-4-6"]
        )
        assert [r.model_name for r, _ in checkable] == ["anthropic/claude-sonnet-4-6"]
        assert static == []

    def test_only_models_filter_accepts_internal_name(self, monkeypatch, fresh_router):
        # The wire id "claude-sonnet-4-6" matches BOTH the direct Anthropic
        # model and the (unconfigured) azure-anthropic deployment of the same
        # name — the configured one is checkable, the other reports its
        # configuration state.
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        static, checkable = mhc.enumerate_backend_checks(
            only_models=["claude-sonnet-4-6"]
        )
        assert [r.model_name for r, _ in checkable] == ["anthropic/claude-sonnet-4-6"]
        assert {r.model_name for r in static} == {"azure-anthropic/claude-sonnet-4-6"}
        assert {r.category for r in static} == {mhc.CATEGORY_NOT_CONFIGURED}

    def test_disabled_backends_never_invoked_end_to_end(
        self, monkeypatch, fresh_router
    ):
        """run_checks_async must not call _infer on an allow-list-disabled model."""
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")
        monkeypatch.setenv("ENABLED_REMOTE_MODELS", "anthropic/claude-opus-4-8")

        calls = []

        async def fake_check(result, instance, timeout):
            calls.append(result.model_name)
            result.category = mhc.CATEGORY_PASS
            result.ok = True
            return result

        with patch.object(mhc, "check_backend", side_effect=fake_check):
            results = asyncio.run(mhc.run_checks_async())
        assert calls == ["anthropic/claude-opus-4-8"]
        by_name = {r.model_name: r for r in results}
        assert by_name["anthropic/claude-sonnet-4-6"].category == mhc.CATEGORY_DISABLED


def _fake_results(*categories):
    out = []
    for i, category in enumerate(categories):
        r = _result(
            model_name=f"prov/model-{i}",
            internal_name=f"model-{i}",
            category=category,
        )
        r.ok = category in (mhc.CATEGORY_PASS, mhc.CATEGORY_PASS_UNREGISTERED)
        if not r.ok and category in mhc.FAILURE_CATEGORIES:
            r.error = f"sanitized detail {i}"
        out.append(r)
    return out


def _patch_results(results):
    return patch.object(mhc, "run_checks_async", new=AsyncMock(return_value=results))


@pytest.mark.django_db
class TestRunHealthCheckOrchestration:
    def test_leader_claim_allows_exactly_one_run_per_deployment(self, monkeypatch):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-leader-1")
        with _patch_results(_fake_results(mhc.CATEGORY_PASS)):
            first = mhc.run_health_check(require_leader=True, persist=False)
            second = mhc.run_health_check(require_leader=True, persist=False)
        assert first.ran_checks is True
        assert second.ran_checks is False
        assert second.results == []

    def test_new_deployment_id_gets_fresh_leader_slot(self, monkeypatch):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-leader-2a")
        with _patch_results(_fake_results(mhc.CATEGORY_PASS)):
            assert mhc.run_health_check(require_leader=True, persist=False).ran_checks
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-leader-2b")
        with _patch_results(_fake_results(mhc.CATEGORY_PASS)):
            assert mhc.run_health_check(require_leader=True, persist=False).ran_checks

    def test_single_consolidated_email_on_failures(self, monkeypatch, mailoutbox):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-email-1")
        monkeypatch.setenv("FHI_MODEL_HEALTH_ALERT_EMAIL", "1")
        results = _fake_results(
            mhc.CATEGORY_AUTH, mhc.CATEGORY_MODEL_NOT_FOUND, mhc.CATEGORY_PASS
        )
        with _patch_results(results):
            summary = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
        assert summary.email_sent is True
        assert len(mailoutbox) == 1  # one email for BOTH failures, not one each
        body = mailoutbox[0].body
        assert mailoutbox[0].to == [mhc.SUPPORT_EMAIL]
        assert "vtest-email-1" in body
        assert "prov/model-0" in body and "prov/model-1" in body
        assert mhc.CATEGORY_AUTH in body
        assert mhc.CATEGORY_MODEL_NOT_FOUND in body
        assert "prov/model-2" not in body  # healthy backend not in the alert
        assert "MODEL_BACKEND_HEALTH_SUMMARY" in mhc.format_summary_block(summary)

    def test_healthy_run_sends_no_email(self, monkeypatch, mailoutbox):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-email-2")
        monkeypatch.setenv("FHI_MODEL_HEALTH_ALERT_EMAIL", "1")
        with _patch_results(_fake_results(mhc.CATEGORY_PASS, mhc.CATEGORY_PASS)):
            summary = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
        assert summary.email_sent is False
        assert mailoutbox == []

    def test_test_environment_suppresses_email_by_default(
        self, monkeypatch, mailoutbox
    ):
        """TESTING=True (the test configs) must not email support unless
        explicitly force-enabled."""
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-email-3")
        monkeypatch.delenv("FHI_MODEL_HEALTH_ALERT_EMAIL", raising=False)
        assert os.getenv("TESTING") == "True"
        with _patch_results(_fake_results(mhc.CATEGORY_AUTH)):
            summary = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
        assert summary.failures
        assert summary.email_sent is False
        assert mailoutbox == []

    def test_explicit_zero_disables_email_everywhere(self, monkeypatch, mailoutbox):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-email-4")
        monkeypatch.setenv("FHI_MODEL_HEALTH_ALERT_EMAIL", "0")
        with _patch_results(_fake_results(mhc.CATEGORY_AUTH)):
            summary = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
        assert summary.email_sent is False
        assert mailoutbox == []

    def test_non_leader_cannot_email(self, monkeypatch, mailoutbox):
        """Whoever loses the leader claim never runs checks, so it can never
        send the alert."""
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-email-5")
        monkeypatch.setenv("FHI_MODEL_HEALTH_ALERT_EMAIL", "1")
        with _patch_results(_fake_results(mhc.CATEGORY_AUTH)):
            leader = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
            loser = mhc.run_health_check(
                require_leader=True, send_alert_email=True, persist=False
            )
        assert leader.email_sent is True
        assert loser.ran_checks is False and loser.email_sent is False
        assert len(mailoutbox) == 1

    def test_results_persisted_for_staff_dashboard(self, monkeypatch):
        from fighthealthinsurance.models import ModelBackendHealthCheckResult

        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "vtest-persist-1")
        results = _fake_results(mhc.CATEGORY_PASS, mhc.CATEGORY_AUTH)
        results[0].latency_ms = 842
        with _patch_results(results):
            summary = mhc.run_health_check(persist=True)
        assert summary.persisted is True
        rows = ModelBackendHealthCheckResult.objects.filter(run_id=summary.run_id)
        assert rows.count() == 2
        pass_row = rows.get(model_name="prov/model-0")
        assert pass_row.ok is True
        assert pass_row.latency_ms == 842
        assert pass_row.deployment_id == "vtest-persist-1"
        fail_row = rows.get(model_name="prov/model-1")
        assert fail_row.ok is False
        assert fail_row.category == mhc.CATEGORY_AUTH


class TestConfigFlags:
    def test_strict_mode_default_off(self, monkeypatch):
        monkeypatch.delenv("FHI_MODEL_HEALTH_STRICT", raising=False)
        assert mhc.strict_mode_enabled() is False

    def test_strict_mode_on(self, monkeypatch):
        monkeypatch.setenv("FHI_MODEL_HEALTH_STRICT", "1")
        assert mhc.strict_mode_enabled() is True

    def test_deployment_id_prefers_explicit_env(self, monkeypatch):
        monkeypatch.setenv("FHI_DEPLOYMENT_ID", "explicit-id")
        monkeypatch.setenv("FHI_RELEASE", "v1.2.3")
        assert mhc.deployment_id() == "explicit-id"

    def test_deployment_id_falls_back_to_release(self, monkeypatch):
        monkeypatch.delenv("FHI_DEPLOYMENT_ID", raising=False)
        monkeypatch.setenv("FHI_RELEASE", "v1.2.3")
        assert mhc.deployment_id() == "v1.2.3"


class TestCheckModelBackendsCommand:
    def _summary(self, results, ran=True, email_sent=False):
        summary = mhc.HealthCheckRunSummary(
            run_id="r1", deployment_id="vcmd", environment="Test"
        )
        summary.results = results
        summary.ran_checks = ran
        summary.email_sent = email_sent
        return summary

    def _call(self, summary, *args):
        out = StringIO()
        with patch.object(mhc, "run_health_check", return_value=summary) as run_mock:
            call_command("check_model_backends", *args, stdout=out, stderr=out)
        return out.getvalue(), run_mock

    def test_manual_run_exits_nonzero_on_failure(self):
        summary = self._summary(_fake_results(mhc.CATEGORY_AUTH))
        with pytest.raises(SystemExit) as excinfo:
            self._call(summary)
        assert excinfo.value.code == 1

    def test_manual_run_success_exits_zero(self):
        output, _ = self._call(self._summary(_fake_results(mhc.CATEGORY_PASS)))
        assert "healthy" in output
        assert "MODEL_BACKEND_HEALTH_SUMMARY" in output

    def test_deploy_hook_failure_non_strict_exits_zero(self, monkeypatch):
        monkeypatch.delenv("FHI_MODEL_HEALTH_STRICT", raising=False)
        output, run_mock = self._call(
            self._summary(_fake_results(mhc.CATEGORY_AUTH)), "--deploy-hook"
        )
        assert "Non-strict mode" in output
        kwargs = run_mock.call_args.kwargs
        assert kwargs["require_leader"] is True
        assert kwargs["send_alert_email"] is True

    def test_deploy_hook_failure_strict_exits_nonzero(self, monkeypatch):
        monkeypatch.setenv("FHI_MODEL_HEALTH_STRICT", "1")
        with pytest.raises(SystemExit) as excinfo:
            self._call(self._summary(_fake_results(mhc.CATEGORY_AUTH)), "--deploy-hook")
        assert excinfo.value.code == 2

    def test_deploy_hook_skip_when_not_leader_exits_zero(self):
        output, _ = self._call(self._summary([], ran=False), "--deploy-hook")
        assert "skipped" in output.lower()

    def test_unknown_model_filter_exits_nonzero(self):
        summary = self._summary([])
        with pytest.raises(SystemExit) as excinfo:
            self._call(summary, "--model", "no/such-model")
        assert excinfo.value.code == 1

    def test_single_model_filter_passed_through(self):
        summary = self._summary(_fake_results(mhc.CATEGORY_PASS))
        _, run_mock = self._call(summary, "--model", "anthropic/claude-sonnet-4-6")
        assert run_mock.call_args.kwargs["only_models"] == [
            "anthropic/claude-sonnet-4-6"
        ]

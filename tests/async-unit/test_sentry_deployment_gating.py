"""Sentry must only initialize for real (non-local) deployments.

Dev machines routinely carry the production SENTRY_ENDPOINT plus a Prod
DJANGO_CONFIGURATION (copied .env files; editors like Cursor export .env
into the process env), so the old ``SENTRY_ENDPOINT and not DEBUG`` gate in
asgi.py reported every local hiccup to Sentry tagged as production. The gate
now also requires a deployment marker: KUBERNETES_SERVICE_HOST (the kubelet
injects it into every pod) or an explicit FHI_DEPLOYED opt-in.
"""

import pytest

from fighthealthinsurance.env_utils import (
    is_deployed_environment,
    should_enable_sentry,
)

_ENV_VARS = ["KUBERNETES_SERVICE_HOST", "FHI_DEPLOYED"]

_DSN = "https://key@sentry.example/1"


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestIsDeployedEnvironment:
    def test_local_machine_is_not_deployed(self, monkeypatch):
        _clear_env(monkeypatch)
        assert is_deployed_environment() is False

    def test_kubernetes_pod_is_deployed(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert is_deployed_environment() is True

    def test_empty_kubernetes_host_is_not_deployed(self, monkeypatch):
        """Templating can materialize unset env vars as empty strings."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "")
        assert is_deployed_environment() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
    def test_explicit_opt_in_counts_as_deployed(self, monkeypatch, value):
        _clear_env(monkeypatch)
        monkeypatch.setenv("FHI_DEPLOYED", value)
        assert is_deployed_environment() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_falsy_opt_in_is_not_deployed(self, monkeypatch, value):
        _clear_env(monkeypatch)
        monkeypatch.setenv("FHI_DEPLOYED", value)
        assert is_deployed_environment() is False


class TestShouldEnableSentry:
    def test_deployed_non_debug_enables_sentry(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert should_enable_sentry(_DSN, debug=False) is True

    def test_local_machine_with_prod_env_stays_silent(self, monkeypatch):
        """The incident case: real DSN + DEBUG off on a laptop must not report."""
        _clear_env(monkeypatch)
        assert should_enable_sentry(_DSN, debug=False) is False

    def test_debug_stays_silent_even_when_deployed(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert should_enable_sentry(_DSN, debug=True) is False

    def test_missing_endpoint_stays_silent_when_deployed(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        assert should_enable_sentry(None, debug=False) is False

    def test_fhi_deployed_opt_in_enables_sentry_off_kubernetes(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("FHI_DEPLOYED", "1")
        assert should_enable_sentry(_DSN, debug=False) is True

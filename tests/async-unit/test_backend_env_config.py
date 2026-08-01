"""Regressions for env-driven backend construction hazards found in the
production x-review.

- RemoteHealthInsurance defaults its backup host/port to the PRIMARY's, so
  with no distinct backup configured, dual-mode raced two identical requests
  against the same box (doubled load, zero redundancy) and the "failover"
  retried the exact endpoint that just failed.
- k8s/compose templating can materialize unset env vars as EMPTY STRINGS;
  ``os.getenv(var, default)`` returns "" then, which built URLs like
  ``http://:80/v1`` instead of falling back.
- ``model_is_ok`` probed ``self.api_base`` unconditionally, so a backup-only
  configuration (primary host unset) crashed the health sweep with
  RuntimeError instead of probing the backup that actually serves traffic.
"""

from unittest.mock import MagicMock, patch

from llm_result_utils.cleaner_utils import CleanerUtils

# Importing ml_models installs the bounded is_valid_url override on
# CleanerUtils (see _bounded_is_valid_url).
from fighthealthinsurance.ml.ml_models import RemoteHealthInsurance


_ENV_VARS = [
    "HEALTH_BACKEND_HOST",
    "HEALTH_BACKEND_PORT",
    "HEALTH_BACKUP_BACKEND_HOST",
    "HEALTH_BACKUP_BACKEND_PORT",
    "HEALTH_BACKUP_BACKEND_MODEL",
]


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestIdenticalBackupDisabled:
    def test_no_distinct_backup_disables_backup_and_dual_mode(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        assert m.api_base == "http://primary.internal:80/v1"
        assert m.backup_api_base is None
        assert m.dual_mode is False

    def test_distinct_backup_host_keeps_backup_and_dual_mode(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_HOST", "backup.internal")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        assert m.backup_api_base == "http://backup.internal:80/v1"
        assert m.dual_mode is True

    def test_same_host_different_model_keeps_backup(self, monkeypatch):
        """One box serving two models is a real fallback (e.g. a smaller
        model that fits when the big one is overloaded)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_MODEL", "smaller-model")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        assert m.backup_api_base == "http://primary.internal:80/v1"
        assert m.backup_model == "smaller-model"


class TestEmptyStringEnvIsUnset:
    def test_empty_backup_host_means_no_backup(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_HOST", "")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        assert m.backup_api_base is None

    def test_empty_port_falls_back_to_default(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        monkeypatch.setenv("HEALTH_BACKEND_PORT", "")
        m = RemoteHealthInsurance("some-model", dual_mode=False)
        assert m.api_base == "http://primary.internal:80/v1"


class TestBoundedUrlValidatorSchemes:
    def test_file_scheme_rejected_without_network(self):
        """URLs come from LLM output; urllib would otherwise happily open
        file:// (and other non-network schemes). Must be rejected before any
        request is attempted."""
        with patch(
            "fighthealthinsurance.ml.ml_models._urllib_request.urlopen"
        ) as mock_open:
            assert CleanerUtils.is_valid_url("file:///etc/passwd") is False
            assert CleanerUtils.is_valid_url("ftp://example.com/x") is False
            assert CleanerUtils.is_valid_url("not a url") is False
        mock_open.assert_not_called()

    def test_https_scheme_probes_network(self):
        with patch(
            "fighthealthinsurance.ml.ml_models._urllib_request.urlopen"
        ) as mock_open:
            mock_open.return_value.read.return_value = b"ok page content"
            assert CleanerUtils.is_valid_url("https://example.com/policy.pdf") is True
        mock_open.assert_called_once()


class TestModelIsOkProbesEffectiveBase:
    def test_backup_only_config_probes_backup(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_HOST", "backup.internal")
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_MODEL", "backup-model")
        m = RemoteHealthInsurance("some-model", dual_mode=False)
        assert m.api_base is None
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": [{"id": "backup-model"}]}
        with patch(
            "fighthealthinsurance.ml.ml_models.requests.get", return_value=resp
        ) as mock_get:
            assert m.model_is_ok() is True
        assert mock_get.call_args.args[0] == "http://backup.internal:80/v1/models"

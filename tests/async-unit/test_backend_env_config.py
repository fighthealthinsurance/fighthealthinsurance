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
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike, RemoteHealthInsurance

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
    def test_no_distinct_backup_disables_racing_keeps_sequential_retry(
        self, monkeypatch
    ):
        """An identical backup must not RACE (doubles load on one box for
        zero redundancy) but must remain as a sequential fallback: a retry
        against the same endpoint still rescues transient per-request
        faults."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        assert m.api_base == "http://primary.internal:80/v1"
        assert m.backup_api_base == "http://primary.internal:80/v1"
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
    def test_empty_backup_host_falls_back_to_primary_without_racing(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("HEALTH_BACKEND_HOST", "primary.internal")
        monkeypatch.setenv("HEALTH_BACKUP_BACKEND_HOST", "")
        m = RemoteHealthInsurance("some-model", dual_mode=True)
        # "" means unset -> backup defaults to the primary: kept as a
        # sequential retry, but never raced.
        assert m.backup_api_base == "http://primary.internal:80/v1"
        assert m.dual_mode is False

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


class TestModelIsOkFallsBackToTheBackup:
    """Inference falls back to the backup when the primary fails, and the
    router drops a backend the health sweep marks down. Probing the primary
    alone took a backend whose backup was still answering out of appeals."""

    PRIMARY = "http://primary.internal/v1"
    BACKUP = "http://backup.internal/v1"

    def _model(self):
        return RemoteFullOpenLike(
            self.PRIMARY, "tok", "m", backup_api_base=self.BACKUP
        )

    @staticmethod
    def _serving(model_id):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": [{"id": model_id}]}
        return resp

    def _probe(self, answers, model=None):
        """model_is_ok() on ``model`` (a fresh one by default) with each
        /models URL answered from ``answers`` (an exception is raised); the
        result and the requests.get calls."""

        def get(url, **kwargs):
            answer = answers[url]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch(
            "fighthealthinsurance.ml.ml_models.requests.get", side_effect=get
        ) as mock_get:
            ok = (model or self._model()).model_is_ok()
        return ok, mock_get.call_args_list

    def _down_primary(self, model=None):
        import requests

        return self._probe(
            {
                f"{self.PRIMARY}/models": requests.Timeout("read timed out"),
                f"{self.BACKUP}/models": self._serving("m"),
            },
            model,
        )

    def test_a_down_primary_with_a_serving_backup_is_ok(self):
        ok, _ = self._down_primary()
        assert ok is True

    def test_a_hung_primary_leaves_the_backup_time_before_the_sweep_gives_up(self):
        """The sweep waits 10s for a backend and the staff status page 8s. A
        primary given all of that on its own would time out with the backup
        never asked."""
        _, calls = self._down_primary()
        assert sum(c.kwargs["timeout"] for c in calls) < 8

    def test_a_serving_primary_is_ok_without_probing_the_backup(self):
        _, calls = self._probe({f"{self.PRIMARY}/models": self._serving("m")})
        assert [c.args[0] for c in calls] == [f"{self.PRIMARY}/models"]

    def test_neither_endpoint_serving_the_model_is_not_ok(self):
        ok, _ = self._probe(
            {
                f"{self.PRIMARY}/models": self._serving("other"),
                f"{self.BACKUP}/models": self._serving("other"),
            }
        )
        assert ok is False

    def test_a_backup_answering_for_a_down_primary_is_recorded_as_the_backup(self):
        """The serving registry reads the first card as what the primary
        serves, so the backup's answer must not stand in for it: drafts
        would point at weights the primary never served."""
        model = self._model()
        self._down_primary(model)
        assert (model.last_model_card, model.last_backup_model_card["endpoint"]) == (
            None,
            "backup.internal",
        )

    def test_a_serving_primary_records_its_own_card(self):
        model = self._model()
        self._probe({f"{self.PRIMARY}/models": self._serving("m")}, model)
        assert model.last_model_card["endpoint"] == "primary.internal"

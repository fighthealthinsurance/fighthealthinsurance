"""Unit tests for WorstInsuranceRefreshActor's testable helpers (without Ray)."""

from unittest.mock import MagicMock, patch

import pytest

from fighthealthinsurance.worst_insurance_refresh_actor import (
    WorstInsuranceRefreshActor,
)


def _identity_sync_to_async(fn, **kwargs):
    """Stub for asgiref.sync_to_async that just calls fn inline."""

    async def _wrapper(*args, **kw):
        return fn(*args, **kw)

    return _wrapper


# Access the underlying class through the Ray decorator's __ray_metadata__.
_Klass = getattr(WorstInsuranceRefreshActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else WorstInsuranceRefreshActor


class TestConfiguredUrl:
    @patch(
        "django.conf.settings.WORST_INSURANCE_RANKINGS_URL",
        "https://example.com/latest.json",
        create=True,
    )
    def test_returns_url_when_set(self):
        assert _Underlying._configured_url() == "https://example.com/latest.json"

    @patch(
        "django.conf.settings.WORST_INSURANCE_RANKINGS_URL",
        "",
        create=True,
    )
    def test_returns_empty_when_unset(self):
        assert _Underlying._configured_url() == ""


class TestRefresh:
    @patch("fighthealthinsurance.worst_insurance_ingest.load_report_dict")
    @patch("fighthealthinsurance.worst_insurance_ingest.fetch_json")
    def test_refresh_fetches_and_loads(self, mock_fetch, mock_load):
        mock_fetch.return_value = {"schema_version": 1, "period": "2026-07"}
        mock_load.return_value = (5, 2, 1, 0)

        result = _Underlying._refresh("https://example.com/latest.json")

        assert result == (5, 2, 1, 0)
        mock_fetch.assert_called_once_with("https://example.com/latest.json")
        mock_load.assert_called_once_with(
            {"schema_version": 1, "period": "2026-07"},
            source_url="https://example.com/latest.json",
        )


class TestIntervalWiring:
    def test_class_is_wired_to_worst_insurance_settings_key(self):
        assert (
            _Underlying.settings_interval_key
            == "WORST_INSURANCE_REFRESH_INTERVAL_HOURS"
        )
        assert _Underlying.default_interval_hours == 24


class TestRefreshDue:
    """The async entry point BaseRefreshActor calls on the refresh cadence."""

    def _actor(self):
        actor = _Underlying.__new__(_Underlying)
        actor._logger = MagicMock()
        return actor

    @pytest.mark.asyncio
    async def test_returns_false_when_url_unset(self):
        actor = self._actor()
        actor._configured_url = MagicMock(return_value="")
        assert await actor._refresh_due(24) is False

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.worst_insurance_refresh_actor.sync_to_async",
        _identity_sync_to_async,
    )
    async def test_returns_true_and_warns_on_match_failures(self):
        actor = self._actor()
        actor._configured_url = MagicMock(return_value="https://example.com/x.json")
        actor._refresh = MagicMock(return_value=(1, 0, 0, 2))
        assert await actor._refresh_due(24) is True
        actor._refresh.assert_called_once_with("https://example.com/x.json")
        # match_failures > 0 escalates the log to a warning.
        actor._logger.warning.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "fighthealthinsurance.worst_insurance_refresh_actor.sync_to_async",
        _identity_sync_to_async,
    )
    async def test_returns_false_on_exception(self):
        actor = self._actor()
        actor._configured_url = MagicMock(return_value="https://example.com/x.json")
        actor._refresh = MagicMock(side_effect=RuntimeError("boom"))
        assert await actor._refresh_due(24) is False

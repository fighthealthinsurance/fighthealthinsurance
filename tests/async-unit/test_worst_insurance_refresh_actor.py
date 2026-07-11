"""Unit tests for WorstInsuranceRefreshActor's testable helpers (without Ray)."""

from unittest.mock import patch

from fighthealthinsurance.worst_insurance_refresh_actor import (
    WorstInsuranceRefreshActor,
)

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

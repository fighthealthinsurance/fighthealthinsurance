"""Unit tests for IMRRefreshActor's testable helpers (without Ray)."""

from unittest.mock import patch

from fighthealthinsurance.imr_refresh_actor import IMRRefreshActor
from fighthealthinsurance.models import IMRDecision

# Access the underlying class through the Ray decorator's __ray_metadata__.
# When @ray.remote wraps a class, the original class is accessible at this
# attribute; falling back to the decorated object if Ray isn't loaded.
_Klass = getattr(IMRRefreshActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else IMRRefreshActor


class TestConfiguredSources:
    @patch("django.conf.settings.IMR_DMHC_CSV_URL", "https://example.gov/dmhc.csv")
    @patch("django.conf.settings.IMR_DFS_CSV_URL", "https://example.gov/dfs.csv")
    def test_returns_both_when_both_set(self):
        sources = _Underlying._configured_sources()
        assert (IMRDecision.SOURCE_CA_DMHC, "https://example.gov/dmhc.csv") in sources
        assert (IMRDecision.SOURCE_NY_DFS, "https://example.gov/dfs.csv") in sources

    @patch("django.conf.settings.IMR_DMHC_CSV_URL", "https://example.gov/dmhc.csv")
    @patch("django.conf.settings.IMR_DFS_CSV_URL", "")
    def test_skips_empty_urls(self):
        sources = _Underlying._configured_sources()
        sources_dict = dict(sources)
        assert IMRDecision.SOURCE_CA_DMHC in sources_dict
        assert IMRDecision.SOURCE_NY_DFS not in sources_dict

    @patch("django.conf.settings.IMR_DMHC_CSV_URL", "")
    @patch("django.conf.settings.IMR_DFS_CSV_URL", "")
    def test_returns_empty_when_none_set(self):
        assert _Underlying._configured_sources() == []


class TestRefreshSource:
    @patch("fighthealthinsurance.imr_ingest.load_csv_text")
    @patch("fighthealthinsurance.imr_ingest.fetch_csv")
    def test_refresh_source_fetches_and_loads(self, mock_fetch, mock_load):
        mock_fetch.return_value = "csv,body"
        mock_load.return_value = (3, 1, 0, 0)

        result = _Underlying._refresh_source(
            IMRDecision.SOURCE_CA_DMHC, "https://example.gov/dmhc.csv"
        )

        assert result == (3, 1, 0, 0)
        mock_fetch.assert_called_once_with("https://example.gov/dmhc.csv")
        mock_load.assert_called_once_with(
            "csv,body",
            source=IMRDecision.SOURCE_CA_DMHC,
            source_url="https://example.gov/dmhc.csv",
        )


class TestIntervalHours:
    @patch("django.conf.settings.IMR_REFRESH_INTERVAL_HOURS", 24, create=True)
    def test_returns_configured_value(self):
        assert _Underlying._interval_hours() == 24

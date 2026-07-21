"""Unit tests for the psycopg pool stats prometheus collector."""

from unittest import mock

from fighthealthinsurance import db_pool_metrics


class FakePool:
    name = "fhi-default"

    def __init__(self, stats=None, error=None):
        self._stats = stats or {}
        self._error = error

    def get_stats(self):
        if self._error is not None:
            raise self._error
        return self._stats


class TestDatabasePoolStatsCollector:
    def test_describe_is_empty_so_registration_never_collects(self):
        # A non-empty describe() would make REGISTRY.register() run
        # collect() during AppConfig.ready(), touching django.db mid
        # startup.
        assert list(db_pool_metrics.DatabasePoolStatsCollector().describe()) == []

    def test_collect_yields_no_samples_without_pools(self):
        # Test configs run sqlite, whose backend has no pool attribute, so
        # the real _iter_pools finds nothing.
        families = list(db_pool_metrics.DatabasePoolStatsCollector().collect())
        assert sum(len(f.samples) for f in families) == 0

    def test_collect_exports_gauge_sample_for_pooled_alias(self):
        pool = FakePool(stats={"pool_size": 7})
        with mock.patch.object(
            db_pool_metrics, "_iter_pools", return_value=[("default", pool)]
        ):
            families = {
                f.name: f
                for f in db_pool_metrics.DatabasePoolStatsCollector().collect()
            }
        (sample,) = families["fhi_pg_pool_size"].samples
        assert (sample.labels, sample.value) == (
            {"alias": "default", "pool": "fhi-default"},
            7,
        )

    def test_collect_exports_counter_sample_for_pooled_alias(self):
        pool = FakePool(stats={"requests_num": 123})
        with mock.patch.object(
            db_pool_metrics, "_iter_pools", return_value=[("default", pool)]
        ):
            families = {
                f.name: f
                for f in db_pool_metrics.DatabasePoolStatsCollector().collect()
            }
        samples = {s.name: s.value for s in families["fhi_pg_pool_requests"].samples}
        assert samples["fhi_pg_pool_requests_total"] == 123

    def test_collect_survives_a_pool_whose_stats_raise(self):
        broken = FakePool(error=RuntimeError("stats exploded"))
        healthy = FakePool(stats={"pool_size": 3})
        with mock.patch.object(
            db_pool_metrics,
            "_iter_pools",
            return_value=[("default", broken), ("mysql", healthy)],
        ):
            families = {
                f.name: f
                for f in db_pool_metrics.DatabasePoolStatsCollector().collect()
            }
        assert [s.labels["alias"] for s in families["fhi_pg_pool_size"].samples] == [
            "mysql"
        ]

    def test_register_pool_stats_collector_is_idempotent(self):
        # ready() already registered once for this process; further calls
        # must be no-ops rather than duplicate registrations.
        db_pool_metrics.register_pool_stats_collector()
        db_pool_metrics.register_pool_stats_collector()
        assert db_pool_metrics._registered is True

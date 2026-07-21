"""Unit tests for the psycopg pool stats prometheus collector."""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from prometheus_client import REGISTRY, generate_latest

from fighthealthinsurance import db_pool_metrics


class FakePool:
    name = "fhi-default"

    def __init__(self, stats=None, error=None, opened=True):
        self._stats = stats or {}
        self._error = error
        self._opened = opened

    def get_stats(self):
        if self._error is not None:
            raise self._error
        return self._stats


class FakeConnectionHandler:
    """Stands in for django.db.connections: alias -> wrapper or exception."""

    def __init__(self, wrappers):
        self._wrappers = wrappers

    def __iter__(self):
        return iter(self._wrappers)

    def __getitem__(self, alias):
        value = self._wrappers[alias]
        if isinstance(value, Exception):
            raise value
        return value


class FakeWrapper:
    def __init__(self, pool):
        self.pool = pool


class TestDatabasePoolStatsCollector:
    def test_describe_is_static_and_never_touches_django_db(self):
        # describe() runs inside REGISTRY.register() during
        # AppConfig.ready(); it must come straight from the metric tables
        # (giving the registry real names for duplicate detection) without
        # touching django.db.connections mid-startup.
        with mock.patch.object(
            db_pool_metrics,
            "_iter_pools",
            side_effect=AssertionError("describe must not touch django.db"),
        ):
            families = list(db_pool_metrics.DatabasePoolStatsCollector().describe())
        assert len(families) == len(db_pool_metrics._GAUGE_STATS) + len(
            db_pool_metrics._COUNTER_STATS
        ) and all(len(f.samples) == 0 for f in families)

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

    def test_iter_pools_survives_a_misconfigured_alias(self):
        # Django's pool property raises ImproperlyConfigured on several
        # misconfigs; one bad alias must not 500 the whole /metrics
        # endpoint, and must not hide the healthy aliases.
        handler = FakeConnectionHandler(
            {
                "default": ImproperlyConfigured("pooling misconfigured"),
                "mysql": FakeWrapper(FakePool(stats={"pool_size": 1})),
            }
        )
        with mock.patch("django.db.connections", handler):
            aliases = [alias for alias, _ in db_pool_metrics._iter_pools()]
        assert aliases == ["mysql"]

    def test_iter_pools_skips_pools_that_were_never_opened(self):
        # An unopened pool reports pool_size == min_size with zero real
        # connections, which reads as "all checked out" on the starvation
        # dashboard.
        handler = FakeConnectionHandler(
            {"default": FakeWrapper(FakePool(stats={"pool_size": 2}, opened=False))}
        )
        with mock.patch("django.db.connections", handler):
            assert list(db_pool_metrics._iter_pools()) == []

    def test_appconfig_registered_collector_on_default_registry(self):
        # End-to-end wiring: FightHealthInsuranceConfig.ready() must have
        # registered the collector on the default registry at django
        # startup, so the families show up in a real scrape even with
        # pooling off.
        assert b"# TYPE fhi_pg_pool_requests_waiting gauge" in generate_latest(REGISTRY)

    def test_register_pool_stats_collector_registers_exactly_once(self):
        # ready() already registered for this process; repeated calls (and
        # a broken module guard, thanks to describe() exposing real names
        # to the registry's duplicate detection) must not produce duplicate
        # families, which would fail the whole scrape on a real Prometheus
        # server.
        db_pool_metrics.register_pool_stats_collector()
        db_pool_metrics.register_pool_stats_collector()
        exposition = generate_latest(REGISTRY).decode()
        assert exposition.count("# HELP fhi_pg_pool_size ") == 1

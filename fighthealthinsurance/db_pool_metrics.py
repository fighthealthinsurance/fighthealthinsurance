"""Prometheus export of psycopg connection-pool statistics.

Django's postgres backend keeps one psycopg_pool ConnectionPool per process
per database alias when pooling is enabled (Prod's PG_USE_POOL). The pool's
``get_stats()`` counters are the only visibility into checkout starvation --
the failure mode behind the PoolTimeout / 60s-ingress-504 incidents -- so we
surface them on the existing django_prometheus ``/metrics`` endpoint.

Registered from ``FightHealthInsuranceConfig.ready()``. When pooling is off
(the current default) no aliases have a pool and every family is emitted
with zero samples (HELP/TYPE stanzas only), so this is nearly free
everywhere except pooled processes.
"""

from typing import Iterable, Iterator, Tuple

from loguru import logger
from prometheus_client import REGISTRY
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

# psycopg_pool get_stats() keys -> exported metric name and help text.
# Gauges are point-in-time measures; counters are process-lifetime monotonic
# (we only ever call get_stats(), never the resetting pop_stats()).
_GAUGE_STATS = {
    "pool_min": ("fhi_pg_pool_min_size", "Configured pool min_size"),
    "pool_max": ("fhi_pg_pool_max_size", "Configured pool max_size"),
    "pool_size": (
        "fhi_pg_pool_size",
        "Connections currently managed by the pool (idle + checked out + being prepared)",
    ),
    "pool_available": (
        "fhi_pg_pool_available",
        "Idle connections sitting in the pool",
    ),
    "requests_waiting": (
        "fhi_pg_pool_requests_waiting",
        "Checkouts currently blocked waiting for a connection",
    ),
}
_COUNTER_STATS = {
    "requests_num": ("fhi_pg_pool_requests", "Connections requested from the pool"),
    "requests_queued": (
        "fhi_pg_pool_requests_queued",
        "Requests that had to wait for a connection",
    ),
    "requests_errors": (
        "fhi_pg_pool_request_errors",
        "Requests resulting in an error (timeouts, queue full)",
    ),
    "requests_wait_ms": (
        "fhi_pg_pool_requests_wait_ms",
        "Total time requests spent waiting for a connection, in ms",
    ),
    "usage_ms": (
        "fhi_pg_pool_usage_ms",
        "Total time connections were checked out, in ms",
    ),
    "connections_num": (
        "fhi_pg_pool_connections",
        "Connection attempts made by the pool to the server",
    ),
    "connections_ms": (
        "fhi_pg_pool_connections_ms",
        "Total time spent establishing connections, in ms",
    ),
    "connections_errors": (
        "fhi_pg_pool_connection_errors",
        "Failed connection attempts",
    ),
    "connections_lost": (
        "fhi_pg_pool_connections_lost",
        "Connections lost after checkout (closed/broken on return)",
    ),
    "returns_bad": (
        "fhi_pg_pool_returns_bad",
        "Connections returned to the pool in a bad state",
    ),
}
_LABELS = ["alias", "pool"]


def _iter_pools() -> Iterator[Tuple[str, object]]:
    """Yield (alias, psycopg_pool pool) for DB aliases with an opened pool.

    Imported lazily so the collector can be constructed before Django's app
    registry is ready. Non-postgres backends have no ``pool`` attribute and
    postgres without OPTIONS["pool"] returns None; both are skipped. So are
    pools nothing has opened yet: an unopened pool reports
    pool_size == min_size with pool_available == 0, which reads as "all
    checked out" on the starvation dashboard while no connection exists.
    Everything is guarded per alias -- Django's pool property raises
    ImproperlyConfigured on several misconfigs (missing driver, empty NAME,
    CONN_MAX_AGE != 0), and one bad alias must degrade to a log line, not
    500 the whole /metrics endpoint mid-incident.
    """
    from django.db import connections

    for alias in connections:
        try:
            pool = getattr(connections[alias], "pool", None)
            if pool is not None and getattr(pool, "_opened", True):
                yield alias, pool
        except Exception:
            logger.opt(exception=True).warning(
                f"Skipping connection pool stats for alias {alias}"
            )


class DatabasePoolStatsCollector(Collector):
    """Expose psycopg_pool get_stats() per database alias."""

    def describe(self) -> Iterable[Metric]:
        # Static description straight from the tables: gives the registry
        # real metric names for duplicate-registration detection without
        # touching django.db.connections during AppConfig.ready(). (The
        # registry only falls back to calling collect() at register time
        # when a collector has no describe() at all.)
        for name, doc in _GAUGE_STATS.values():
            yield GaugeMetricFamily(name, doc, labels=_LABELS)
        for name, doc in _COUNTER_STATS.values():
            yield CounterMetricFamily(name, doc, labels=_LABELS)

    def collect(self) -> Iterator[Metric]:
        gauges = {
            key: GaugeMetricFamily(name, doc, labels=_LABELS)
            for key, (name, doc) in _GAUGE_STATS.items()
        }
        counters = {
            key: CounterMetricFamily(name, doc, labels=_LABELS)
            for key, (name, doc) in _COUNTER_STATS.items()
        }
        for alias, pool in _iter_pools():
            try:
                stats = pool.get_stats()  # type: ignore[attr-defined]
                labels = [alias, getattr(pool, "name", "")]
            except Exception:
                logger.opt(exception=True).warning(
                    f"Failed to read connection pool stats for {alias}"
                )
                continue
            for key, gauge_family in gauges.items():
                gauge_family.add_metric(labels, stats.get(key, 0))
            for key, counter_family in counters.items():
                counter_family.add_metric(labels, stats.get(key, 0))
        yield from gauges.values()
        yield from counters.values()


_registered = False


def register_pool_stats_collector() -> None:
    """Register the collector on the default registry, once per process."""
    global _registered
    if _registered:
        return
    try:
        REGISTRY.register(DatabasePoolStatsCollector())
    except Exception:
        # Metrics must never block app startup.
        logger.opt(exception=True).warning(
            "Could not register the DB pool stats collector"
        )
    else:
        _registered = True

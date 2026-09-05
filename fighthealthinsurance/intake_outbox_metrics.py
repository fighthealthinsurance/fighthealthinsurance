"""Prometheus export of the intake outbox backlog.

Two gauges on the existing django_prometheus ``/metrics`` endpoint, read
from the ``IntakeJourneyEvent`` table at scrape time:

- ``fhi_intake_outbox_pending_total`` -- deliverable events not yet acked
- ``fhi_intake_outbox_oldest_pending_seconds`` -- age of the oldest one

Zero-cost while nothing is pending: the collector runs an EXISTS probe on
the partial pending index first and only aggregates when it hits. Any
failure (table not migrated yet, database away) degrades to a log line and
an empty scrape, never a 500 on ``/metrics``. Registered from
``FightHealthInsuranceConfig.ready()`` like the connection-pool stats.
"""

from typing import Iterable, Iterator

from loguru import logger
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

_PENDING = ("fhi_intake_outbox_pending_total", "Intake outbox events not yet acked")
_OLDEST = (
    "fhi_intake_outbox_oldest_pending_seconds",
    "Age in seconds of the oldest un-acked intake outbox event",
)


class IntakeOutboxCollector(Collector):
    def describe(self) -> Iterable[Metric]:
        # Static description: real metric names for duplicate-registration
        # detection without touching the database during AppConfig.ready().
        yield GaugeMetricFamily(*_PENDING)
        yield GaugeMetricFamily(*_OLDEST)

    def collect(self) -> Iterator[Metric]:
        pending = GaugeMetricFamily(*_PENDING)
        oldest = GaugeMetricFamily(*_OLDEST)
        try:
            from fighthealthinsurance.intake_outbox import pending_stats

            count, age = pending_stats()
            pending.add_metric([], count)
            oldest.add_metric([], age)
        except Exception:
            logger.opt(exception=True).warning("intake outbox metrics unavailable")
        yield pending
        yield oldest


_registered = False


def register_intake_outbox_collector() -> None:
    global _registered
    if _registered:
        return
    try:
        REGISTRY.register(IntakeOutboxCollector())
        _registered = True
    except ValueError:
        # Already registered (e.g. the app registry loaded twice in tests).
        _registered = True

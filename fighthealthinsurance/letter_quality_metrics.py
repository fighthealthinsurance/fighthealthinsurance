"""Prometheus export of draft quality per backend (ml/letter_quality.py).

Read from ``ProposedAppeal`` at scrape time, over the last 24 hours, the same
way the intake outbox gauges are: no extra writes on the request path, and any
failure (column not migrated yet, database away) degrades to a log line and
an empty scrape, never a 500 on ``/metrics``.

- ``fhi_letter_quality_avg{model_name,scorer}`` -- mean 0..1 composite score
- ``fhi_letter_quality_scored_total{model_name,scorer}`` -- drafts scored in the
  window (by scoring time, since older drafts are rescored on re-serve)
- ``fhi_letter_quality_ungrounded_total{model_name,scorer}`` -- of those, drafts
  that scored below the grounding threshold (invented facts)

``scorer`` is the provenance string recorded on the row (the model TypeSafe
reported plus our rubric version), so a provider-side repoint of the alias
shows up as a second series rather than a blended one -- when TypeSafe names
the model it used; a response without one records the alias.
- ``fhi_letter_quality_requests_total{outcome}`` -- process-local scorer calls
  by outcome (scored / failed / skipped), so a dead key or a rate limit shows
  up as ``failed`` climbing while ``scored`` stops

This is the leading indicator for backend drift: win rate needs users to pick
drafts and weeks of them to mean anything; this moves the minute a backend
starts producing worse letters.
"""

import datetime
from typing import Dict, Iterable, Iterator, Tuple

from loguru import logger
from prometheus_client import REGISTRY
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

WINDOW = datetime.timedelta(hours=24)

_AVG = (
    "fhi_letter_quality_avg",
    "Mean draft quality score (0..1) per backend, last 24h",
)
_SCORED = ("fhi_letter_quality_scored_total", "Drafts scored per backend, last 24h")
_UNGROUNDED = (
    "fhi_letter_quality_ungrounded_total",
    "Scored drafts below the grounding threshold per backend, last 24h",
)
_REQUESTS = (
    "fhi_letter_quality_requests",
    "Draft scoring calls by outcome (this process)",
)


def quality_by_model(now: datetime.datetime) -> Dict[Tuple[str, str], Dict[str, float]]:
    """{(model_name, scorer): {"avg", "scored", "ungrounded"}} for current-rubric
    drafts in the window.

    The stored model name is normalized the way the staff dashboard does
    it (a legacy object repr collapses to its class, whitespace is
    stripped), and rows that land on one label are merged with a
    scored-weighted mean and summed counts, so one backend is one
    Prometheus series rather than one per stored spelling (review).
    """
    from django.db.models import Avg, Count, Q

    from fighthealthinsurance.ml import letter_quality
    from fighthealthinsurance.ml.model_identity import normalize_model_label
    from fighthealthinsurance.models import ProposedAppeal

    rows = (
        ProposedAppeal.objects.filter(
            quality_score__isnull=False,
            # One rubric per series; the answering model is recorded per row
            # so a provider-side repoint is visible, not averaged away.
            quality_scorer__endswith=letter_quality._RUBRIC_SUFFIX,
            model_name__isnull=False,
            quality_scored_at__gte=now - WINDOW,
        )
        .values_list("model_name", "quality_scorer")
        .annotate(
            avg=Avg("quality_score"),
            scored=Count("id"),
            ungrounded=Count(
                "id",
                filter=Q(grounding_score__lt=letter_quality.GROUNDING_DEMOTE_BELOW),
            ),
        )
        .values_list("model_name", "quality_scorer", "avg", "scored", "ungrounded")
    )
    merged: Dict[Tuple[str, str], Dict[str, float]] = {}
    for name, scorer, avg, scored, ungrounded in rows:
        if not letter_quality.same_rubric(scorer):
            continue
        label = normalize_model_label(name)
        if label is None:
            continue  # a blank name labels nothing
        entry = merged.setdefault(
            (label, str(scorer)), {"avg": 0.0, "scored": 0.0, "ungrounded": 0.0}
        )
        count = float(scored)
        total = entry["scored"] + count
        if total:
            entry["avg"] = (
                entry["avg"] * entry["scored"] + float(avg or 0.0) * count
            ) / total
        entry["scored"] = total
        entry["ungrounded"] += float(ungrounded)
    return merged


class LetterQualityCollector(Collector):
    def describe(self) -> Iterable[Metric]:
        yield GaugeMetricFamily(*_AVG, labels=["model_name", "scorer"])
        yield GaugeMetricFamily(*_SCORED, labels=["model_name", "scorer"])
        yield GaugeMetricFamily(*_UNGROUNDED, labels=["model_name", "scorer"])
        yield CounterMetricFamily(*_REQUESTS, labels=["outcome"])

    def collect(self) -> Iterator[Metric]:
        avg = GaugeMetricFamily(*_AVG, labels=["model_name", "scorer"])
        scored = GaugeMetricFamily(*_SCORED, labels=["model_name", "scorer"])
        ungrounded = GaugeMetricFamily(*_UNGROUNDED, labels=["model_name", "scorer"])
        requests = CounterMetricFamily(*_REQUESTS, labels=["outcome"])
        try:
            from django.utils import timezone

            for (model_name, scorer), stats in quality_by_model(timezone.now()).items():
                avg.add_metric([model_name, scorer], stats["avg"])
                scored.add_metric([model_name, scorer], stats["scored"])
                ungrounded.add_metric([model_name, scorer], stats["ungrounded"])
        except Exception:
            logger.opt(exception=True).warning("letter quality metrics unavailable")
        try:
            from fighthealthinsurance.ml import letter_quality

            for outcome, count in letter_quality.outcomes.items():
                requests.add_metric([outcome], float(count))
        except Exception:
            logger.opt(exception=True).warning(
                "letter quality request counters unavailable"
            )
        yield avg
        yield scored
        yield ungrounded
        yield requests


_registered = False


def register_letter_quality_collector() -> None:
    global _registered
    if _registered:
        return
    try:
        REGISTRY.register(LetterQualityCollector())
        _registered = True
    except ValueError:
        # Already registered (e.g. the app registry loaded twice in tests).
        _registered = True

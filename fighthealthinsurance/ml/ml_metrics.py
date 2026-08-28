"""Prometheus metrics for the ML call layer.

The July 2026 reliability work added rich per-attempt DB rows and log
classification for APPEAL generation, but no aggregate view: there was no way
to alert on "backend X's failure rate jumped" or "p95 latency doubled"
without log archaeology. These metrics are label-bounded (model name and a
small outcome enum -- never denial/chat ids or free text) and exported
through the same django_prometheus endpoint the DB metrics already use.

All recording helpers are no-op safe: a metrics failure must never break an
inference call.
"""

from typing import Iterable

from loguru import logger
from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector, REGISTRY

# One outcome per completed __timeout_infer call.
ML_CALLS_TOTAL = Counter(
    "fhi_ml_calls_total",
    "Model backend calls by outcome (ok = non-empty completion).",
    labelnames=("model", "outcome"),
)

# Failure *reasons* observed inside the transport layer. Deliberately a
# separate counter from ML_CALLS_TOTAL: a failed call shows up once there
# (outcome=none/timeout) and once here with its classified reason.
ML_CALL_FAILURES_TOTAL = Counter(
    "fhi_ml_call_failures_total",
    "Classified model call failures (transport, http, bad body...).",
    labelnames=("model", "reason"),
)

ML_CALL_SECONDS = Histogram(
    "fhi_ml_call_seconds",
    "Wall-clock duration of model backend calls.",
    labelnames=("model",),
    buckets=(1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420),
)

CHAT_TURNS_TOTAL = Counter(
    "fhi_chat_turns_total",
    "Chat turns by outcome (ok, failed, timeout, letter_fallback -- a "
    "failed/timed-out turn rescued by the appeal-generator letter fallback; "
    "a timed-out rescue counts under both timeout and letter_fallback).",
    labelnames=("outcome",),
)

# Loop-prevention visibility: how often candidate chat replies (nearly)
# repeated a recent reply, and what happened to them. Actions:
#   rejected_candidates -- a turn hard-rejected at least one repeated
#       candidate in the primary pass (counted once per turn);
#   delivered_repeat -- despite the ladder, the reply we delivered still
#       repeated a recent reply (last-resort delivery; should stay rare).
CHAT_REPEATED_RESPONSES_TOTAL = Counter(
    "fhi_chat_repeated_responses_total",
    "Chat turns where candidate replies repeated a recent reply, by action.",
    labelnames=("action",),
)

# Side-by-side alternate answers: how often one was offered, and which
# answer users said they preferred.
CHAT_ALTERNATE_ANSWERS_TOTAL = Counter(
    "fhi_chat_alternate_answers_total",
    "Chat turns where an alternate (side-by-side) answer was offered.",
)

CHAT_ANSWER_FEEDBACK_TOTAL = Counter(
    "fhi_chat_answer_feedback_total",
    "User feedback on side-by-side answers (preferred=primary/alternate).",
    labelnames=("preferred",),
)

# Bounded label set for the feedback counter: the value arrives from the
# client, so anything unexpected is collapsed to "other" to keep metric
# cardinality fixed.
_ANSWER_FEEDBACK_ALLOWED = frozenset({"primary", "alternate"})


def _safe_label(value: object, limit: int = 80) -> str:
    return str(value)[:limit] if value else "unknown"


def record_ml_call(model: object, outcome: str, seconds: float) -> None:
    """Record one completed model call. Never raises."""
    try:
        name = _safe_label(model)
        ML_CALLS_TOTAL.labels(model=name, outcome=outcome).inc()
        ML_CALL_SECONDS.labels(model=name).observe(seconds)
    except Exception:  # pragma: no cover - metrics must never break calls
        logger.opt(exception=True).debug("Failed to record ml call metric")


def record_ml_failure(model: object, reason: str) -> None:
    """Record a classified failure reason. Never raises."""
    try:
        ML_CALL_FAILURES_TOTAL.labels(model=_safe_label(model), reason=reason).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record ml failure metric")


def record_chat_turn(outcome: str) -> None:
    """Record a chat turn outcome (ok / failed / timeout / letter_fallback).
    Never raises."""
    try:
        CHAT_TURNS_TOTAL.labels(outcome=outcome).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record chat turn metric")


def record_chat_repeat(action: str) -> None:
    """Record a repeated-reply event (see CHAT_REPEATED_RESPONSES_TOTAL).
    Never raises."""
    try:
        CHAT_REPEATED_RESPONSES_TOTAL.labels(action=action).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record chat repeat metric")


def record_chat_alternate_offered() -> None:
    """Record that a side-by-side alternate answer was offered. Never raises."""
    try:
        CHAT_ALTERNATE_ANSWERS_TOTAL.inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record alternate metric")


def record_answer_feedback(preferred: object) -> None:
    """Record which side-by-side answer the user preferred. The value comes
    from the client, so it is collapsed to a bounded label set. Never
    raises."""
    try:
        label = str(preferred) if preferred else "other"
        if label not in _ANSWER_FEEDBACK_ALLOWED:
            label = "other"
        CHAT_ANSWER_FEEDBACK_TOTAL.labels(preferred=label).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record answer feedback metric")


class ExecutorQueueCollector(Collector):
    """Samples the executor pools' queue depth and active threads at scrape
    time (mirrors db_pool_metrics' collector pattern)."""

    def collect(self) -> Iterable[Metric]:
        try:
            from fighthealthinsurance import exec as fhi_exec

            pools = {
                "interactive": fhi_exec.executor,
                "background": fhi_exec.background_executor,
                "pubmed": fhi_exec.pubmed_executor,
            }
            queued = GaugeMetricFamily(
                "fhi_executor_queued_tasks",
                "Tasks waiting in the executor's work queue.",
                labels=["pool"],
            )
            threads = GaugeMetricFamily(
                "fhi_executor_threads",
                "Threads currently created in the executor pool.",
                labels=["pool"],
            )
            for name, pool in pools.items():
                queued.add_metric([name], pool._work_queue.qsize())
                threads.add_metric([name], len(pool._threads))
            yield queued
            yield threads
        except Exception:  # pragma: no cover
            logger.opt(exception=True).debug("Executor metrics collection failed")
            return


_collector_registered = False


def ensure_executor_collector_registered() -> None:
    global _collector_registered
    if _collector_registered:
        return
    try:
        REGISTRY.register(ExecutorQueueCollector())
        _collector_registered = True
    except Exception:  # pragma: no cover - double registration in tests
        logger.opt(exception=True).debug(
            "Executor queue collector not registered (already registered?)"
        )
        _collector_registered = True


ensure_executor_collector_registered()

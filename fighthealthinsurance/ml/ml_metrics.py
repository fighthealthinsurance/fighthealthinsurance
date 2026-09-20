"""Prometheus metrics for the ML call layer.

The July 2026 reliability work added rich per-attempt DB rows and log
classification for APPEAL generation, but no aggregate view: there was no way
to alert on "backend X's failure rate jumped" or "p95 latency doubled"
without log archaeology. These metrics are label-bounded (the model's
registry name -- the identity ProposedAppeal, ModelCallAttempt and the staff
dashboard key on, so the series can be joined to them -- a primary/backup
leg, why the call was made (appeal, chat, probe, other), and a small outcome
enum; never denial/chat ids or free text) and exported through the same
django_prometheus endpoint the DB metrics already use. Only processes that
serve that endpoint are scraped: the Temporal worker serves this registry too
(run_temporal_worker.app_metrics_server, on FHI_APP_METRICS_BIND), but
generation that runs on the Ray actors (the speculative precompute, the
chooser refill) records into a registry nothing reads yet.

All recording helpers are no-op safe: a metrics failure must never break an
inference call.
"""

import contextlib
import contextvars
import functools
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    ParamSpec,
    TypeVar,
)

from loguru import logger
from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector, REGISTRY

# One outcome per __timeout_infer call: ok (non-empty completion), none (the
# call returned nothing), timeout, or error (the transport raised -- an HTTP
# 4xx/5xx re-raised for status-specific handling). Every call lands here
# exactly once, so failures / calls is always a rate.
#
# ``purpose`` is why the call was made (see ML_CALL_PURPOSE). Appeal
# generation shares its backend instances with chat, the health probes,
# entity extraction and summaries, so without it an appeal-only outage (a
# redeploy that shrinks the context window under long appeal prompts while
# short chat turns keep succeeding) was diluted by the traffic that kept
# succeeding, and the appeal latency quantile by the probes' "Hello"s.
ML_CALLS_TOTAL = Counter(
    "fhi_ml_calls_total",
    "Model backend calls by outcome (ok = non-empty completion, none, timeout, "
    "error).",
    labelnames=("model", "leg", "purpose", "outcome"),
)

# Failure *reasons* observed inside the transport layer. Deliberately a
# separate counter from ML_CALLS_TOTAL: a failed call shows up once there
# (outcome=none/timeout/error) and once here with its classified reason
# (transport_error, http_error, bad_body, context_overflow, missing_model,
# skipped_missing_model, unexpected_error).
ML_CALL_FAILURES_TOTAL = Counter(
    "fhi_ml_call_failures_total",
    "Classified model call failures (transport, http, bad body...).",
    labelnames=("model", "leg", "purpose", "reason"),
)

ML_CALL_SECONDS = Histogram(
    "fhi_ml_call_seconds",
    "Wall-clock duration of model backend calls.",
    labelnames=("model", "leg", "purpose"),
    buckets=(1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420),
)

# What the appeal path made of a completion, once per _checked_infer
# invocation. ML_CALLS_TOTAL's "ok" is transport-level (a non-empty body):
# a backend that is up but answers every appeal prompt with a refusal, a
# runt or runaway repetition read as 100% ok there while every draft it
# produced was rejected and filed as a no_output attempt, so the failure
# rate the call series exists to alert on never moved. Results: accepted;
# rejected_bad_result (a refusal / severe repetition / runt, after the one
# retry); rejected_repetition (the cleaners removed everything);
# skipped_deadline (the requester's budget had passed).
ML_RESULTS_TOTAL = Counter(
    "fhi_ml_results_total",
    "Checked appeal inferences by what became of the completion (accepted, "
    "rejected_bad_result, rejected_repetition, skipped_deadline).",
    labelnames=("model", "infer_type", "result"),
)
_RESULTS = frozenset(
    {"accepted", "rejected_bad_result", "rejected_repetition", "skipped_deadline"}
)

CHAT_TURNS_TOTAL = Counter(
    "fhi_chat_turns_total",
    "Chat turns by outcome (ok, failed, timeout).",
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


# Which endpoint of a primary/backup pair a call went to. Bounded here so a
# caller cannot widen the label set by accident.
_LEGS = frozenset({"primary", "backup"})


def _leg_label(leg: object) -> str:
    return leg if isinstance(leg, str) and leg in _LEGS else "primary"


# Why a model call was made. Bounded here so a caller cannot widen the label
# set: appeal (letter generation through _checked_infer -- appeals,
# escalation and prior-auth letters -- and the synthesis pass), chat (a chat
# turn), probe (the reachability probes), other (everything else: entity
# extraction, summaries, the chooser refill, model_query ...).
_PURPOSES = frozenset({"appeal", "chat", "probe", "other"})

# The purpose of the calls in flight on this task/thread. A ContextVar rather
# than an argument threaded through the _infer signatures (three of them,
# plus the tests that patch them): it reaches every call awaited inside the
# labelled entry point, including the temperature legs and the primary/
# backup race, which copy the context when they are spawned.
ML_CALL_PURPOSE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "fhi_ml_call_purpose", default="other"
)


def _purpose_label(purpose: object) -> str:
    return purpose if isinstance(purpose, str) and purpose in _PURPOSES else "other"


@contextlib.contextmanager
def ml_call_purpose(purpose: str) -> Iterator[None]:
    """Record every model call made inside this block under ``purpose``."""
    token = ML_CALL_PURPOSE.set(_purpose_label(purpose))
    try:
        yield
    finally:
        ML_CALL_PURPOSE.reset(token)


_P = ParamSpec("_P")
_T = TypeVar("_T")


def labelled_ml_calls(
    purpose: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Coroutine[Any, Any, _T]]]:
    """Decorate an async entry point so every model call awaited inside it is
    recorded under ``purpose``."""

    def decorate(
        fn: Callable[_P, Awaitable[_T]],
    ) -> Callable[_P, Coroutine[Any, Any, _T]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with ml_call_purpose(purpose):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate


def record_ml_call(
    model: object, outcome: str, seconds: float, leg: str = "primary"
) -> None:
    """Record one completed model call. ``model`` is the registry name when the
    router stamped one (RemoteModelLike._metric_identity), ``leg`` the
    endpoint of a primary/backup pair; the purpose comes from the
    ML_CALL_PURPOSE in scope. Never raises."""
    try:
        name = _safe_label(model)
        leg = _leg_label(leg)
        purpose = ML_CALL_PURPOSE.get()
        ML_CALLS_TOTAL.labels(
            model=name, leg=leg, purpose=purpose, outcome=outcome
        ).inc()
        ML_CALL_SECONDS.labels(model=name, leg=leg, purpose=purpose).observe(seconds)
    except Exception:  # pragma: no cover - metrics must never break calls
        logger.opt(exception=True).debug("Failed to record ml call metric")


def record_ml_failure(model: object, reason: str, leg: str = "primary") -> None:
    """Record a classified failure reason. Never raises."""
    try:
        ML_CALL_FAILURES_TOTAL.labels(
            model=_safe_label(model),
            leg=_leg_label(leg),
            purpose=ML_CALL_PURPOSE.get(),
            reason=reason,
        ).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record ml failure metric")


def record_ml_result(model: object, infer_type: object, result: str) -> None:
    """Record what _checked_infer made of a completion (see ML_RESULTS_TOTAL).
    Never raises."""
    try:
        ML_RESULTS_TOTAL.labels(
            model=_safe_label(model),
            infer_type=_safe_label(infer_type),
            result=result if result in _RESULTS else "other",
        ).inc()
    except Exception:  # pragma: no cover
        logger.opt(exception=True).debug("Failed to record ml result metric")


def record_chat_turn(outcome: str) -> None:
    """Record a chat turn outcome (ok / failed / timeout). Never raises."""
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

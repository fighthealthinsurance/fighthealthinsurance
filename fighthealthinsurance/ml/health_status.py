"""
Lightweight, cached health snapshot for model backends.

What & why:
- Computes how many model backends are currently reachable/healthy.
- Starts on the first selection or status request (there is no startup hook),
  caches results, and refreshes periodically (hourly) in the background; until
  the first sweep lands every backend reads as unchecked and selection fails open.
- Between sweeps, rechecks only the backends the last one marked down (every
  few minutes), so one failed probe does not keep a backend out of routing,
  the public count or the serving registry for the hour.
- Avoids heavy checks per request; endpoint simply returns the cached snapshot,
  with this pod's live signals (``live_problem``) read on top at each read.

Trade-offs:
- Uses a simple ping inference with short timeout; does not validate output quality.
- Background refresh via threading.Timer keeps dependencies minimal (no Celery needed).
"""

import concurrent.futures
import datetime
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.utils import sanitize_url_for_display
from fighthealthinsurance.ml import spend

REFRESH_INTERVAL_SECONDS = 60 * 60  # hourly
# How soon a backend the sweep marked down is probed again. Routing leaves it
# out until a probe passes, and a routed-out backend gets no calls that could
# bring it back, so without this one failed probe (a restart, a slow /models)
# cost it the hour. Doubled after each recheck that leaves one down, so a
# backend that stays dead is not probed (and warned about) every few minutes.
DOWN_RECHECK_SECONDS = 3 * 60
ALERT_THROTTLE_SECONDS = 60 * 60  # at most one alert email per hour
# How long a sweep (or a recheck) waits for its probes. Each probe has its own
# worker and a 7s budget (MODEL_PROBE_BUDGET_SECONDS), so all of them fit.
SWEEP_TIMEOUT_SECONDS = 10
PAUSED_FOR_CREDIT = "provider paused for credit or quota until 00:00 UTC"


def _model_key(model: Any) -> str:
    """Stable per-instance identifier for a backend, shared by the health sweep
    (which records per-model results) and the router (which looks them up).

    Keyed by object identity so distinct backend instances never collide — e.g.
    internal replicas of one model on several hosts, or two providers exposing
    the same wire ``model`` id — even when they share a friendly ``name``. The
    sweep and the router operate on the same registered singleton instances, so
    the identity matches across them; an unregistered/foreign instance simply
    isn't found (``model_ok`` returns ``None`` -> the router fails open). A
    readable prefix (friendly name / wire id / class) is kept for debuggability.
    """
    prefix = getattr(model, "name", None) or getattr(model, "model", None)
    if not prefix:
        prefix = type(model).__name__
    return f"{prefix}@{id(model):x}"


def _probe_name(model: Any) -> str:
    """The name a health check shows for ``model``: its wire model id, or its
    class name. Shared by the sweep, the down recheck and the staff
    breakdown, so a backend reads the same in each."""
    return str(
        getattr(model, "model", None)
        or getattr(model, "__class__", type(model)).__name__
    )


def _sweep_candidates(router: Any) -> Tuple[List[Any], Set[int], Set[int]]:
    """Every backend a health check covers, with the ids of the context-only
    ones (citations) and of chat's own outside models. Those two sit in no
    generation pool: they are checked so a dead one shows up, but neither can
    draft an appeal, so neither counts as one that can.

    all_models_by_cost is read first, so a router that cannot enumerate
    raises its own error.
    """
    candidates = list(router.all_models_by_cost)
    context_only = list(router.context_only_models_by_cost)
    candidates += context_only
    chat_only = [
        m for m in router.chat_outside_models_by_name.values() if m not in candidates
    ]
    candidates += chat_only
    return candidates, {id(m) for m in context_only}, {id(m) for m in chat_only}


def _unavailable_reason(model: Any) -> Optional[str]:
    """Why the backend recorded itself unusable on this pod (a retired model,
    a refused key, an unreachable host), or None. In memory, never a call.
    Staff-only: the reason can name our billing or key state, so it never
    goes in the public snapshot."""
    try:
        reason = getattr(model, "unavailable_reason", None)
        value = reason() if callable(reason) else None
    except Exception:
        logger.opt(exception=True).debug(f"unavailable_reason failed for {model}")
        return None
    return value if isinstance(value, str) and value else None


def live_problem(instance: Any) -> Optional[str]:
    """Why this pod's own signals say ``instance`` cannot answer now, or
    None: the flags its calls left (not served, refused, unreachable; see
    ``unavailable_reason``), or its provider paused for credit or quota
    today. In memory only, never a model or the network. Never raises: a
    backend without these accessors, or a test double, reads as None.

    A /models probe passes through all of these (listing models is free and
    needs no credit), so the probe result alone read such a backend as
    healthy. Staff pages show the reason; the public snapshot only says
    "not ok". Read at display time and never written into the sweep's map,
    so it clears when the refusal or pause does, not an hour later.
    """
    reason = _unavailable_reason(instance)
    if reason:
        return reason
    try:
        # Every use ("*"), the pause a credit or quota refusal sets. A spent
        # per-use budget is not a dead provider.
        provider = getattr(instance, "SPEND_PROVIDER", None)
        if isinstance(provider, str) and provider and spend.paused(provider, "*"):
            return PAUSED_FOR_CREDIT
    except Exception as e:
        logger.debug(f"Spend pause not read: {type(e).__name__}")
    return None


def _note_reachable(model: Any) -> None:
    """A /models probe just heard ``model``'s endpoint list it, so that host
    is reachable again: a connect-failure cooldown on it (an outside one
    escalates up to an hour while the host stays gone) no longer holds, and
    its next outage starts from the short cooldown. Only reachability:
    /models can list a model a key is refused for, so the refused and
    missing flags stay until a call is served. In memory; never raises.
    """
    try:
        note_reachable = getattr(model, "_note_reachable", None)
        if not callable(note_reachable):
            return
        legs = model.serving_legs()
        if len(legs) == 1:
            pairs = [(legs[0][1], legs[0][2])]
        else:
            # The probe records a card on each leg that answered; one that
            # did not was not heard from.
            cards = ("last_model_card", "last_backup_model_card")
            pairs = [
                (base, wire)
                for (_leg, base, wire), card in zip(legs, cards)
                if getattr(model, card, None)
            ]
        for pair in pairs:
            note_reachable(*pair)
    except Exception:
        logger.opt(exception=True).debug(f"Reachability not noted for {model}")


@dataclass
class BackendHealthDetail:
    name: str
    ok: bool
    error: Optional[str] = None
    # The backend's _model_key, so a recheck that brings it back drops this
    # row (names can repeat across providers). Never shown.
    key: Optional[str] = None


@dataclass
class PassedProbe:
    """A backend whose probe passed in the sweep, or in a later recheck that
    put it back. The snapshot re-reads it for live problems at each read
    (see ``live_problem``)."""

    model: Any
    name: str
    # Counts in alive_models: it can draft (not context-only or chat-only).
    drafting: bool
    external: bool


@dataclass
class HealthSnapshot:
    alive_models: int = 0
    last_checked: float = field(default_factory=lambda: time.time())
    details: List[BackendHealthDetail] = field(default_factory=list)
    passed: List[PassedProbe] = field(default_factory=list)


class _HealthStatus:
    def __init__(self):
        self._snapshot: HealthSnapshot = HealthSnapshot()
        # Per-model result of the last sweep, keyed by ``_model_key``. Rebound
        # atomically on each refresh and read lock-free by ``model_ok`` so the
        # router can consult it on the request path without ever blocking on the
        # sweep lock.
        self._health_map: Dict[str, bool] = {}
        # The last sweep's map and when it ran (epoch seconds), published as
        # one tuple so a reader never pairs one sweep's verdicts with another
        # sweep's time. None before the first sweep on this process.
        self._last_sweep: Optional[Tuple[Dict[str, bool], float]] = None
        # The last sweep's candidates (the serving registry and the down
        # recheck read them), when it started (monotonic; the hourly cadence
        # counts from it), and the wait before the next recheck of the ones it
        # marked down.
        self._last_candidates: List[Any] = []
        # ids of the last sweep's candidates that cannot draft (context-only
        # and chat-only), so a recheck that puts one back counts it as the
        # sweep would have.
        self._last_non_drafting_ids: Set[int] = set()
        self._last_sweep_at: Optional[float] = None
        self._recheck_seconds: float = DOWN_RECHECK_SECONDS
        self._timer: Optional[threading.Timer] = None
        self._initialized = False
        # Whether the recurring background sweep has been kicked off. Kept
        # separate from ``_initialized`` (which get_snapshot owns for its first
        # synchronous refresh) so ensure_started() can start the sweep without
        # making get_snapshot's first call return a stale empty snapshot.
        # Guarded by a short, dedicated lock that is never held during a sweep,
        # so request-path callers (via model_ok) never block.
        self._sweep_started = False
        self._start_lock = threading.Lock()
        # Use RLock to allow reentrant locking and reduce deadlock chances
        self._lock = threading.RLock()
        # Fast mode for tests to avoid network stalls
        self._fast_mode = os.getenv("FHI_HEALTH_FAST", "0") == "1"
        # Monotonic timestamp of last alert email, used to throttle
        # outgoing alerts to at most one per ALERT_THROTTLE_SECONDS.
        self._last_alert_sent_at: Optional[float] = None

    def get_snapshot(self) -> Dict[str, Any]:
        # Use lock to prevent race condition on first access
        pending_alert: Optional[
            Tuple[int, int, List[BackendHealthDetail], Optional[str]]
        ] = None
        with self._lock:
            if not self._initialized:
                pending_alert = self._refresh_unlocked()
                self._initialized = True

        # Start the recurring sweep exactly once. Done outside _lock via a
        # short, separate lock so it neither double-schedules with
        # ensure_started() nor blocks. We just refreshed synchronously above
        # (when first initializing), so only the timer chain needs starting.
        self._ensure_sweep_scheduled(refresh_now=False)

        if pending_alert is not None:
            self._alert_if_all_internal_dead(*pending_alert)
            # The first, synchronous sweep's cards are recorded too, or drafts
            # would point at nothing until the next round an hour later.
            self._record_serving()

        snapshot = self._snapshot
        # A backend whose probe passed can still be unable to answer: a key
        # refused, a model not served, a host cooling down, or its provider
        # paused for credit. Read now rather than at the sweep, so one that
        # starts after the sweep is not counted for up to an hour, and one
        # that clears is counted again at once. Listed with no reason: it can
        # name our billing or key state.
        live_down = [p for p in snapshot.passed if live_problem(p.model)]
        details = [
            {"name": d.name, "ok": d.ok, "error": d.error} for d in snapshot.details
        ]
        # Internal backends stay out of the list, as their probe failures do.
        details += [
            {"name": p.name, "ok": False, "error": "not ok"}
            for p in live_down
            if p.external
        ]
        return {
            "alive_models": snapshot.alive_models
            - sum(1 for p in live_down if p.drafting),
            "last_checked": snapshot.last_checked,
            "details": details,
        }

    def model_ok(self, model: Any) -> Optional[bool]:
        """Return the last sweep's result for ``model`` — ``True``/``False`` —
        or ``None`` if it hasn't been checked yet.

        Lock-free and non-blocking: it reads the atomically-rebound health map
        and never triggers a probe, so the router can call it on the request
        path. It does ensure the periodic sweep is running (in the background)
        so the cache gets populated, but returns immediately regardless.
        """
        self.ensure_started()
        return self._health_map.get(_model_key(model))

    def last_sweep_result(self, model: Any) -> Tuple[Optional[bool], Optional[float]]:
        """The last background sweep's verdict for ``model`` and when that
        sweep ran (epoch seconds); ``None`` for either when there is none.

        A read for the staff status page. Unlike :meth:`model_ok` it never
        starts the sweep, so looking at the page has no side effects.
        """
        last = self._last_sweep
        if last is None:
            return None, None
        health, checked_at = last
        return health.get(_model_key(model)), checked_at

    def ensure_started(self) -> None:
        """Start the periodic health sweep once, in the background, so cached
        results get populated without any caller blocking on the initial probe.

        Lock-free fast path; the short ``_start_lock`` is never held during a
        sweep, so request-path callers (via :meth:`model_ok`) don't contend.
        Crucially this does *not* set ``_initialized`` — that flag is owned by
        :meth:`get_snapshot`, so its first call still does a synchronous refresh
        and never returns a stale empty snapshot just because a background sweep
        was kicked off here first.
        """
        self._ensure_sweep_scheduled(refresh_now=True)

    def _background_sweep_enabled(self) -> bool:
        """Whether the recurring background sweep should run in this config.

        Disabled in the ``Test*`` configs: the sweep re-arms itself on a timer
        thread for the life of the process and writes the cross-pod alert
        throttle row on every pass, so its writes land in the middle of
        unrelated tests and can lock the table against a
        ``TransactionTestCase`` teardown flush. Callers that need a real
        measurement (``get_snapshot``) still sweep synchronously.
        """
        from django.conf import settings

        return bool(getattr(settings, "ML_HEALTH_BACKGROUND_SWEEP", True))

    def _ensure_sweep_scheduled(self, refresh_now: bool) -> None:
        """Kick off the recurring sweep exactly once (idempotent, non-blocking).

        ``refresh_now`` runs the first sweep immediately in a background thread
        (used when the cached snapshot may still be cold); otherwise we only
        start the timer chain, because the caller already refreshed
        synchronously and the snapshot is warm.
        """
        if self._sweep_started or not self._background_sweep_enabled():
            return
        with self._start_lock:
            if self._sweep_started:
                return
            self._sweep_started = True
        if refresh_now:
            threading.Thread(
                target=self._refresh, daemon=True, name="health-sweep"
            ).start()
        else:
            self._schedule_refresh()

    def _schedule_refresh(self):
        """Schedule the next refresh.

        No-ops when the background sweep is disabled, so a direct ``_refresh``
        call can't arm a timer chain the config asked us not to run.
        """
        if not self._background_sweep_enabled():
            return
        try:
            self._timer = threading.Timer(self._next_tick_seconds(), self._tick)
            self._timer.daemon = True
            self._timer.start()
        except Exception as e:
            logger.warning(f"Failed to schedule health refresh: {e}")

    def _seconds_to_full_sweep(self) -> float:
        """How long until the next full sweep is due; 0 before the first."""
        if self._last_sweep_at is None:
            return 0.0
        interval = 5 if self._fast_mode else REFRESH_INTERVAL_SECONDS
        return max(0.0, self._last_sweep_at + interval - time.monotonic())

    def _next_tick_seconds(self) -> float:
        """When the one timer chain fires next: at the full sweep, or sooner
        to recheck the backends the last sweep left out of routing. The
        rechecks do not move the hourly sweep."""
        wait = self._seconds_to_full_sweep()
        if self._down_for_routing():
            wait = min(wait, self._recheck_seconds)
        # Never a zero wait, so nothing can make the chain spin.
        return max(1.0, wait)

    def _tick(self) -> None:
        """The timer chain's callback: the full sweep when it is due, else a
        recheck of just the backends the last sweep marked down."""
        # Within a second counts as due: a timer can fire a hair early.
        if self._seconds_to_full_sweep() <= 1.0:
            self._refresh()  # re-arms the chain itself
            return
        try:
            self._recheck_down()
        except Exception:
            logger.opt(exception=True).warning("Recheck of down model backends failed")
        finally:
            self._schedule_refresh()

    def _down_for_routing(self) -> List[Any]:
        """The last sweep's backends that routing leaves out on its result:
        marked down, with no live signal of their own (health_checked_live),
        so only another probe can bring them back."""
        health = self._health_map
        return [
            m
            for m in self._last_candidates
            if health.get(_model_key(m)) is False
            and not getattr(m, "health_checked_live", False)
        ]

    def _recheck_down(self) -> None:
        """Probe again only the backends the last sweep marked down, and put
        each that passes back now rather than at the next sweep: into
        routing, into the public snapshot (counted alive when it can draft,
        its failing row dropped), and into the serving registry with the card
        its passing probe just read, so its drafts are attributed from the
        start. Never marks one down: the hourly sweep stays the only source
        of that and of the alert. Probes run outside the sweep lock, which
        only guards rebinding the map and the snapshot."""
        down = self._down_for_routing()
        if not down:
            return
        recovered: List[Any] = []
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(down))
        try:
            future_map = {ex.submit(m.model_is_ok): m for m in down}
            concurrent.futures.wait(future_map, timeout=SWEEP_TIMEOUT_SECONDS)
            for future, m in future_map.items():
                if not future.done():
                    continue
                try:
                    if future.result(timeout=0):
                        recovered.append(m)
                except Exception as e:
                    logger.debug(f"Recheck error for {_model_key(m)}: {e}")
        finally:
            # As in the sweep: return at the deadline, not after stragglers.
            ex.shutdown(wait=False, cancel_futures=True)
        if len(recovered) < len(down):
            self._recheck_seconds = min(
                self._recheck_seconds * 2, REFRESH_INTERVAL_SECONDS
            )
        if not recovered:
            return
        for m in recovered:
            _note_reachable(m)
        with self._lock:
            # Copied and rebound, so model_ok's lock-free reads see one map
            # or the other. Only entries still False change: a sweep that
            # ran meanwhile has the newer word on the rest (and has put them
            # in its own snapshot and registry round).
            health = dict(self._health_map)
            back = [m for m in recovered if health.get(_model_key(m)) is False]
            for m in back:
                health[_model_key(m)] = True
            self._health_map = health
            # In the same step, so the public status never disagrees with
            # routing about them.
            if back:
                self._snapshot = self._snapshot_with_recovered(back)
        if not back:
            return
        # The sweep that marked these down left the registry a "nothing" for
        # each, which holds until a round records an answer: record the cards
        # their passing probes just set, or every draft they write goes
        # unattributed until the next sweep.
        self._record_serving(back)
        logger.info(
            "Back in routing after a recheck: " + ", ".join(_model_key(m) for m in back)
        )

    def _snapshot_with_recovered(self, back: List[Any]) -> HealthSnapshot:
        """The current snapshot with ``back`` (backends a recheck just put
        back in routing) counted as the sweep counts a probe that passed:
        alive when it can draft, its failing row dropped. get_snapshot reads
        live problems on top of them as on the rest. Caller holds _lock."""
        snapshot = self._snapshot
        added = [
            PassedProbe(
                model=m,
                name=_probe_name(m),
                drafting=id(m) not in self._last_non_drafting_ids,
                external=bool(getattr(m, "external", True)),
            )
            for m in back
        ]
        keys = {_model_key(m) for m in back}
        return HealthSnapshot(
            alive_models=snapshot.alive_models + sum(1 for p in added if p.drafting),
            last_checked=snapshot.last_checked,
            details=[d for d in snapshot.details if d.key not in keys],
            passed=snapshot.passed + added,
        )

    def _refresh_unlocked(
        self,
    ) -> Tuple[int, int, List[BackendHealthDetail], Optional[str]]:
        """
        Recalculate health snapshot using cheap checks and cache it.

        This method does NOT acquire the lock - caller must hold it if needed.
        Used during initialization when we already hold the lock.

        Returns ``(internal_total, internal_alive, internal_failures,
        enumeration_error)`` so the caller can fire the alert email outside
        the lock — ``send_mail`` can block on SMTP and we don't want any
        concurrent ``get_snapshot()`` reader stuck behind it.
        """
        # Set first, so a sweep that fails still counts as this hour's.
        self._last_sweep_at = time.monotonic()
        self._recheck_seconds = DOWN_RECHECK_SECONDS
        alive_count = 0
        internal_total = 0
        internal_alive = 0
        internal_failures: List[BackendHealthDetail] = []
        passed: List[PassedProbe] = []

        # Choose a small, representative set of backends
        candidates: List[Any] = []
        # ids of the candidates that cannot draft (context-only and chat-only):
        # swept, never counted as alive.
        non_drafting_ids: Set[int] = set()
        enumeration_error: Optional[str] = None
        try:
            logger.debug("Starting to look up the models")
            router = ml_router_module.ml_router
            # Context-only backends (citations) are swept so a dead one shows
            # up between deploys; chat's own outside models are swept so one
            # that stops answering loses its place in the chat roster.
            candidates, context_only_ids, chat_only_ids = _sweep_candidates(router)
            non_drafting_ids = context_only_ids | chat_only_ids
            logger.debug(f"Considering candidates {candidates}")
        except Exception as e:
            enumeration_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Could not get all_models_by_cost: {e}")
            candidates = []
        # Kept for the serving registry, which _refresh feeds after the
        # sweep, outside the lock, and for the down recheck.
        self._last_candidates = list(candidates)
        self._last_non_drafting_ids = set(non_drafting_ids)
        # A card describes this round's successful probe. Clear them all
        # first: a probe still queued at the deadline is cancelled before
        # model_is_ok runs, so it would never clear its own.
        for m in candidates:
            for attr in ("last_model_card", "last_backup_model_card"):
                if hasattr(m, attr):
                    setattr(m, attr, None)
        for m in candidates:
            if not getattr(m, "external", True):
                internal_total += 1
        # Run health checks in parallel with a shared deadline. We wait up to
        # timeout_seconds for the checks to finish, then classify every backend
        # from its *final* state. Doing the classification in a single pass (as
        # opposed to splitting it across an as_completed loop plus a "not done"
        # sweep) means a check that completes right at the deadline is never
        # dropped from both buckets, which could otherwise fire a false "all
        # internal models are dead" page for a slow-but-healthy backend.
        details: List[BackendHealthDetail] = []
        new_health: Dict[str, bool] = {}
        timeout_seconds = SWEEP_TIMEOUT_SECONDS
        if candidates:
            # A worker per candidate, so every probe starts at once and its
            # own budget fits the deadline. With fewer, a probe still queued
            # behind slow ones at the deadline was marked down for the hour.
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates))
            try:
                future_map = {ex.submit(m.model_is_ok): m for m in candidates}
                # Block until all checks finish or the deadline elapses.
                concurrent.futures.wait(future_map, timeout=timeout_seconds)

                for future, m in future_map.items():
                    name = _probe_name(m)
                    is_internal = not getattr(m, "external", True)
                    ok = False
                    err: Optional[str] = None
                    if future.done():
                        try:
                            ok = future.result(timeout=0)
                        except Exception as e:
                            err = str(e)
                            logger.debug(f"Health check error for {name}: {e}")
                    elif future.cancel():
                        # Never started, so it measured nothing this round:
                        # the backend keeps last round's result (or stays
                        # unchecked) rather than reading down for an hour.
                        previous = self._health_map.get(_model_key(m))
                        if previous is not None:
                            new_health[_model_key(m)] = previous
                        continue
                    else:
                        err = f"timeout>{timeout_seconds}s"
                    # Routing's input: the probe alone. Live problems are
                    # read on top where the status is shown (get_snapshot),
                    # so this map never holds a refusal or a pause past the
                    # moment it clears.
                    new_health[_model_key(m)] = bool(ok)
                    if ok:
                        # alive_models is the public "a model is ready to
                        # write your appeal" number, and a context-only or
                        # chat-only backend can't draft, so it never counts.
                        drafting = id(m) not in non_drafting_ids
                        if drafting:
                            alive_count += 1
                        if is_internal:
                            internal_alive += 1
                        passed.append(
                            PassedProbe(
                                model=m,
                                name=name,
                                drafting=drafting,
                                external=not is_internal,
                            )
                        )
                        if not getattr(m, "health_checked_live", False):
                            # Its /models answered: the host is reachable.
                            _note_reachable(m)
                    else:
                        # The public snapshot lists failing EXTERNAL backends,
                        # timed out or not, saying no more than it does today:
                        # the recorded reason can name our billing or key
                        # state. Internal failures stay out of it (their names
                        # are internal wire paths) and drive the staff alert,
                        # which carries that reason.
                        if is_internal:
                            err = err or _unavailable_reason(m)
                        detail = BackendHealthDetail(
                            name=name,
                            ok=False,
                            error=err or "not ok",
                            key=_model_key(m),
                        )
                        (internal_failures if is_internal else details).append(detail)
            finally:
                # Return at the deadline rather than blocking on stragglers: a
                # `with` block's shutdown(wait=True) would join every probe, so
                # one hung model_is_ok() could stall the sweep (and the held
                # _lock) past timeout_seconds and delay publishing _health_map.
                # Cancel queued probes; let any in-flight ones finish in the
                # background. Matches compute_model_health_details below.
                ex.shutdown(wait=False, cancel_futures=True)

        snapshot = HealthSnapshot(
            alive_models=alive_count,
            last_checked=time.time(),
            details=details,
            passed=passed,
        )

        self._snapshot = snapshot
        if candidates:
            # Only replace the cached per-model health when we actually ran a
            # sweep; on an enumeration failure (no candidates) keep the last
            # known-good map rather than wiping it to "unknown".
            self._health_map = new_health
            self._last_sweep = (new_health, snapshot.last_checked)

        return internal_total, internal_alive, internal_failures, enumeration_error

    def _record_serving(self, backends: Optional[List[Any]] = None) -> None:
        """Hand model cards to the serving registry: this round's, or only
        those of ``backends`` (the ones a recheck just put back; the registry
        records per backend, so a subset leaves the rest as they were). It
        records on its own background thread and returns at once, so the
        registry can never delay this sweep, its next round, a recheck, or a
        reader of the snapshot."""
        try:
            from fighthealthinsurance.ml.serving_registry import (
                record_backends_async,
            )

            if backends is None:
                backends = getattr(self, "_last_candidates", [])
            record_backends_async(backends)
        except Exception as e:
            logger.warning(f"Serving registry update failed: {e}")

    def _alert_if_all_internal_dead(
        self,
        internal_total: int,
        internal_alive: int,
        internal_failures: List[BackendHealthDetail],
        enumeration_error: Optional[str] = None,
    ) -> None:
        """Log an error and email support when zero internal models are alive.

        If ``enumeration_error`` is set, the router itself failed and we have
        no real measurement; surface that distinctly so on-call doesn't chase
        a backend outage that's actually a router/init bug.
        """
        if internal_alive > 0:
            return
        if enumeration_error is not None:
            subject = "[FHI] Could not enumerate model backends"
            message = (
                "Model liveliness check could not enumerate backends "
                f"(ml_router.all_models_by_cost raised: {enumeration_error}). "
                "Internal model health is unknown."
            )
        else:
            detail = (
                ", ".join(f"{f.name}: {f.error or 'not ok'}" for f in internal_failures)
                or "no internal backends registered"
            )
            subject = "[FHI] All internal models are dead"
            message = (
                f"All internal models are dead "
                f"(internal_total={internal_total}, internal_alive=0). "
                f"Failures: {detail}"
            )
        logger.error(message)

        # Throttle outgoing emails to at most one per ALERT_THROTTLE_SECONDS,
        # shared across alert subjects (the recipient is the same on-call
        # address either way, and a duplicate page during an active incident
        # is more noise than signal). The error log above still fires every
        # refresh so log-based monitoring isn't suppressed.
        #
        # The authoritative throttle is a shared DB row so that, with multiple
        # web pods each running their own check, the cluster sends at most one
        # email per window. The in-memory timestamp is a per-pod fallback used
        # only if the DB throttle is unreachable.
        if not self._should_send_alert():
            return

        try:
            from django.conf import settings
            from django.core.mail import send_mail

            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                ["support42@fighthealthinsurance.com"],
                fail_silently=False,
            )
        except Exception:
            logger.opt(exception=True).error(
                "Failed to send internal-models-dead alert email"
            )

    def _should_send_alert(self) -> bool:
        """Decide whether this caller should send the alert email now.

        Prefers a cross-pod DB throttle; on any DB error falls back to the
        per-pod in-memory throttle so a database hiccup degrades to "one email
        per pod per hour" rather than one per refresh.
        """
        try:
            from django.db import close_old_connections
            from fighthealthinsurance.models import ModelHealthAlertState

            # This can run from the long-lived refresh timer thread, which
            # never gets Django's per-request connection cleanup. Drop any
            # stale connection first so we don't fall back to the per-pod
            # throttle (and lose cluster-wide dedup) on a dead socket.
            close_old_connections()

            claimed = ModelHealthAlertState.try_claim(
                "internal_models_dead", ALERT_THROTTLE_SECONDS
            )
            # Keep the in-memory fallback timestamp warm so a later DB outage
            # doesn't immediately re-open the floodgates on this pod.
            if claimed:
                with self._lock:
                    self._last_alert_sent_at = time.monotonic()
            else:
                logger.debug("Suppressing alert email; throttled cluster-wide")
            return claimed
        except Exception:
            logger.opt(exception=True).warning(
                "Cross-pod alert throttle unavailable; using per-pod throttle"
            )
            return self._claim_in_memory_alert_slot()

    def _claim_in_memory_alert_slot(self) -> bool:
        """Per-pod fallback throttle. Returns True if this pod may send now."""
        now = time.monotonic()
        with self._lock:
            last = self._last_alert_sent_at
            if last is not None and now - last < ALERT_THROTTLE_SECONDS:
                logger.debug(
                    f"Suppressing alert email; last sent {now - last:.0f}s ago"
                )
                return False
            # Mark sent before the SMTP call so a concurrent caller doesn't
            # double-send while we're inside send_mail.
            self._last_alert_sent_at = now
            return True

    def _refresh(self):
        """
        Recalculate health snapshot (called from background timer).

        Acquires lock, performs refresh, then schedules next refresh outside
        lock. The next refresh is armed whatever happens: an exception here
        used to end the timer chain for the life of the process, freezing the
        cached map every routing decision reads.
        """
        try:
            with self._lock:
                pending_alert = self._refresh_unlocked()

            self._alert_if_all_internal_dead(*pending_alert)
            self._record_serving()
        except Exception:
            logger.opt(exception=True).error(
                "Model health sweep failed; keeping the previous snapshot"
            )
        finally:
            # Since only one timer no need to worry about lock. Scheduled
            # whatever happened above, so one bad round never ends the sweep.
            self._schedule_refresh()


def _display_url(url: Any) -> Optional[str]:
    """``url`` without userinfo, query or fragment, for the status page;
    ``None`` when the backend has no endpoint of that kind."""
    if not url or not isinstance(url, str):
        return None
    try:
        return sanitize_url_for_display(url)
    except ValueError:
        return "?"


def compute_model_health_details(timeout_seconds: int = 8) -> List[Dict[str, Any]]:
    """Run a fresh, uncached health check across every known model backend.

    Unlike :meth:`_HealthStatus.get_snapshot`, which returns a cached summary
    (used by the public ``live_models_status`` endpoint and deliberately keeps
    internal failures out of ``details``), this returns a full per-backend
    breakdown — ``{"name", "ok", "external", "context_only", "chat_only",
    "error", "ref"}`` — for the staff-only system status dashboard. It does
    not send alerts or mutate the cached snapshot.

    A backend that is down shows the reason it recorded on this pod (a
    retired model, a refused key, an unreachable host, its provider paused
    for credit; see ``live_problem``) when it has one, instead of a bare
    "not ok". Such a reason takes the row down even when the probe passed.
    That reason can name our billing or key state, which is why it is here
    and not in the public snapshot.

    Each row also carries the backend's endpoint (``url``) with credentials,
    query and fragment stripped; its backup leg, when that differs from the
    primary in endpoint or model, the same way (``backup_url``, plus
    ``backup_model`` when the backup serves another model); ``checked_at``,
    when this probe answered (``None`` if it hadn't by the deadline); and
    the hourly background sweep's last verdict for the same instance
    (``sweep_ok``, ``sweep_checked_at``), the cached result the router reads
    for backends that have no live signal of their own.

    Checks run in parallel with a shared deadline; a backend whose check has
    not finished by ``timeout_seconds`` is reported as not-ok with a timeout
    error. Results are sorted problems-first (down before up, internal before
    external, then by name) so on-call sees failures at the top.
    """
    try:
        router = ml_router_module.ml_router
        # Context-only backends (citations) and chat's own outside models are
        # included: "every known backend" used to leave them out because they
        # are in no generation pool.
        candidates, context_only_ids, chat_only_ids = _sweep_candidates(router)
    except Exception:
        # Propagate rather than returning [] — an empty list is indistinguishable
        # from "no models registered" and would let the caller (_model_status)
        # report a broken router as a healthy "0 backends" instead of an error.
        logger.opt(exception=True).warning(
            "compute_model_health_details could not enumerate models"
        )
        raise

    results: List[Dict[str, Any]] = []
    if not candidates:
        return results

    # Cross-process reference so the status page can link each row to a direct
    # staff query of that exact backend. Best-effort: a backend we cannot label
    # still gets a health row, just without the query link.
    try:
        from fighthealthinsurance.ml.model_query import model_refs_by_identity

        ref_by_id = model_refs_by_identity()
    except Exception:
        logger.opt(exception=True).debug("Could not build model query references")
        ref_by_id = {}

    # When each probe returned or raised, keyed by id(model). Written by the
    # probe threads, and read only for probes that finished by the deadline.
    answered_at: Dict[int, datetime.datetime] = {}

    def probe(m: Any) -> Any:
        try:
            return m.model_is_ok()
        finally:
            answered_at[id(m)] = datetime.datetime.now(datetime.timezone.utc)

    # A worker per candidate, as in the sweep, so none waits in a queue past
    # the deadline.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates))
    try:
        future_map = {ex.submit(probe, m): m for m in candidates}
        concurrent.futures.wait(future_map, timeout=timeout_seconds)
        for future, m in future_map.items():
            name = _probe_name(m)
            is_external = bool(getattr(m, "external", True))
            ok = False
            err: Optional[str] = None
            # Read once: a probe that finishes after this point is still a
            # timeout, in its status and in its answer time alike.
            done = future.done()
            if done:
                try:
                    ok = bool(future.result(timeout=0))
                except Exception as e:
                    err = str(e)
            else:
                err = f"timeout>{timeout_seconds}s"
            sweep_ok, sweep_at = health_status.last_sweep_result(m)
            api_base = getattr(m, "api_base", None)
            backup_api_base = getattr(m, "backup_api_base", None)
            wire_model = getattr(m, "model", None)
            backup_model = getattr(m, "backup_model", None) or wire_model
            # The same rule as RemoteModelLike.backend_descriptor: a backup
            # leg is worth naming when it differs in endpoint or in model.
            backup_url = (
                _display_url(backup_api_base)
                if backup_api_base != api_base or backup_model != wire_model
                else None
            )
            # A passing /models probe says nothing of a refused key or a
            # credit pause, so this pod's live signals can still take the
            # backend down, and their reason beats the probe's own.
            live = live_problem(m)
            if live:
                ok = False
            if not ok:
                err = live or err or "not ok"
            chat_only = id(m) in chat_only_ids
            results.append(
                {
                    "name": name,
                    "ok": ok,
                    "external": is_external,
                    # Callers count what can draft by leaving context_only
                    # rows out, so a chat-only row (it cannot draft either)
                    # carries it too; chat_only says which kind it is.
                    "context_only": id(m) in context_only_ids or chat_only,
                    "chat_only": chat_only,
                    "error": err,
                    "ref": ref_by_id.get(id(m)),
                    "url": _display_url(api_base),
                    "backup_url": backup_url,
                    "backup_model": (
                        backup_model
                        if backup_url and backup_model != wire_model
                        else None
                    ),
                    "checked_at": answered_at.get(id(m)) if done else None,
                    "sweep_ok": sweep_ok,
                    "sweep_checked_at": (
                        datetime.datetime.fromtimestamp(
                            sweep_at, tz=datetime.timezone.utc
                        )
                        if sweep_at is not None
                        else None
                    ),
                }
            )
    finally:
        # Return at the deadline rather than blocking on stragglers. Exiting a
        # `with ThreadPoolExecutor()` calls shutdown(wait=True), which joins
        # every submitted probe — so a single hung/slow model_is_ok() would
        # stall the staff status request well past timeout_seconds, defeating
        # the bounded live check. Cancel queued probes and let any in-flight
        # ones finish in the background instead.
        ex.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda r: (r["ok"], r["external"], r["name"]))
    return results


# Singleton used by views
health_status = _HealthStatus()

"""The chat routing policy: which outside models the chat fan-out asks, and
how long our own models get to answer before the outside ones are sent.

Two loops run at different speeds:

* A slow one (the ``compute_chat_policy`` command, or a scheduled job) reads
  ChatTurn metadata over a window, runs the pure :func:`compute_policy` and
  appends one ChatRoutingPolicy row (:func:`compute_and_store_chat_policy`).
  Rows are never edited; rows older than POLICY_KEEP_DAYS (30) are deleted
  after a new one is written.
* A fast one, on the chat path, takes the newest row from a per-process
  cache (:func:`aget_chat_policy`) and hands it to the router, which
  narrows the outside models, and to the fan-out, which holds them back.
  The cache is refreshed on a thread of its own, so a turn never waits on
  the database for it.

The safety rules:

* :data:`DEFAULT_POLICY` is the behaviour without a policy: no outside
  model left out, the roster's own order, no delay.
* Spending caps are not here: ml/spend.py counts spend live and the router
  skips a provider whose budget is spent, as the calls happen.
* Chat follows a row only while FHI_CHAT_POLICY_APPLY is on and the row is
  newer than FHI_CHAT_POLICY_MAX_AGE_MINUTES. With the switch off (the
  default) rows are still computed and shown on the staff usage dashboard,
  but chat routes by the default. An empty table and a row that does not
  parse give the default too, and so does a database error or a stuck
  read before any row has been read; a failed read keeps the row already
  cached, and that row is followed only while it is fresh.
* A policy can only narrow the outside models the router already picks
  (:func:`narrow_externals`). It never adds one, and it never overrides a
  person's choice to keep chat on our own models: with that choice the
  router asks no outside model whatever the policy says.
* When none of our own models is selectable the router sets the policy
  aside altogether, so a turn is never left waiting on models that are down.

Everything here is names and numbers. No message, reply or other chat text
is read, kept or written.
"""

import datetime
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

from django.conf import settings
from django.utils import timezone
from loguru import logger

T = TypeVar("T")

# The ChatRoutingPolicy row layout this code writes and understands.
SCHEMA_VERSION = 1

# Reader bounds. Chat never waits on the read (see _PolicyCache): it runs
# on a thread of its own, under this statement timeout on PostgreSQL, at
# most once per cache period per process.
POLICY_READ_TIMEOUT_SECONDS = 0.5
POLICY_CACHE_SECONDS = 60.0
# The statement timeout ends a slow query on the server, not a read waiting
# on a connection that stopped answering. A read running longer than this
# is given up on (its result is dropped) so a new one can start, with at
# most MAX_STUCK_READS given-up reads still waiting per process: at most
# MAX_STUCK_READS + 1 reads at once, counting the one in flight.
POLICY_READ_STUCK_SECONDS = 30.0
MAX_STUCK_READS = 2
# A row asking for a longer delay than this is not trusted (the default
# policy is used instead): the delay comes out of the fan-out's window.
MAX_EXTERNAL_DELAY_SECONDS = 15.0
# Bounds on the names a row may carry.
_MAX_NAMES = 50
_NAME_MAX = 200

# The writer's defaults. A week, so each outside model can gather enough
# turns for its place in the order to mean something.
DEFAULT_WINDOW_MINUTES = 7 * 24 * 60
RECENT_MINUTES = 60
# Retention: rows are never edited, and rows older than this are deleted
# after a new one is written (prune_old_chat_policies).
POLICY_KEEP_DAYS = 30

# Reason tokens (ChatRoutingPolicy.reason is a comma-joined list of these).
REASON_DEFAULT = "default"
REASON_OK = "ok"
REASON_FEW_TURNS = "few_turns"
REASON_KEEP_ALL_INTERNALS_FAILING = "keep_all_internals_failing"
REASON_KEEP_ALL_NO_HEALTHY_EXTERNAL = "keep_all_no_healthy_external"
REASON_ORDERED = "ordered"
REASON_ORDERED_BY_JEV = "ordered_by_jev"
_REASON_MAX = 64


# --- The policy ------------------------------------------------------------


@dataclass(frozen=True)
class ChatPolicy:
    """One routing policy: what the router and the fan-out follow, plus the
    numbers it was computed from (for the dashboard)."""

    # Registry names of outside models to leave out of the chat fan-out.
    external_excluded: Tuple[str, ...] = ()
    # Seconds the fan-out holds the outside models back while ours answer
    # (FHI_CHAT_EXTERNAL_HOLD_SECONDS when the policy was computed).
    external_delay_seconds: float = 0.0
    # The outside models in the order chat should ask them: the roster
    # (FHI_CHAT_OUTSIDE_MODELS), with the models that have enough turns
    # reordered among their own places by how well they did. Empty means
    # the roster's own order.
    outside_order: Tuple[str, ...] = ()
    # {name: [score 0..1, turns it was asked]} for the reordered models.
    order_scores: Mapping[str, Tuple[float, int]] = field(default_factory=dict)
    window_minutes: int = 0
    turns_considered: int = 0
    internal_usable_rate: Optional[float] = None
    internal_ttu_p75_ms: Optional[int] = None
    reason: str = REASON_DEFAULT
    # Set on a policy read from a row.
    created_at: Optional[datetime.datetime] = None
    row_id: Optional[int] = None

    @property
    def narrows_nothing(self) -> bool:
        """Whether following this policy routes exactly as the default."""
        return (
            not self.external_excluded
            and not self.outside_order
            and self.external_delay_seconds == 0
        )

    def row_fields(self) -> Dict[str, Any]:
        """Keyword arguments for ChatRoutingPolicy.objects.create (minus the
        source)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "window_minutes": int(self.window_minutes),
            "turns_considered": int(self.turns_considered),
            "external_excluded": list(self.external_excluded),
            "external_delay_seconds": float(self.external_delay_seconds),
            "outside_order": list(self.outside_order),
            "order_scores": {
                name: [float(score), int(turns)]
                for name, (score, turns) in self.order_scores.items()
            },
            "internal_usable_rate": self.internal_usable_rate,
            "internal_ttu_p75_ms": self.internal_ttu_p75_ms,
            "reason": self.reason[:_REASON_MAX],
        }


DEFAULT_POLICY = ChatPolicy()


def narrow_externals(externals: Sequence[T], policy: ChatPolicy) -> List[T]:
    """The outside models, from the router's own list, that the policy
    keeps. Never adds one: the result is always a subset of ``externals``
    in the same order.

    The excluded ones go, except that when that would leave none, the
    first one stays, so a person who allowed outside models still has one
    to fall back on.
    """
    excluded = set(policy.external_excluded)
    kept = [m for m in externals if str(m) not in excluded]
    if not kept and externals:
        kept = list(externals[:1])
    return kept


# --- Computing a policy (pure: no Django, no Temporal) ----------------------


@dataclass
class ModelAggregate:
    """One model's numbers over the window."""

    external: Optional[bool] = None
    # Turns that sent it at least one call.
    asked: int = 0
    # OK turns that delivered its answer, and turns where it came second.
    wins: int = 0
    runner_up: int = 0
    # Calls sent to it (calls held back and never sent are not counted),
    # and those that returned a usable candidate.
    calls: int = 0
    usable_calls: int = 0
    # Calls whose reply Jev scored in a ranking (chat/reply_gate.py), and
    # the sum of those scores (each 0 to 1).
    jev_scored: int = 0
    jev_total: float = 0.0

    @property
    def usable_rate(self) -> Optional[float]:
        return self.usable_calls / self.calls if self.calls else None


@dataclass
class ChatAggregates:
    """What compute_policy needs from the ChatTurn rows: counts and times."""

    window_minutes: int
    turns: int = 0
    ok_turns: int = 0
    timeouts: int = 0
    # OK turns that delivered an outside model's answer.
    external_wins: int = 0
    models: Dict[str, ModelAggregate] = field(default_factory=dict)
    # Turns whose primary pass sent a call to one of our own models, and of
    # those, the turns where one of ours returned a usable answer.
    internal_turns: int = 0
    internal_usable_turns: int = 0
    # For each of those usable turns, the milliseconds until our first
    # usable answer.
    internal_ttu_ms: List[int] = field(default_factory=list)
    # The same two counts over the last RECENT_MINUTES.
    recent_minutes: int = RECENT_MINUTES
    recent_internal_turns: int = 0
    recent_internal_usable_turns: int = 0


@dataclass(frozen=True)
class PolicyRules:
    """The thresholds compute_policy uses. These are product knobs; the
    defaults lean towards changing as little as possible."""

    # Below this many turns in the window, keep the default routing.
    min_turns: int = 50
    # An outside model moves in the order only after this many turns asked
    # it; until then it keeps its place in the roster.
    min_asks_to_order: int = 30
    # The percentile of our time to a usable answer shown on the dashboard.
    delay_percentile: float = 75.0
    # When our models failed to answer usably on at least this share of
    # the window's turns, every outside model is kept.
    keep_all_failure_rate: float = 0.25
    # An outside model other than the top one is left out only after this
    # many turns asked it with fewer than min_wins_to_keep wins.
    min_asks_to_exclude: int = 20
    min_wins_to_keep: int = 1
    # "Healthy": at least this share of its calls returned a usable answer.
    min_healthy_usable_rate: float = 0.5


DEFAULT_RULES = PolicyRules()


def _ratio(part: int, whole: int) -> Optional[float]:
    return part / whole if whole else None


def _percentile(values: Sequence[int], percentile: float) -> Optional[int]:
    """Nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return int(ordered[min(rank, len(ordered)) - 1])


def _choose_exclusions(
    aggregates: ChatAggregates, rules: PolicyRules
) -> Tuple[Tuple[str, ...], Optional[str]]:
    externals = {
        name: m
        for name, m in aggregates.models.items()
        if m.external is True and m.asked > 0
    }
    if not externals:
        return (), None
    usable = _ratio(aggregates.internal_usable_turns, aggregates.internal_turns)
    if usable is None or 1.0 - usable >= rules.keep_all_failure_rate:
        return (), REASON_KEEP_ALL_INTERNALS_FAILING
    ranked = sorted(
        externals.items(),
        key=lambda item: (
            -item[1].wins,
            -(item[1].usable_rate or 0.0),
            -item[1].asked,
            item[0],
        ),
    )
    healthy = [
        name
        for name, m in ranked
        if (m.usable_rate or 0.0) >= rules.min_healthy_usable_rate
    ]
    if not healthy:
        return (), REASON_KEEP_ALL_NO_HEALTHY_EXTERNAL
    top = healthy[0]
    excluded = tuple(
        sorted(
            name
            for name, m in externals.items()
            if name != top
            and m.asked >= rules.min_asks_to_exclude
            and m.wins < rules.min_wins_to_keep
        )
    )
    return excluded, None


def _choose_order(
    aggregates: ChatAggregates, roster: Sequence[str], rules: PolicyRules
) -> Tuple[Tuple[str, ...], Dict[str, Tuple[float, int]], bool]:
    """The roster, with the well-sampled models reordered among the places
    they already hold, best first, and whether Jev's scores ordered them.

    Jev's scores come first: once at least two roster models each have
    ``min_asks_to_order`` replies Jev scored in a ranking, those models are
    ordered by their mean score. Until then a model asked on at least
    ``min_asks_to_order`` turns is scored by the share of those turns where
    its answer was delivered. Models without enough data keep their places,
    so the roster's order stays the prior until the data says otherwise."""
    roster = [name for name in dict.fromkeys(roster) if name]
    by_jev: Dict[str, Tuple[float, int]] = {}
    for name in roster:
        model = aggregates.models.get(name)
        if model is not None and model.jev_scored >= rules.min_asks_to_order:
            by_jev[name] = (model.jev_total / model.jev_scored, model.jev_scored)
    scores: Dict[str, Tuple[float, int]] = {}
    if len(by_jev) >= 2:
        scores = by_jev
    else:
        for name in roster:
            model = aggregates.models.get(name)
            if model is not None and model.asked >= rules.min_asks_to_order:
                scores[name] = (model.wins / model.asked, model.asked)
    places = [i for i, name in enumerate(roster) if name in scores]
    ranked = sorted(
        (name for name in roster if name in scores),
        key=lambda name: (-scores[name][0], roster.index(name)),
    )
    order = list(roster)
    for place, name in zip(places, ranked):
        order[place] = name
    return tuple(order), scores, scores is by_jev


def compute_policy(
    aggregates: ChatAggregates,
    roster: Sequence[str] = (),
    hold_seconds: float = 0.0,
    rules: PolicyRules = DEFAULT_RULES,
) -> ChatPolicy:
    """Turn a window of ChatTurn aggregates into a routing policy.

    * Below ``rules.min_turns`` the routing stays the default (no outside
      model left out, the roster's order, no hold).
    * The top healthy outside model is never left out. Any other is left
      out only once enough turns asked it and it did not win, and never
      while our own models are failing often.
    * The hold is ``hold_seconds`` (FHI_CHAT_EXTERNAL_HOLD_SECONDS), within
      MAX_EXTERNAL_DELAY_SECONDS.
    * The order is the roster reordered by Jev's scores once two models
      have enough, else by how often each well-sampled model's answer was
      delivered (_choose_order).
    """
    recent_rate = _ratio(
        aggregates.recent_internal_usable_turns, aggregates.recent_internal_turns
    )
    ttu_p = _percentile(aggregates.internal_ttu_ms, rules.delay_percentile)

    reasons: List[str] = []
    excluded: Tuple[str, ...] = ()
    delay = 0.0
    order: Tuple[str, ...] = ()
    scores: Dict[str, Tuple[float, int]] = {}
    if aggregates.turns < rules.min_turns:
        reasons.append(REASON_FEW_TURNS)
    else:
        reasons.append(REASON_OK)
        excluded, why_keep = _choose_exclusions(aggregates, rules)
        if why_keep:
            reasons.append(why_keep)
        hold = float(hold_seconds) if math.isfinite(float(hold_seconds)) else 0.0
        delay = min(max(hold, 0.0), MAX_EXTERNAL_DELAY_SECONDS)
        order, scores, by_jev = _choose_order(aggregates, roster, rules)
        if scores:
            reasons.append(REASON_ORDERED_BY_JEV if by_jev else REASON_ORDERED)

    return ChatPolicy(
        external_excluded=excluded,
        external_delay_seconds=float(delay),
        outside_order=order,
        order_scores=scores,
        window_minutes=int(aggregates.window_minutes),
        turns_considered=int(aggregates.turns),
        internal_usable_rate=recent_rate,
        internal_ttu_p75_ms=ttu_p,
        reason=",".join(reasons)[:_REASON_MAX],
    )


def _names(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_NAMES:
        raise ValueError("not a list of names")
    if not all(isinstance(v, str) and 0 < len(v) <= _NAME_MAX for v in value):
        raise ValueError("not a list of names")
    return tuple(value)


def _scores(value: Any) -> Dict[str, Tuple[float, int]]:
    if not isinstance(value, dict) or len(value) > _MAX_NAMES:
        raise ValueError("not a mapping")
    out: Dict[str, Tuple[float, int]] = {}
    for name, pair in value.items():
        if not (isinstance(name, str) and 0 < len(name) <= _NAME_MAX):
            raise ValueError("not a mapping of names to scores")
        if not (isinstance(pair, list) and len(pair) == 2):
            raise ValueError("not a mapping of names to scores")
        score, turns = pair
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("not a score")
        if isinstance(turns, bool) or not isinstance(turns, int) or turns < 0:
            raise ValueError("not a turn count")
        if not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            raise ValueError("score out of bounds")
        out[name] = (float(score), turns)
    return out


def policy_from_row(row: Any) -> Optional[ChatPolicy]:
    """The policy a ChatRoutingPolicy row holds, or None when the row does
    not parse (an unknown schema version, a delay out of bounds, or names
    and counts of the wrong shape). Logs the error class only."""
    try:
        if row.schema_version != SCHEMA_VERSION:
            raise ValueError("unknown schema version")
        delay = float(row.external_delay_seconds)
        if not math.isfinite(delay) or not 0 <= delay <= MAX_EXTERNAL_DELAY_SECONDS:
            raise ValueError("delay out of bounds")
        usable_rate = row.internal_usable_rate
        if usable_rate is not None:
            usable_rate = float(usable_rate)
        return ChatPolicy(
            external_excluded=_names(row.external_excluded),
            external_delay_seconds=delay,
            outside_order=_names(row.outside_order),
            order_scores=_scores(row.order_scores),
            window_minutes=int(row.window_minutes),
            turns_considered=int(row.turns_considered),
            internal_usable_rate=usable_rate,
            internal_ttu_p75_ms=(
                int(row.internal_ttu_p75_ms)
                if row.internal_ttu_p75_ms is not None
                else None
            ),
            reason=str(row.reason or "")[:_REASON_MAX],
            created_at=row.created_at,
            row_id=row.pk,
        )
    except Exception as e:
        logger.warning(f"Ignoring a chat routing policy row: {type(e).__name__}")
        return None


def policy_max_age() -> datetime.timedelta:
    return datetime.timedelta(minutes=settings.FHI_CHAT_POLICY_MAX_AGE_MINUTES)


def policy_is_fresh(
    policy: ChatPolicy, now: Optional[datetime.datetime] = None
) -> bool:
    """Whether a policy read from a row is new enough to follow."""
    if policy.created_at is None:
        return False
    now = now or timezone.now()
    return now - policy.created_at <= policy_max_age()


def newest_policy_row() -> Any:
    """The newest ChatRoutingPolicy row, or None. Synchronous."""
    from fighthealthinsurance.models import ChatRoutingPolicy

    return ChatRoutingPolicy.objects.order_by("-created_at", "-id").first()


def _bound_statements(connection: Any, ms: int) -> None:
    """On PostgreSQL, end any statement of the current transaction that runs
    past ``ms`` (waiting on a lock included). Nothing elsewhere: sqlite in
    tests and development has no statement timeout."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        # set_config(..., true) is SET LOCAL: it ends with the transaction.
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(ms)])


def _read_newest_isolated() -> Optional[ChatPolicy]:
    """Read the newest row on the calling thread's own connection, inside a
    transaction bounded by a statement timeout on PostgreSQL, then close
    that thread's connections (each refresh runs on a new thread, so nothing
    else would).

    Returns None for an empty table or a row that does not parse. Raises on
    a database error, the statement timeout included.
    """
    from django.db import connection, connections, transaction

    try:
        with transaction.atomic():
            _bound_statements(connection, int(POLICY_READ_TIMEOUT_SECONDS * 1000))
            row = newest_policy_row()
    finally:
        connections.close_all()
    return None if row is None else policy_from_row(row)


class _PolicyCache:
    """The newest row's policy for this process, and the refresh that keeps
    it current.

    Chat turns only ever read the cached value; none of them waits on the
    database. A turn that finds the value due for a refresh starts one on a
    thread of its own and goes on with the value it has (the default until
    a first read lands). At most one refresh runs at a time per process;
    turns that come along meanwhile use the cached value too.

    A refresh that reads the table replaces the cached value, with None
    when the table is empty or the newest row does not parse, since both
    mean chat should route by the default. A refresh that fails (a database
    error, the statement timeout) keeps the value already cached. Either
    way the next refresh is due POLICY_CACHE_SECONDS later, so a failing
    database is not asked again on every turn. How old the cached row is,
    is checked on every call (aget_chat_policy), so a kept value stops
    being followed once its row is older than
    FHI_CHAT_POLICY_MAX_AGE_MINUTES, however long refreshes keep failing.

    Why a thread and not database_sync_to_async or the native async ORM
    (CLAUDE.md): both run on the connection's single thread-sensitive
    executor, and cancelling an await there does not stop the query. A
    read stuck on a lock or a dead connection would then hold that
    executor, and the chat's next ORM call (loading the OngoingChat,
    writing the ChatTurn) would queue behind it. The refresh's own thread
    and connection, closed when it is done, keep the read off that
    executor, and the statement timeout on PostgreSQL (with the connect
    timeout in settings) bounds how long it can run.

    Neither bounds a connection that stops answering mid-read, so a read
    still running after POLICY_READ_STUCK_SECONDS is given up on: the next
    turn starts a new one, and the old one's result, should it ever come,
    is dropped like one from before a reset. Once MAX_STUCK_READS given-up
    reads are still waiting, no more are started until one ends, and chat
    keeps the value it has (the default, once that value is too old). The
    read in flight is then kept, and its result used whenever it comes: it
    is the newest read started, and the row's age is checked on every call
    anyway.
    """

    def __init__(self) -> None:
        # Held only to read or swap the fields below, never during a read.
        self._lock = threading.Lock()
        self._policy: Optional[ChatPolicy] = None
        self._refresh_due = float("-inf")
        # The refresh running now, if any, and a count that a reset bumps so
        # a refresh started before it cannot write its result afterwards.
        self._thread: Optional[threading.Thread] = None
        self._started = 0.0
        self._generation = 0
        # Reads given up on as stuck that may still be waiting.
        self._stuck: list[threading.Thread] = []

    def current(self) -> Optional[ChatPolicy]:
        """The cached policy, starting a refresh first when one is due and
        none is running. Never waits on the database."""
        with self._lock:
            policy = self._policy
            now = time.monotonic()
            if self._thread is not None:
                if now - self._started < POLICY_READ_STUCK_SECONDS:
                    return policy
                if not self._give_up_on_running_read():
                    return policy
            elif now < self._refresh_due:
                return policy
            thread = threading.Thread(
                target=self._refresh,
                args=(self._generation,),
                name="fhi-chat-policy-read",
                daemon=True,
            )
            self._thread = thread
            self._started = now
        try:
            thread.start()
        except Exception as e:
            logger.warning(
                f"Could not start a chat routing policy read: {type(e).__name__}"
            )
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                    self._refresh_due = time.monotonic() + POLICY_CACHE_SECONDS
        return policy

    def _give_up_on_running_read(self) -> bool:
        """Stop counting the running read as the one in flight, so a new
        one can start, and drop its result. Returns False, leaving it as the
        one in flight, when MAX_STUCK_READS given-up reads are still
        waiting. Call with the lock held."""
        self._stuck = [t for t in self._stuck if t.is_alive()]
        if len(self._stuck) >= MAX_STUCK_READS:
            return False
        logger.warning("Giving up on a stuck chat routing policy read")
        if self._thread is not None:
            self._stuck.append(self._thread)
        self._thread = None
        self._generation += 1
        return True

    def _refresh(self, generation: int) -> None:
        read = False
        policy: Optional[ChatPolicy] = None
        try:
            policy = _read_newest_isolated()
            read = True
        except Exception as e:
            # Includes the statement timeout. The cached value stays.
            logger.warning(
                f"Could not read the chat routing policy: {type(e).__name__}"
            )
        with self._lock:
            if generation != self._generation:
                return
            self._thread = None
            self._refresh_due = time.monotonic() + POLICY_CACHE_SECONDS
            if read:
                self._policy = policy

    def reset(self) -> Optional[threading.Thread]:
        """Forget the cached value, so the next call starts a refresh.
        Returns the refresh that was running, if any; its result is
        dropped."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._generation += 1
            self._policy = None
            self._refresh_due = float("-inf")
        return thread

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the running refresh, if any.
        Returns whether none is running now. For tests and scripts; the
        chat path never waits."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True


_policy_cache = _PolicyCache()


def reset_chat_policy_cache() -> None:
    _policy_cache.reset()


def wait_for_chat_policy_refresh(timeout: float = 5.0) -> bool:
    """Wait for a running policy refresh to finish (see _PolicyCache.wait)."""
    return _policy_cache.wait(timeout)


async def aget_chat_policy() -> ChatPolicy:
    """The policy chat should follow now. Never raises, and never waits on
    the database.

    DEFAULT_POLICY unless FHI_CHAT_POLICY_APPLY is on and the cached newest
    row parses and is newer than FHI_CHAT_POLICY_MAX_AGE_MINUTES; its age is
    checked on every call. The cached row is refreshed in the background at
    most once per POLICY_CACHE_SECONDS per process (see _PolicyCache), so
    a turn that starts a refresh, like the first turn after a start, routes
    by the value it already had.
    """
    try:
        if not settings.FHI_CHAT_POLICY_APPLY:
            return DEFAULT_POLICY
        policy = _policy_cache.current()
        if policy is None or not policy_is_fresh(policy):
            return DEFAULT_POLICY
        return policy
    except Exception as e:
        logger.warning(f"Chat routing policy unavailable: {type(e).__name__}")
        return DEFAULT_POLICY


# --- Writing a row (synchronous; the command and any scheduled job) ----------


def aggregate_chat_turns(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: Optional[datetime.datetime] = None,
    recent_minutes: int = RECENT_MINUTES,
) -> ChatAggregates:
    """Read ChatTurn metadata into the aggregates compute_policy needs.

    One read from the earlier of the window start and the recent-hour
    start, bucketed in Python. Only labels, statuses, flags and
    times are read.
    """
    from fighthealthinsurance.models import ChatTurn

    now = now or timezone.now()
    window_start = now - datetime.timedelta(minutes=window_minutes)
    recent_start = now - datetime.timedelta(minutes=recent_minutes)
    aggregates = ChatAggregates(
        window_minutes=int(window_minutes), recent_minutes=int(recent_minutes)
    )
    rows = (
        ChatTurn.objects.filter(
            created_at__gte=min(window_start, recent_start),
            created_at__lte=now,
        )
        .order_by()
        .values_list(
            "created_at",
            "outcome",
            "calls",
            "winner_model",
            "winner_external",
            "runner_up_model",
        )
    )
    for created_at, outcome, calls, winner, winner_external, runner_up in rows.iterator(
        chunk_size=500
    ):
        sent = [
            c
            for c in (calls or [])
            if isinstance(c, dict) and c.get("status") != "skipped"
        ]
        ours = [
            c
            for c in sent
            if c.get("external") is False
            and c.get("pass") == "primary"
            and c.get("depth", 0) == 0
        ]
        ours_usable_ms = [
            c["ms"]
            for c in ours
            if c.get("status") == "scored" and isinstance(c.get("ms"), int)
        ]
        if ours and created_at >= recent_start:
            aggregates.recent_internal_turns += 1
            if ours_usable_ms:
                aggregates.recent_internal_usable_turns += 1

        if created_at < window_start:
            continue
        aggregates.turns += 1
        if outcome == "timeout":
            aggregates.timeouts += 1
        if ours:
            aggregates.internal_turns += 1
            if ours_usable_ms:
                aggregates.internal_usable_turns += 1
                aggregates.internal_ttu_ms.append(min(ours_usable_ms))
        asked = set()
        for c in sent:
            name = str(c.get("model") or "")
            if not name:
                continue
            model = aggregates.models.setdefault(name, ModelAggregate())
            if model.external is None and isinstance(c.get("external"), bool):
                model.external = c["external"]
            model.calls += 1
            if c.get("status") == "scored":
                model.usable_calls += 1
            jev = c.get("jev")
            if (
                isinstance(jev, (int, float))
                and not isinstance(jev, bool)
                and math.isfinite(jev)
                and 0.0 <= jev <= 1.0
            ):
                model.jev_scored += 1
                model.jev_total += float(jev)
            asked.add(name)
        for name in asked:
            aggregates.models[name].asked += 1
        if outcome == "ok":
            aggregates.ok_turns += 1
            if winner:
                aggregates.models.setdefault(winner, ModelAggregate()).wins += 1
                if winner_external is True:
                    aggregates.external_wins += 1
        if runner_up:
            aggregates.models.setdefault(runner_up, ModelAggregate()).runner_up += 1
    return aggregates


def compute_current_policy(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: Optional[datetime.datetime] = None,
) -> ChatPolicy:
    """The policy the last ``window_minutes`` of ChatTurn rows give under
    the current settings (the roster and the hold). Synchronous."""
    return compute_policy(
        aggregate_chat_turns(window_minutes, now=now),
        roster=list(getattr(settings, "FHI_CHAT_OUTSIDE_MODELS", None) or []),
        hold_seconds=float(getattr(settings, "FHI_CHAT_EXTERNAL_HOLD_SECONDS", 8.0)),
    )


def prune_old_chat_policies(keep_pk: Any = None) -> int:
    """Delete ChatRoutingPolicy rows written more than POLICY_KEEP_DAYS ago,
    never the row ``keep_pk``. Returns how many were deleted. Synchronous.

    This is retention, not an edit: no row is ever changed, and a row past
    the retention period is deleted whole.
    """
    from fighthealthinsurance.models import ChatRoutingPolicy

    cutoff = timezone.now() - datetime.timedelta(days=POLICY_KEEP_DAYS)
    old = ChatRoutingPolicy.objects.filter(created_at__lt=cutoff)
    if keep_pk is not None:
        old = old.exclude(pk=keep_pk)
    deleted, _by_model = old.delete()
    return int(deleted)


def compute_and_store_chat_policy(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    source: str = "manual",
    now: Optional[datetime.datetime] = None,
) -> Any:
    """Compute a policy from the last ``window_minutes`` of ChatTurn rows and
    append it as a new ChatRoutingPolicy row. Returns the new row.
    Synchronous.

    Rows are never edited: every run appends one. Once the new row is
    stored, rows older than POLICY_KEEP_DAYS are deleted as a separate step
    (prune_old_chat_policies). That step never touches the new row, and if
    it fails the failure is logged (class name only) and the run still
    returns the stored row.
    """
    from fighthealthinsurance.models import ChatRoutingPolicy

    policy = compute_current_policy(window_minutes, now=now)
    row = ChatRoutingPolicy.objects.create(source=source, **policy.row_fields())
    try:
        prune_old_chat_policies(keep_pk=row.pk)
    except Exception as e:
        logger.warning(f"Could not prune old chat routing policies: {type(e).__name__}")
    return row

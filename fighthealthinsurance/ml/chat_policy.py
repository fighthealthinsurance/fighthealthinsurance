"""The chat routing policy: which outside models the chat fan-out asks, and
how long our own models get to answer before the outside ones are sent.

Two loops run at different speeds:

* A slow one (the ``compute_chat_policy`` command, or a scheduled job) reads
  ChatTurn metadata over a window, runs the pure :func:`compute_policy` and
  appends one ChatRoutingPolicy row (:func:`compute_and_store_chat_policy`).
* A fast one, on the chat path, reads the newest row through a short cache
  (:func:`aget_chat_policy`) and hands it to the router, which narrows the
  outside models, and to the fan-out, which holds them back.

The safety rules:

* :data:`DEFAULT_POLICY` is the behaviour without a policy: no outside
  model left out, no caps, no delay.
* Chat follows a row only while FHI_CHAT_POLICY_APPLY is on and the row is
  newer than FHI_CHAT_POLICY_MAX_AGE_MINUTES. With the switch off (the
  default) rows are still computed and shown on the staff usage dashboard,
  but chat routes by the default. An empty table, a row that does not
  parse, a database error and a slow read all give the default too.
* A policy can only narrow the outside models the router already picks
  (:func:`narrow_externals`). It never adds one, and it never overrides a
  person's choice to keep chat on our own models: with that choice the
  router asks no outside model whatever the policy says.
* When none of our own models is selectable the router sets the policy
  aside altogether, so a turn is never left waiting on models that are down.

Everything here is names and numbers. No message, reply or other chat text
is read, kept or written.
"""

import asyncio
import datetime
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

from django.conf import settings
from django.utils import timezone
from loguru import logger

T = TypeVar("T")

# The ChatRoutingPolicy row layout this code writes and understands.
SCHEMA_VERSION = 1

# Reader bounds. A read slower than this gives the default policy for the
# turn; the result (row or default) is reused for the cache time.
POLICY_READ_TIMEOUT_SECONDS = 0.5
POLICY_CACHE_SECONDS = 60.0
# A row asking for a longer delay than this is not trusted (the default
# policy is used instead): the delay comes out of the fan-out's window.
MAX_EXTERNAL_DELAY_SECONDS = 15.0
# Bounds on the names a row may carry.
_MAX_NAMES = 50
_NAME_MAX = 200

# The writer's defaults.
DEFAULT_WINDOW_MINUTES = 24 * 60
RECENT_MINUTES = 60
# Rows older than this are deleted when a new one is written.
POLICY_KEEP_DAYS = 30

# Reason tokens (ChatRoutingPolicy.reason is a comma-joined list of these).
REASON_DEFAULT = "default"
REASON_OK = "ok"
REASON_FEW_TURNS = "few_turns"
REASON_KEEP_ALL_INTERNALS_FAILING = "keep_all_internals_failing"
REASON_KEEP_ALL_NO_HEALTHY_EXTERNAL = "keep_all_no_healthy_external"
REASON_NO_DELAY_QUIET_HOUR = "no_delay_quiet_hour"
REASON_NO_DELAY_INTERNALS_DOWN = "no_delay_internals_down"
REASON_NO_DELAY_FEW_SAMPLES = "no_delay_few_samples"
REASON_CAPPED = "capped"
_REASON_MAX = 64


# --- The policy ------------------------------------------------------------


@dataclass(frozen=True)
class ChatPolicy:
    """One routing policy: what the router and the fan-out follow, plus the
    numbers it was computed from (for the dashboard)."""

    # Registry names of outside models to leave out of the chat fan-out.
    external_excluded: Tuple[str, ...] = ()
    # Seconds the fan-out holds the outside models back while ours answer.
    external_delay_seconds: float = 0.0
    # Models at or over their daily call cap when the policy was computed.
    exhausted: Tuple[str, ...] = ()
    daily_call_caps: Mapping[str, int] = field(default_factory=dict)
    calls_today: Mapping[str, int] = field(default_factory=dict)
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
            and not self.exhausted
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
            "daily_call_caps": dict(self.daily_call_caps),
            "calls_today": dict(self.calls_today),
            "exhausted": list(self.exhausted),
            "internal_usable_rate": self.internal_usable_rate,
            "internal_ttu_p75_ms": self.internal_ttu_p75_ms,
            "reason": self.reason[:_REASON_MAX],
        }


DEFAULT_POLICY = ChatPolicy()


def narrow_externals(externals: Sequence[T], policy: ChatPolicy) -> List[T]:
    """The outside models, from the router's own best-first list, that the
    policy keeps. Never adds one: the result is always a subset of
    ``externals`` in the same order.

    Models at their cap go first. Of the rest, the excluded ones go, except
    that when that would leave none, the best remaining one stays, so a
    person who allowed outside models still has one to fall back on.
    """
    exhausted = set(policy.exhausted)
    excluded = set(policy.external_excluded)
    within_caps = [m for m in externals if str(m) not in exhausted]
    kept = [m for m in within_caps if str(m) not in excluded]
    if not kept and within_caps:
        kept = within_caps[:1]
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
    # Calls sent to each model since UTC midnight.
    calls_today: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyRules:
    """The thresholds compute_policy uses. These are product knobs; the
    defaults lean towards changing as little as possible."""

    # Below this many turns in the window, keep the default routing.
    min_turns: int = 50
    # The delay needs at least this many of the last hour's turns to have
    # asked our models, with at least this share of them answered usably.
    min_recent_turns: int = 3
    min_recent_usable_rate: float = 0.5
    # ... and this many timed usable answers in the window.
    min_ttu_samples: int = 20
    # The delay is this percentile of our time to a usable answer, within
    # these bounds.
    delay_percentile: float = 75.0
    min_delay_seconds: float = 5.0
    max_delay_seconds: float = MAX_EXTERNAL_DELAY_SECONDS
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


def clean_caps(caps: Optional[Mapping[Any, Any]]) -> Dict[str, int]:
    """Only well-formed caps: a model name and a whole number of calls."""
    out: Dict[str, int] = {}
    for name, cap in (caps or {}).items():
        if (
            isinstance(name, str)
            and name
            and len(name) <= _NAME_MAX
            and isinstance(cap, int)
            and not isinstance(cap, bool)
            and cap >= 0
        ):
            out[name] = cap
    return out


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


def _choose_delay(
    aggregates: ChatAggregates,
    rules: PolicyRules,
    recent_rate: Optional[float],
    ttu_p: Optional[int],
) -> Tuple[float, Optional[str]]:
    if aggregates.recent_internal_turns < rules.min_recent_turns:
        return 0.0, REASON_NO_DELAY_QUIET_HOUR
    if recent_rate is None or recent_rate < rules.min_recent_usable_rate:
        return 0.0, REASON_NO_DELAY_INTERNALS_DOWN
    if len(aggregates.internal_ttu_ms) < rules.min_ttu_samples or ttu_p is None:
        return 0.0, REASON_NO_DELAY_FEW_SAMPLES
    seconds = ttu_p / 1000.0
    return (
        min(max(seconds, rules.min_delay_seconds), rules.max_delay_seconds),
        None,
    )


def compute_policy(
    aggregates: ChatAggregates,
    caps: Optional[Mapping[str, int]] = None,
    rules: PolicyRules = DEFAULT_RULES,
) -> ChatPolicy:
    """Turn a window of ChatTurn aggregates into a routing policy.

    * Below ``rules.min_turns`` the routing stays the default (no outside
      model left out, no delay); caps still apply.
    * The top healthy outside model is never left out. Any other is left
      out only once enough turns asked it and it did not win, and never
      while our own models are failing often.
    * The delay is our models' time to a usable answer at
      ``rules.delay_percentile``, clamped to the rules' bounds, and 0 when
      the last hour was quiet or our models were mostly not answering.
    * A model at or over its cap (calls since UTC midnight) is exhausted.
    """
    caps = clean_caps(caps)
    calls_today = {
        str(name): int(count) for name, count in sorted(aggregates.calls_today.items())
    }
    exhausted = tuple(
        sorted(name for name, cap in caps.items() if calls_today.get(name, 0) >= cap)
    )
    recent_rate = _ratio(
        aggregates.recent_internal_usable_turns, aggregates.recent_internal_turns
    )
    ttu_p = _percentile(aggregates.internal_ttu_ms, rules.delay_percentile)

    reasons: List[str] = []
    excluded: Tuple[str, ...] = ()
    delay = 0.0
    if aggregates.turns < rules.min_turns:
        reasons.append(REASON_FEW_TURNS)
    else:
        reasons.append(REASON_OK)
        excluded, why_keep = _choose_exclusions(aggregates, rules)
        delay, why_no_delay = _choose_delay(aggregates, rules, recent_rate, ttu_p)
        reasons.extend(r for r in (why_keep, why_no_delay) if r)
    if exhausted:
        reasons.append(REASON_CAPPED)

    return ChatPolicy(
        external_excluded=excluded,
        external_delay_seconds=float(delay),
        exhausted=exhausted,
        daily_call_caps=caps,
        calls_today=calls_today,
        window_minutes=int(aggregates.window_minutes),
        turns_considered=int(aggregates.turns),
        internal_usable_rate=recent_rate,
        internal_ttu_p75_ms=ttu_p,
        reason=",".join(reasons)[:_REASON_MAX],
    )


# --- Reading the newest row --------------------------------------------------


def _names(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_NAMES:
        raise ValueError("not a list of names")
    if not all(isinstance(v, str) and 0 < len(v) <= _NAME_MAX for v in value):
        raise ValueError("not a list of names")
    return tuple(value)


def _counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("not a mapping")
    cleaned = clean_caps(value)
    if len(cleaned) != len(value):
        raise ValueError("not a mapping of names to counts")
    return cleaned


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
            exhausted=_names(row.exhausted),
            daily_call_caps=_counts(row.daily_call_caps),
            calls_today=_counts(row.calls_today),
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


# (monotonic expiry, policy of the newest row or None). Rebound as one
# tuple and read without a lock, so a reader always sees a matching pair.
_cache: Tuple[float, Optional[ChatPolicy]] = (float("-inf"), None)


def reset_chat_policy_cache() -> None:
    global _cache
    _cache = (float("-inf"), None)


async def _aread_newest() -> Optional[ChatPolicy]:
    from fighthealthinsurance.models import ChatRoutingPolicy

    try:
        row = await asyncio.wait_for(
            ChatRoutingPolicy.objects.order_by("-created_at", "-id").afirst(),
            timeout=POLICY_READ_TIMEOUT_SECONDS,
        )
    except Exception as e:
        # Includes the timeout. The turn goes on with the default.
        logger.warning(f"Could not read the chat routing policy: {type(e).__name__}")
        return None
    if row is None:
        return None
    return policy_from_row(row)


async def aget_chat_policy() -> ChatPolicy:
    """The policy chat should follow now. Never raises.

    DEFAULT_POLICY unless FHI_CHAT_POLICY_APPLY is on and the newest row
    parses and is newer than FHI_CHAT_POLICY_MAX_AGE_MINUTES. The row is
    read at most once per POLICY_CACHE_SECONDS per process, under a
    POLICY_READ_TIMEOUT_SECONDS bound; its age is checked on every call.
    """
    global _cache
    try:
        if not settings.FHI_CHAT_POLICY_APPLY:
            return DEFAULT_POLICY
        now = time.monotonic()
        expires, policy = _cache
        if now >= expires:
            policy = await _aread_newest()
            _cache = (now + POLICY_CACHE_SECONDS, policy)
        if policy is None or not policy_is_fresh(policy):
            return DEFAULT_POLICY
        return policy
    except Exception as e:
        logger.warning(f"Chat routing policy unavailable: {type(e).__name__}")
        return DEFAULT_POLICY


# --- Writing a row (synchronous; the command and any scheduled job) ----------


def configured_daily_call_caps() -> Dict[str, int]:
    """FHI_CHAT_DAILY_CALL_CAPS: a JSON object of {registry name: calls per
    UTC day}. Empty (no caps) when unset or malformed."""
    raw = getattr(settings, "FHI_CHAT_DAILY_CALL_CAPS", "") or ""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        logger.warning(f"Ignoring FHI_CHAT_DAILY_CALL_CAPS: {type(e).__name__}")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Ignoring FHI_CHAT_DAILY_CALL_CAPS: not a JSON object")
        return {}
    return clean_caps(parsed)


def _utc_midnight(now: datetime.datetime) -> datetime.datetime:
    return now.astimezone(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def aggregate_chat_turns(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: Optional[datetime.datetime] = None,
    recent_minutes: int = RECENT_MINUTES,
) -> ChatAggregates:
    """Read ChatTurn metadata into the aggregates compute_policy needs.

    One read from the earliest of the window start, the recent-hour start
    and UTC midnight, bucketed in Python. Only labels, statuses, flags and
    times are read.
    """
    from fighthealthinsurance.models import ChatTurn

    now = now or timezone.now()
    window_start = now - datetime.timedelta(minutes=window_minutes)
    recent_start = now - datetime.timedelta(minutes=recent_minutes)
    day_start = _utc_midnight(now)
    aggregates = ChatAggregates(
        window_minutes=int(window_minutes), recent_minutes=int(recent_minutes)
    )
    rows = (
        ChatTurn.objects.filter(
            created_at__gte=min(window_start, recent_start, day_start),
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
        if created_at >= day_start:
            for c in sent:
                name = str(c.get("model") or "")
                if name:
                    aggregates.calls_today[name] = (
                        aggregates.calls_today.get(name, 0) + 1
                    )

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


def compute_and_store_chat_policy(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    source: str = "manual",
    now: Optional[datetime.datetime] = None,
) -> Any:
    """Compute a policy from the last ``window_minutes`` of ChatTurn rows,
    append it as a ChatRoutingPolicy row and delete rows older than
    POLICY_KEEP_DAYS. Returns the new row. Synchronous."""
    from fighthealthinsurance.models import ChatRoutingPolicy

    policy = compute_policy(
        aggregate_chat_turns(window_minutes, now=now), configured_daily_call_caps()
    )
    row = ChatRoutingPolicy.objects.create(source=source, **policy.row_fields())
    cutoff = timezone.now() - datetime.timedelta(days=POLICY_KEEP_DAYS)
    ChatRoutingPolicy.objects.filter(created_at__lt=cutoff).exclude(pk=row.pk).delete()
    return row

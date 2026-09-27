"""Monthly spend budgets for paid model providers, counted as calls happen.

Every pod adds what it spends to one row per (UTC day, counter) in
SpendCounter, and asks :func:`allows` before spending. A counter over its
budget switches that provider off for that use until the budget allows it
again: the next UTC day for a daily share, the next month for a monthly
one. Nothing here reads or keeps chat text; the counters are names and
amounts in micro-dollars.

The budgets (settings, all in US dollars):

* TypeSafe (Jev), one account: FHI_SPEND_TYPESAFE_MONTHLY_USD (5). Letter
  scoring and denial triage may use all of it. Chat may use at most
  FHI_SPEND_TYPESAFE_CHAT_MONTHLY_USD (3), and stops before total spend
  would reach into FHI_SPEND_TYPESAFE_LETTERS_RESERVE_USD (2), which is kept
  for letters. Chat's month is spread by day: each day it may spend what is
  left of its month divided by the days left, so one busy day cannot use
  the month.
* DeepInfra, for chat only: FHI_SPEND_DEEPINFRA_CHAT_MONTHLY_USD (20), spread
  by day the same way. Other DeepInfra use (appeals, summaries) is counted
  but not capped here.
* Azure OpenAI (GPT-5.5, sponsored): counted in calls for visibility, with no
  cap unless FHI_SPEND_AZURE_CHAT_DAILY_CALLS is set.

A provider that refuses for credit or quota (HTTP 402, or a 429 naming the
quota) is paused for the rest of the UTC day with :func:`pause`, on every
pod, instead of being asked again on every turn.

How it stays off the request path: :func:`allows` reads a per-process copy
of this month's counters and never touches the database. A single daemon
thread per process refreshes that copy (at most every REFRESH_SECONDS) and
applies the increments :func:`record` queues, each on its own connection
closed afterwards. The queue is bounded: past MAX_QUEUED increments the
oldest are dropped with a warning rather than piling up behind a stalled
database. Until the first refresh lands (or while the database cannot be
read), TypeSafe is refused and outside chat models are allowed: losing a
Jev check costs nothing, losing an answer costs the person.
"""

import calendar
import datetime
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from django.conf import settings
from loguru import logger

# Providers and uses. A counter's name is "<provider>:<use>".
TYPESAFE = "typesafe"
DEEPINFRA = "deepinfra"
AZURE = "azure"
CHAT = "chat"
LETTERS = "letters"
TRIAGE = "triage"
OTHER = "other"
PAUSED = "paused"

MICRO = 1_000_000
REFRESH_SECONDS = 30.0
MAX_QUEUED = 1000

# TypeSafe bills input tokens only: $0.042 per million for jev-1.13.0
# (docs.typesafe.ai/models, checked 2026-09-27). Output tokens are free.
TYPESAFE_USD_PER_MTOK = 0.042

# DeepInfra list prices, (input, output) US dollars per million tokens, from
# api.deepinfra.com/models/list on 2026-09-27. Used only when a response
# carries no estimated_cost of its own. A model missing here is charged at
# FALLBACK_USD_PER_MTOK, high on purpose, so an unpriced model uses up its
# budget early rather than late.
DEEPINFRA_USD_PER_MTOK: Dict[str, Tuple[float, float]] = {
    "mistralai/Mistral-Small-3.2-24B-Instruct-2506": (0.075, 0.2),
    "zai-org/GLM-5.3-Flash": (0.15, 0.5),
    "deepseek-ai/DeepSeek-V4.1-Flash": (0.2, 0.6),
    "Qwen/Qwen3.8-2.4T-A95B": (2.0, 6.0),
    "moonshotai/Kimi-K3": (2.85, 14.25),
    "deepseek-ai/DeepSeek-V4-Pro": (1.3, 2.6),
    "google/gemma-4-26B-A4B-it": (0.1, 0.4),
}
FALLBACK_USD_PER_MTOK: Tuple[float, float] = (3.0, 15.0)


def current_use() -> str:
    """The use a model call in this task spends for: chat inside a chat
    reply (ml_metrics' "chat" purpose, set by generate_chat_response), else
    other, so a provider's chat spend is not counted against appeals."""
    from fighthealthinsurance.ml.ml_metrics import ML_CALL_PURPOSE

    return CHAT if ML_CALL_PURPOSE.get() == "chat" else OTHER


def quota_refusal(status: int, body: str) -> bool:
    """Whether a provider's error means credit or quota ran out, not a
    passing rate limit: HTTP 402, or a 429 whose body says so."""
    if status == 402:
        return True
    if status != 429:
        return False
    text = (body or "").lower()
    return any(
        word in text
        for word in ("insufficient_quota", "quota", "credit", "balance", "billing")
    )


def counter(provider: str, use: str) -> str:
    return f"{provider}:{use}"


def _usd_setting(name: str, default: float) -> float:
    value = getattr(settings, name, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def typesafe_cost_micro(input_tokens: Any) -> int:
    """What a TypeSafe answer cost, in micro-dollars, from its usage."""
    try:
        tokens = max(0, int(input_tokens))
    except (TypeError, ValueError):
        return 0
    return round(tokens * TYPESAFE_USD_PER_MTOK)


def deepinfra_cost_micro(model: str, usage: Any) -> int:
    """What a DeepInfra answer cost, in micro-dollars: the response's own
    estimated_cost when it gives one, else list price times its tokens."""
    if not isinstance(usage, Mapping):
        return 0
    estimated = usage.get("estimated_cost")
    if isinstance(estimated, (int, float)) and not isinstance(estimated, bool):
        if estimated >= 0:
            return round(estimated * MICRO)
    price_in, price_out = DEEPINFRA_USD_PER_MTOK.get(model, FALLBACK_USD_PER_MTOK)
    try:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
    except (TypeError, ValueError):
        return 0
    return round(prompt * price_in + completion * price_out)


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


@dataclass
class _Month:
    """This month's counters as last read, plus what this process added
    since. Amounts in micro-dollars (calls for Azure)."""

    month: Tuple[int, int] = (0, 0)
    by_day: Dict[str, Dict[datetime.date, int]] = field(default_factory=dict)
    loaded: bool = False

    def add(self, name: str, day: datetime.date, amount: int) -> None:
        days = self.by_day.setdefault(name, {})
        days[day] = days.get(day, 0) + amount

    def month_total(self, name: str) -> int:
        return sum(self.by_day.get(name, {}).values())

    def day_total(self, name: str, day: datetime.date) -> int:
        return self.by_day.get(name, {}).get(day, 0)


class _Ledger:
    """The per-process view and the one worker thread that keeps it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._view = _Month()
        self._pending: Dict[Tuple[str, datetime.date], int] = {}
        self._queue: "queue.Queue[Tuple[str, datetime.date, int]]" = queue.Queue(
            maxsize=MAX_QUEUED
        )
        self._refreshed_at = float("-inf")
        self._refresh_wanted = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._local_pauses: Dict[str, datetime.date] = {}

    # --- the request path: memory only -------------------------------------

    def snapshot(self) -> _Month:
        self._ensure_worker()
        if time.monotonic() - self._refreshed_at >= REFRESH_SECONDS:
            self._refresh_wanted.set()
        with self._lock:
            view = _Month(
                month=self._view.month,
                by_day={k: dict(v) for k, v in self._view.by_day.items()},
                loaded=self._view.loaded,
            )
            if not getattr(settings, "FHI_SPEND_BACKGROUND", True) and not view.loaded:
                # No worker (tests): this process's own counts are the whole
                # ledger, starting from nothing.
                today = _today()
                view.month = (today.year, today.month)
                view.loaded = True
            for (name, day), amount in self._pending.items():
                if (day.year, day.month) == view.month:
                    view.add(name, day, amount)
        return view

    def add(self, name: str, amount: int, day: Optional[datetime.date] = None) -> None:
        if amount <= 0:
            return
        day = day or _today()
        with self._lock:
            key = (name, day)
            self._pending[key] = self._pending.get(key, 0) + amount
        try:
            self._queue.put_nowait((name, day, amount))
        except queue.Full:
            logger.warning(f"Spend counter {name} not recorded: the queue is full")
        self._ensure_worker()

    def paused_locally(self, name: str) -> bool:
        with self._lock:
            return self._local_pauses.get(name) == _today()

    def pause_locally(self, name: str) -> None:
        with self._lock:
            self._local_pauses[name] = _today()

    # --- the worker ---------------------------------------------------------

    def _ensure_worker(self) -> None:
        if not getattr(settings, "FHI_SPEND_BACKGROUND", True):
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="fhi-spend-ledger", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        self._refresh_wanted.set()
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self._write(*item)
                if self._refresh_wanted.is_set():
                    self._refresh_wanted.clear()
                    self._refresh()
            except Exception as e:
                logger.warning(f"Spend ledger work failed: {type(e).__name__}")
            finally:
                try:
                    from django.db import connections

                    connections.close_all()
                except Exception:
                    pass

    def _write(self, name: str, day: datetime.date, amount: int) -> None:
        from django.db import IntegrityError, transaction
        from django.db.models import F

        from fighthealthinsurance.models import SpendCounter

        updated = SpendCounter.objects.filter(day=day, name=name).update(
            amount=F("amount") + amount
        )
        if not updated:
            try:
                with transaction.atomic():
                    SpendCounter.objects.create(day=day, name=name, amount=amount)
            except IntegrityError:
                # Another pod made the row first.
                SpendCounter.objects.filter(day=day, name=name).update(
                    amount=F("amount") + amount
                )
        with self._lock:
            key = (name, day)
            left = self._pending.get(key, 0) - amount
            if left > 0:
                self._pending[key] = left
            else:
                self._pending.pop(key, None)
            # Now counted in the database: keep it in the view until the next
            # refresh reads it back, so this process never under-counts.
            if (day.year, day.month) == self._view.month:
                self._view.add(name, day, amount)

    def _refresh(self) -> None:
        from fighthealthinsurance.models import SpendCounter

        today = _today()
        first = today.replace(day=1)
        rows = SpendCounter.objects.filter(day__gte=first, day__lte=today).values_list(
            "name", "day", "amount"
        )
        view = _Month(month=(today.year, today.month), loaded=True)
        for name, day, amount in rows:
            view.add(name, day, int(amount))
        with self._lock:
            self._view = view
        self._refreshed_at = time.monotonic()

    # --- tests --------------------------------------------------------------

    def load_for_tests(self, rows: Mapping[Tuple[str, datetime.date], int]) -> None:
        """Replace the view (tests; no database)."""
        today = _today()
        view = _Month(month=(today.year, today.month), loaded=True)
        for (name, day), amount in rows.items():
            view.add(name, day, amount)
        with self._lock:
            self._view = view
            self._pending.clear()
            self._local_pauses.clear()
        self._refreshed_at = time.monotonic()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._view = _Month()
            self._pending.clear()
            self._local_pauses.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._refreshed_at = float("-inf")

    def flush_sync_for_tests(self) -> None:
        """Apply queued increments and refresh on the calling thread."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write(*item)
        self._refresh()


_ledger = _Ledger()


# --- the rules ---------------------------------------------------------------


def _days_left(day: datetime.date) -> int:
    last = calendar.monthrange(day.year, day.month)[1]
    return last - day.day + 1


def _daily_share_left(view: _Month, name: str, monthly_micro: int) -> bool:
    """Whether ``name`` may still spend today: under its month, and under
    today's share of what the month had left this morning."""
    today = _today()
    spent_month = view.month_total(name)
    if spent_month >= monthly_micro:
        return False
    spent_today = view.day_total(name, today)
    left_this_morning = monthly_micro - (spent_month - spent_today)
    return spent_today < left_this_morning / _days_left(today)


def paused(provider: str, use: str) -> bool:
    name = counter(provider, use)
    if _ledger.paused_locally(name):
        return True
    view = _ledger.snapshot()
    return view.day_total(counter(PAUSED, name), _today()) > 0


def allows(provider: str, use: str) -> bool:
    """Whether ``provider`` may be asked now for ``use``. Never waits on the
    database (see the module docstring for what an unread ledger means)."""
    try:
        if paused(provider, use) or paused(provider, "*"):
            return False
        view = _ledger.snapshot()
        if provider == TYPESAFE:
            if not view.loaded:
                return False
            monthly = round(_usd_setting("FHI_SPEND_TYPESAFE_MONTHLY_USD", 5.0) * MICRO)
            total = sum(
                view.month_total(counter(TYPESAFE, u))
                for u in (CHAT, LETTERS, TRIAGE, OTHER)
            )
            if use != CHAT:
                return total < monthly
            reserve = round(
                _usd_setting("FHI_SPEND_TYPESAFE_LETTERS_RESERVE_USD", 2.0) * MICRO
            )
            chat_monthly = round(
                _usd_setting("FHI_SPEND_TYPESAFE_CHAT_MONTHLY_USD", 3.0) * MICRO
            )
            return total < monthly - reserve and _daily_share_left(
                view, counter(TYPESAFE, CHAT), chat_monthly
            )
        if provider == DEEPINFRA and use == CHAT:
            if not view.loaded:
                return True
            monthly = round(
                _usd_setting("FHI_SPEND_DEEPINFRA_CHAT_MONTHLY_USD", 20.0) * MICRO
            )
            return _daily_share_left(view, counter(DEEPINFRA, CHAT), monthly)
        if provider == AZURE and use == CHAT:
            cap = getattr(settings, "FHI_SPEND_AZURE_CHAT_DAILY_CALLS", None)
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                return True
            return view.day_total(counter(AZURE, CHAT), _today()) < cap
        return True
    except Exception as e:
        logger.warning(f"Spend check failed: {type(e).__name__}")
        # The same fail rule as an unread ledger.
        return provider != TYPESAFE


def record(provider: str, use: str, amount: int) -> None:
    """Add ``amount`` (micro-dollars; calls for Azure) to today's counter.
    Never raises and never waits on the database."""
    try:
        _ledger.add(counter(provider, use), int(amount))
    except Exception as e:
        logger.warning(f"Spend not recorded: {type(e).__name__}")


def pause(provider: str, use: str = "*") -> None:
    """Stop asking ``provider`` for ``use`` (every use with "*") until the
    next UTC day, here at once and on other pods at their next refresh."""
    try:
        name = counter(provider, use)
        if _ledger.paused_locally(name):
            return
        _ledger.pause_locally(name)
        _ledger.add(counter(PAUSED, name), 1)
        logger.warning(f"Paused {name} for the rest of the UTC day")
    except Exception as e:
        logger.warning(f"Spend pause not recorded: {type(e).__name__}")


def month_summary() -> Dict[str, float]:
    """This month's spend per counter in US dollars (calls for Azure), for
    the staff pages."""
    view = _ledger.snapshot()
    out: Dict[str, float] = {}
    for name in sorted(view.by_day):
        if name.startswith(PAUSED + ":"):
            continue
        total = view.month_total(name)
        out[name] = float(total) if name.startswith(AZURE + ":") else total / MICRO
    return out

"""LLM usage counted in the database, shared by every pod and process.

ml/llm_usage.py adds to three tables through here:

* LLMUsageDaily: per UTC day, surface, task, model, tier and network class.
* LLMUsageNetworkDaily: per UTC day, surface, network class, ASN name and
  country (no rows for system work, which has no network).
* LLMUsageNetworkWeek: per ISO week and keyed network digest, for the
  people-facing surfaces only. Rolled up into LLMUsageNetworkWeekSummary
  (no keys) and deleted about an hour after the week ends.

Off the request path like spend.py's ledger: record_llm_usage adds to this
process's pending totals in memory, and one daemon thread per process
writes them every WRITE_EVERY_SECONDS (subtracting only what a write
stored, so a failed write is retried, never lost) on its own connection,
closed after each pass. Pending totals are one entry per row, capped at
MAX_PENDING; past the cap new entries are dropped, keyed ones first, and
counted in fhi_llm_usage_ledger_dropped_total. A process that exits loses
at most its last few seconds.

Unlike the Prometheus series, these also count the calls made on the Ray
actors, whose registries nothing scrapes.
"""

import datetime
import threading
from dataclasses import dataclass, fields
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from loguru import logger
from prometheus_client import Counter

WRITE_EVERY_SECONDS = 5.0
MAX_PENDING = 20_000
# How long after a week ends its keyed rows are rolled up: writes still in
# flight from its last seconds land well inside this.
WEEK_GRACE = datetime.timedelta(hours=1)
DAILY_RETENTION_DAYS = 400
NETWORK_RETENTION_DAYS = 90
ALL = "all"

DAILY = "daily"
NETWORK = "network"
WEEK = "week"
_TABLES = (DAILY, NETWORK, WEEK)

LEDGER_DROPPED_TOTAL = Counter(
    "fhi_llm_usage_ledger_dropped_total",
    "LLM usage entries dropped before reaching the database (pending cap, or "
    "a keyed entry for a week already rolled up).",
    labelnames=("table",),
)


@dataclass
class Amounts:
    calls: int = 0
    usage_missing_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "Amounts") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def minus(self, other: "Amounts") -> "Amounts":
        return Amounts(
            **{
                f.name: getattr(self, f.name) - getattr(other, f.name)
                for f in fields(self)
            }
        )

    def as_dict(self) -> Dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def empty(self) -> bool:
        return all(getattr(self, f.name) <= 0 for f in fields(self))


# Each table's key fields, in the order its pending keys hold them.
_KEY_FIELDS: Dict[str, Tuple[str, ...]] = {
    DAILY: ("day", "surface", "task", "model", "tier", "network_class"),
    NETWORK: ("day", "surface", "network_class", "asn_name", "country"),
    WEEK: ("week_start", "key", "surface", "network_class"),
}


def _model_for(table: str) -> Any:
    from fighthealthinsurance.models import (
        LLMUsageDaily,
        LLMUsageNetworkDaily,
        LLMUsageNetworkWeek,
    )

    return {
        DAILY: LLMUsageDaily,
        NETWORK: LLMUsageNetworkDaily,
        WEEK: LLMUsageNetworkWeek,
    }[table]


def _week_closed(week_start: datetime.date, now: datetime.datetime) -> bool:
    end = datetime.datetime.combine(
        week_start + datetime.timedelta(days=7),
        datetime.time(0),
        tzinfo=datetime.timezone.utc,
    )
    return now >= end + WEEK_GRACE


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _Ledger:
    """This process's pending totals and the one thread that writes them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[Tuple[Any, ...], Amounts]] = {
            t: {} for t in _TABLES
        }
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- the request path: memory only -------------------------------------

    def add(self, table: str, key: Tuple[Any, ...], amounts: Amounts) -> None:
        with self._lock:
            pending = self._pending[table]
            entry = pending.get(key)
            if entry is None:
                if self._size() >= MAX_PENDING and not self._make_room(table):
                    LEDGER_DROPPED_TOTAL.labels(table=table).inc()
                    return
                pending[key] = Amounts(**amounts.as_dict())
            else:
                entry.add(amounts)
        self._ensure_worker()

    def _size(self) -> int:
        return sum(len(p) for p in self._pending.values())

    def _make_room(self, table: str) -> bool:
        """Drop one keyed entry so a daily one fits. Under the lock."""
        if table == WEEK or not self._pending[WEEK]:
            return False
        dropped = next(iter(self._pending[WEEK]))
        del self._pending[WEEK][dropped]
        LEDGER_DROPPED_TOTAL.labels(table=WEEK).inc()
        return True

    # --- the worker ---------------------------------------------------------

    def _ensure_worker(self) -> None:
        if not getattr(settings, "FHI_LLM_USAGE_BACKGROUND", True):
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="fhi-llm-usage-ledger", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=WRITE_EVERY_SECONDS)
            self._wake.clear()
            self._tick()

    def _tick(self) -> None:
        try:
            self._write_pending()
        except Exception as e:
            # Pending totals stay pending and are written next time.
            logger.warning(f"LLM usage ledger write failed: {type(e).__name__}")
        finally:
            try:
                from django.db import connections

                connections.close_all()
            except Exception:
                pass

    def _write_pending(self) -> None:
        """Store each pending total, subtracting only what was stored."""
        now = _now()
        for table in _TABLES:
            with self._lock:
                batch = list(self._pending[table].items())
            for key, amounts in batch:
                if table == WEEK and _week_closed(key[0], now):
                    # Its week is rolled up (or about to be): a row written
                    # now would outlive the sweep that promises it is gone.
                    LEDGER_DROPPED_TOTAL.labels(table=WEEK).inc()
                else:
                    _store(table, key, amounts)
                with self._lock:
                    left = self._pending[table].get(key)
                    if left is None:
                        continue
                    remaining = left.minus(amounts)
                    if remaining.empty():
                        del self._pending[table][key]
                    else:
                        self._pending[table][key] = remaining

    # --- tests ----------------------------------------------------------------

    def flush_sync_for_tests(self) -> None:
        self._write_pending()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._pending = {t: {} for t in _TABLES}

    def pending_for_tests(self, table: str) -> Dict[Tuple[Any, ...], Amounts]:
        with self._lock:
            return dict(self._pending[table])


def _store(table: str, key: Tuple[Any, ...], amounts: Amounts) -> None:
    from django.db import IntegrityError, transaction
    from django.db.models import F

    model = _model_for(table)
    lookup = dict(zip(_KEY_FIELDS[table], key))
    increments = {name: F(name) + n for name, n in amounts.as_dict().items() if n}
    if not increments:
        return
    rows = model.objects.filter(**lookup)
    if rows.update(**increments):
        return
    try:
        with transaction.atomic():
            model.objects.create(**lookup, **amounts.as_dict())
    except IntegrityError:
        # Another process made the row first.
        rows.update(**increments)


_ledger = _Ledger()


def add_daily(
    day: datetime.date,
    surface: str,
    task: str,
    model: str,
    tier: str,
    network_class: str,
    amounts: Amounts,
) -> None:
    _ledger.add(DAILY, (day, surface, task, model, tier, network_class), amounts)


def add_network(
    day: datetime.date,
    surface: str,
    network_class: str,
    asn_name: str,
    country: str,
    amounts: Amounts,
) -> None:
    _ledger.add(NETWORK, (day, surface, network_class, asn_name, country), amounts)


def add_week(
    week_start: datetime.date,
    key: str,
    surface: str,
    network_class: str,
    amounts: Amounts,
) -> None:
    _ledger.add(WEEK, (week_start, key, surface, network_class), amounts)


# --- rolling up and sweeping ----------------------------------------------------


@dataclass
class _Group:
    by_key: Dict[str, List[int]]

    def summary(self) -> Dict[str, int]:
        calls = sorted((v[0] for v in self.by_key.values()), reverse=True)
        tokens = sorted((v[1] for v in self.by_key.values()), reverse=True)
        return {
            "networks": len(self.by_key),
            "calls": sum(calls),
            "tokens": sum(tokens),
            "top1_calls": calls[0] if calls else 0,
            "top10_calls": sum(calls[:10]),
            "top1_tokens": tokens[0] if tokens else 0,
            "top10_tokens": sum(tokens[:10]),
        }


def concentration(
    rows: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, int]]:
    """Keyless concentration figures per (surface, network class), per
    surface, per class and overall ("all"), from keyed rows (dicts with key,
    surface, network_class, calls, prompt_tokens, completion_tokens). Each
    network is added up across its rows first."""
    groups: Dict[Tuple[str, str], _Group] = {}
    for row in rows:
        tokens = int(row["prompt_tokens"]) + int(row["completion_tokens"])
        for surface in (row["surface"], ALL):
            for network_class in (row["network_class"], ALL):
                group = groups.setdefault((surface, network_class), _Group({}))
                totals = group.by_key.setdefault(row["key"], [0, 0])
                totals[0] += int(row["calls"])
                totals[1] += tokens
    return {name: group.summary() for name, group in groups.items()}


_ROW_FIELDS = (
    "key",
    "surface",
    "network_class",
    "calls",
    "prompt_tokens",
    "completion_tokens",
)


def _retention_days(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def rollup_and_sweep(now: Optional[datetime.datetime] = None) -> Dict[str, int]:
    """Roll each closed week's keyed rows up into keyless summaries and
    delete them; then apply retention to the daily tables. A week is closed
    WEEK_GRACE after it ends. Running it again changes nothing: a week that
    already has summaries only has late rows deleted."""
    from django.db import IntegrityError, transaction

    from fighthealthinsurance.models import (
        LLMUsageDaily,
        LLMUsageNetworkDaily,
        LLMUsageNetworkWeek,
        LLMUsageNetworkWeekSummary,
    )

    now = now or _now()
    out = {
        "weeks_rolled_up": 0,
        "keyed_rows_deleted": 0,
        "late_rows_deleted": 0,
        "daily_rows_deleted": 0,
        "network_rows_deleted": 0,
    }
    weeks = sorted(
        set(LLMUsageNetworkWeek.objects.values_list("week_start", flat=True))
    )
    for week in weeks:
        if not _week_closed(week, now):
            continue
        try:
            with transaction.atomic():
                rows = list(
                    LLMUsageNetworkWeek.objects.select_for_update()
                    .filter(week_start=week)
                    .values(*_ROW_FIELDS)
                )
                if LLMUsageNetworkWeekSummary.objects.filter(week_start=week).exists():
                    out["late_rows_deleted"] += len(rows)
                else:
                    LLMUsageNetworkWeekSummary.objects.bulk_create(
                        LLMUsageNetworkWeekSummary(
                            week_start=week,
                            surface=surface,
                            network_class=network_class,
                            **figures,
                        )
                        for (surface, network_class), figures in concentration(
                            rows
                        ).items()
                    )
                    out["weeks_rolled_up"] += 1
                    out["keyed_rows_deleted"] += len(rows)
                LLMUsageNetworkWeek.objects.filter(week_start=week).delete()
        except IntegrityError:
            # Another run summarized it first; the next run deletes its rows.
            logger.warning(f"LLM usage week {week} rolled up elsewhere")
    today = now.astimezone(datetime.timezone.utc).date()
    daily_days = _retention_days(
        "FHI_LLM_USAGE_DAILY_RETENTION_DAYS", DAILY_RETENTION_DAYS
    )
    network_days = _retention_days(
        "FHI_LLM_USAGE_NETWORK_RETENTION_DAYS", NETWORK_RETENTION_DAYS
    )
    out["daily_rows_deleted"], _ = LLMUsageDaily.objects.filter(
        day__lt=today - datetime.timedelta(days=daily_days)
    ).delete()
    out["network_rows_deleted"], _ = LLMUsageNetworkDaily.objects.filter(
        day__lt=today - datetime.timedelta(days=network_days)
    ).delete()
    return out

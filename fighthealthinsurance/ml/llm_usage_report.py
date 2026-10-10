"""What the staff pages show of the LLM usage tables (llm_usage_ledger.py).

Database reads only, so the numbers cover every pod, the Temporal worker and
the Ray actors. Counts, labels, ASN names and countries only: no row's
weekly key ever leaves this module, just how many there were and what the
busiest made of the total.
"""

import datetime
from typing import Any, Dict, Iterable, List, Optional

from django.db.models import F, Q, QuerySet, Sum
from django.utils import timezone

from fighthealthinsurance import client_network
from fighthealthinsurance.ml import llm_usage

_TOKENS = F("prompt_tokens") + F("completion_tokens")
_EXTERNAL = Q(tier=llm_usage.EXTERNAL)


def _today(now: datetime.datetime) -> datetime.date:
    return now.astimezone(datetime.timezone.utc).date()


def _sums() -> Dict[str, Any]:
    return {
        "calls_sum": Sum("calls"),
        "prompt_sum": Sum("prompt_tokens"),
        "completion_sum": Sum("completion_tokens"),
        "tokens_sum": Sum(_TOKENS),
        "missing_sum": Sum("usage_missing_calls"),
    }


def _external_sums() -> Dict[str, Any]:
    return {
        "external_calls_sum": Sum("calls", filter=_EXTERNAL),
        "external_tokens_sum": Sum(_TOKENS, filter=_EXTERNAL),
    }


def _percent(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if not whole:
        return None
    return round(100.0 * (part or 0) / whole, 1)


def _row(values: Dict[str, Any]) -> Dict[str, Any]:
    """A template-friendly row: Nones as 0, and the external share."""
    row = dict(values)
    for name in (
        "calls_sum",
        "prompt_sum",
        "completion_sum",
        "tokens_sum",
        "missing_sum",
        "external_calls_sum",
        "external_tokens_sum",
    ):
        if name in row:
            row[name] = int(row[name] or 0)
    row["calls"] = row.pop("calls_sum", 0)
    row["prompt_tokens"] = row.pop("prompt_sum", 0)
    row["completion_tokens"] = row.pop("completion_sum", 0)
    row["tokens"] = row.pop("tokens_sum", 0)
    row["usage_missing_calls"] = row.pop("missing_sum", 0)
    if "external_tokens_sum" in row:
        row["external_calls"] = row.pop("external_calls_sum")
        row["external_tokens"] = row.pop("external_tokens_sum")
        row["external_token_percent"] = _percent(row["external_tokens"], row["tokens"])
    return row


def _grouped(
    qs: QuerySet, fields: Iterable[str], limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    rows = qs.values(*fields).order_by().annotate(**_sums(), **_external_sums())
    ordered = sorted((_row(r) for r in rows), key=lambda r: (-r["tokens"], -r["calls"]))
    return ordered[:limit] if limit else ordered


def _since(days: int, now: datetime.datetime) -> datetime.date:
    return _today(now) - datetime.timedelta(days=days - 1)


def week_concentration(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """This week's keyed rows, all surfaces: how many networks, and the
    share of calls and tokens the busiest one and ten sent. Live, from the
    rows the weekly sweep has not rolled up yet."""
    from fighthealthinsurance.models import LLMUsageNetworkWeek

    now = now or timezone.now()
    week = client_network.week_start(_today(now))
    per_network = (
        LLMUsageNetworkWeek.objects.filter(week_start=week)
        .values("key")
        .order_by()
        .annotate(calls_sum=Sum("calls"), tokens_sum=Sum(_TOKENS))
    )
    networks = per_network.count()
    totals = LLMUsageNetworkWeek.objects.filter(week_start=week).aggregate(
        calls=Sum("calls"), tokens=Sum(_TOKENS)
    )
    calls = int(totals["calls"] or 0)
    tokens = int(totals["tokens"] or 0)
    top_tokens = [
        int(r["tokens_sum"] or 0) for r in per_network.order_by("-tokens_sum")[:10]
    ]
    top_calls = [
        int(r["calls_sum"] or 0) for r in per_network.order_by("-calls_sum")[:10]
    ]
    return {
        "week_start": week,
        "networks": networks,
        "calls": calls,
        "tokens": tokens,
        "top1_token_percent": _percent(top_tokens[0] if top_tokens else 0, tokens),
        "top10_token_percent": _percent(sum(top_tokens), tokens),
        "top1_call_percent": _percent(top_calls[0] if top_calls else 0, calls),
        "top10_call_percent": _percent(sum(top_calls), calls),
    }


def _summary_row(summary: Any) -> Dict[str, Any]:
    return {
        "week_start": summary.week_start,
        "networks": summary.networks,
        "calls": summary.calls,
        "tokens": summary.tokens,
        "top1_token_percent": _percent(summary.top1_tokens, summary.tokens),
        "top10_token_percent": _percent(summary.top10_tokens, summary.tokens),
        "top1_call_percent": _percent(summary.top1_calls, summary.calls),
        "top10_call_percent": _percent(summary.top10_calls, summary.calls),
    }


def status_summary(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The admin status page's panel: today and the last 7 days in totals,
    the last 7 days by surface, top task and network class, and this week's
    network concentration."""
    from fighthealthinsurance.models import LLMUsageDaily

    now = now or timezone.now()
    daily = LLMUsageDaily.objects
    windows = []
    for label, days in (("Today (UTC)", 1), ("Last 7 days", 7)):
        totals = daily.filter(day__gte=_since(days, now)).aggregate(
            **_sums(), **_external_sums()
        )
        windows.append({"label": label, **_row(totals)})
    week = daily.filter(day__gte=_since(7, now))
    unknown = week.filter(surface=llm_usage.UNKNOWN).aggregate(n=Sum("calls"))["n"]
    return {
        "windows": windows,
        "by_surface": _grouped(week, ["surface"]),
        "top_tasks": _grouped(week, ["task"], limit=5),
        "by_network": _grouped(week, ["network_class"]),
        "unknown_calls": int(unknown or 0),
        "this_week": week_concentration(now),
    }


def dashboard_tables(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The model usage dashboard's tables: the last 30 days by surface and
    task, by model, by surface and network class, the top ASNs, and the
    weekly network concentration (8 past weeks and this one)."""
    from fighthealthinsurance.models import (
        LLMUsageDaily,
        LLMUsageNetworkDaily,
        LLMUsageNetworkWeekSummary,
    )

    now = now or timezone.now()
    since = _since(30, now)
    month = LLMUsageDaily.objects.filter(day__gte=since)
    surface_network = _grouped(month, ["surface", "network_class"])
    surface_tokens: Dict[str, int] = {}
    for row in surface_network:
        surface_tokens[row["surface"]] = (
            surface_tokens.get(row["surface"], 0) + row["tokens"]
        )
    for row in surface_network:
        row["surface_token_percent"] = _percent(
            row["tokens"], surface_tokens[row["surface"]]
        )
    asns = (
        LLMUsageNetworkDaily.objects.filter(day__gte=since)
        .exclude(asn_name="")
        .values("asn_name", "country", "network_class")
        .order_by()
        .annotate(**_sums())
    )
    top_asns = sorted(
        (_row(dict(r)) for r in asns), key=lambda r: (-r["tokens"], -r["calls"])
    )[:25]
    past_weeks = [
        _summary_row(s)
        for s in LLMUsageNetworkWeekSummary.objects.filter(
            surface="all", network_class="all"
        ).order_by("-week_start")[:8]
    ]
    return {
        "since": since,
        "by_surface_task": _grouped(month, ["surface", "task"]),
        "by_model": _grouped(month, ["model", "tier"], limit=30),
        "by_surface_network": surface_network,
        "top_asns": top_asns,
        "this_week": week_concentration(now),
        "past_weeks": past_weeks,
    }

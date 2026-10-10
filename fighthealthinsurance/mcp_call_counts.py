"""MCP tool calls counted in the database, per UTC hour, tool and outcome,
for the staff status page. The Prometheus counter in mcp_server.py is per
pod and resets on restart; this one is shared and kept.

Names and counts only. The tool is a name the server registered or
"unknown", never anything a client sent.
"""

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import IntegrityError, transaction
from django.db.models import DateTimeField, F, Max, Q, Sum, Value
from django.db.models.functions import Greatest
from django.utils import timezone

OUTCOMES = ("ok", "refused", "failed")
UNKNOWN_TOOL = "unknown"

# Requests counted by type rather than by tool (mcp_server._counted_request),
# with what each row means on the status page.
INITIALIZE = "_initialize"
TOOLS_LIST = "_tools_list"
TOOLS_CALL = "_tools_call"
OTHER_REQUEST = "_other"
PROTOCOL_ROWS: Tuple[Tuple[str, str], ...] = (
    (INITIALIZE, "initialize: a client connecting"),
    (TOOLS_LIST, "tools/list: a client listing the tools"),
    (TOOLS_CALL, "tools/call read but refused before reaching a tool"),
    (
        OTHER_REQUEST,
        "anything else: notifications, pings, unreadable requests, and ones "
        "refused on a declared length over the limit, never read",
    ),
)
_TOOL_NAME = re.compile(r"[a-z0-9_]{1,64}")

# Rows older than this are swept: the page reads only the last 30 days.
KEEP = datetime.timedelta(days=90)

# The windows the status page shows, newest first.
WINDOWS: Tuple[Tuple[str, datetime.timedelta], ...] = (
    ("24h", datetime.timedelta(hours=24)),
    ("7d", datetime.timedelta(days=7)),
    ("30d", datetime.timedelta(days=30)),
)


def _hour(moment: datetime.datetime) -> datetime.datetime:
    return moment.astimezone(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def bump(tool: str, outcome: str, now: Optional[datetime.datetime] = None) -> None:
    """Add one call to this hour's count. Raises on a database error; the
    caller logs it."""
    from fighthealthinsurance.models import McpToolCallCount

    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if not _TOOL_NAME.fullmatch(tool):
        tool = UNKNOWN_TOOL
    now = now or timezone.now()
    key = {"hour": _hour(now), "tool": tool, "outcome": outcome}
    rows = McpToolCallCount.objects.filter(**key)
    # The later time wins, whichever pod writes last.
    later = Greatest(F("last_call_at"), Value(now, output_field=DateTimeField()))
    if rows.update(count=F("count") + 1, last_call_at=later):
        return
    try:
        with transaction.atomic():
            McpToolCallCount.objects.create(**key, count=1, last_call_at=now)
    except IntegrityError:
        # Another pod made the row first.
        rows.update(count=F("count") + 1, last_call_at=later)
        return
    # A new row comes at most once per tool, outcome and hour.
    McpToolCallCount.objects.filter(hour__lt=now - KEEP).delete()


@dataclass
class ToolCalls:
    """One tool's calls: counts[window][outcome], and its last call in the
    longest window."""

    tool: str
    counts: Dict[str, Dict[str, int]] = field(
        default_factory=lambda: {w: {o: 0 for o in OUTCOMES} for w, _ in WINDOWS}
    )
    last_call_at: Optional[datetime.datetime] = None

    @property
    def cells(self) -> List[Tuple[int, str]]:
        """(count, outcome) for each window and outcome, in table order."""
        return [(self.counts[w][o], o) for w, _ in WINDOWS for o in OUTCOMES]


def summary(
    tools: Iterable[str], now: Optional[datetime.datetime] = None
) -> List[ToolCalls]:
    """A row for each of ``tools``, then, by name, one for any other name
    with calls in the last 30 days ("unknown", or a tool no longer listed).
    One query.

    A window holds every hour that overlaps it, so it can reach up to an
    hour further back than its length, never less."""
    from fighthealthinsurance.models import McpToolCallCount

    now = now or timezone.now()
    listed = list(dict.fromkeys(tools))
    by_tool: Dict[str, ToolCalls] = {t: ToolCalls(t) for t in listed}
    sums = {
        name: Sum("count", filter=Q(hour__gte=_hour(now - span)))
        for name, span in WINDOWS
    }
    rows = (
        McpToolCallCount.objects.filter(hour__gte=_hour(now - WINDOWS[-1][1]))
        .values("tool", "outcome")
        .order_by()
        .annotate(last=Max("last_call_at"), **sums)
    )
    for typed_row in rows:
        # The window sums are named at runtime, so read the row as a dict.
        row: Dict[str, Any] = dict(typed_row)
        calls = by_tool.setdefault(row["tool"], ToolCalls(row["tool"]))
        if calls.last_call_at is None or row["last"] > calls.last_call_at:
            calls.last_call_at = row["last"]
        if row["outcome"] in OUTCOMES:
            for name, _ in WINDOWS:
                calls.counts[name][row["outcome"]] = int(row[name] or 0)
    others = sorted(set(by_tool) - set(listed))
    return [by_tool[t] for t in listed + others]

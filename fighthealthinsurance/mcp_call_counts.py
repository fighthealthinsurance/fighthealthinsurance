"""MCP tool calls counted in the database, per UTC hour, tool and outcome,
for the staff status page. The Prometheus counter in mcp_server.py is per
pod and resets on restart; this one is shared and kept.

Names and counts only. The tool is a name the server registered or
"unknown", never anything a client sent.

Connections are also counted per UTC hour by the app name the client gave
when it connected (bump_connect). That name is what the client sent, so it
is cleaned to a short label (assistant_handoff.client_label), never checked
or trusted, and capped at MAX_CLIENTS_PER_HOUR names an hour.
"""

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type

from django.db import IntegrityError, models, transaction
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

# Connections by app name: past this many names in an hour, a new name
# counts as OTHER_CLIENT, so a client that sends a new name every time can't
# grow the table. A client that gives no name counts as "", and one whose
# name cleans to nothing (no ASCII letters, say) as UNREADABLE_CLIENT. Both
# bucket names hold a "*", which no cleaned name can, so no client can land
# its own connections in one.
MAX_CLIENTS_PER_HOUR = 20
OTHER_CLIENT = "*other*"
UNREADABLE_CLIENT = "*unreadable*"


def _hour(moment: datetime.datetime) -> datetime.datetime:
    return moment.astimezone(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def _add_one(
    model: Type[models.Model],
    key: Dict[str, Any],
    time_field: str,
    now: datetime.datetime,
) -> bool:
    """Add one to the row for ``key``, making it if there is none yet, and
    keep the later of its ``time_field`` and ``now``. True when this call
    made the row."""
    rows = model._default_manager.filter(**key)
    # The later time wins, whichever pod writes last.
    later = Greatest(F(time_field), Value(now, output_field=DateTimeField()))
    if rows.update(count=F("count") + 1, **{time_field: later}):
        return False
    try:
        with transaction.atomic():
            model._default_manager.create(**key, count=1, **{time_field: now})
    except IntegrityError:
        # Another pod made the row first.
        rows.update(count=F("count") + 1, **{time_field: later})
        return False
    return True


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
    if _add_one(McpToolCallCount, key, "last_call_at", now):
        # A new row comes at most once per tool, outcome and hour.
        McpToolCallCount.objects.filter(hour__lt=now - KEEP).delete()


def bump_connect(client: str, now: Optional[datetime.datetime] = None) -> None:
    """Add one connection to this hour's count for the app name the client
    gave, cleaned first. Raises on a database error; the caller logs it."""
    from fighthealthinsurance.assistant_handoff import client_label
    from fighthealthinsurance.models import McpClientConnectCount

    now = now or timezone.now()
    hour = _hour(now)
    label = client_label(client)
    if not label and isinstance(client, str) and client.strip():
        label = UNREADABLE_CLIENT
    this_hour = McpClientConnectCount.objects.filter(hour=hour)
    if (
        not this_hour.filter(client=label).exists()
        and this_hour.count() >= MAX_CLIENTS_PER_HOUR
    ):
        label = OTHER_CLIENT
    key = {"hour": hour, "client": label}
    if _add_one(McpClientConnectCount, key, "last_seen_at", now):
        # A new row comes at most once per name and hour.
        McpClientConnectCount.objects.filter(hour__lt=now - KEEP).delete()


def _window_sums(now: datetime.datetime) -> Dict[str, Sum]:
    """The count summed over each window, named for the window. A window
    holds every hour that overlaps it."""
    return {
        name: Sum("count", filter=Q(hour__gte=_hour(now - span)))
        for name, span in WINDOWS
    }


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
    rows = (
        McpToolCallCount.objects.filter(hour__gte=_hour(now - WINDOWS[-1][1]))
        .values("tool", "outcome")
        .order_by()
        .annotate(last=Max("last_call_at"), **_window_sums(now))
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


@dataclass
class ClientConnects:
    """Connections from one app name: counts[window], and its last
    connection in the longest window."""

    client: str
    counts: Dict[str, int] = field(default_factory=lambda: {w: 0 for w, _ in WINDOWS})
    last_seen_at: Optional[datetime.datetime] = None

    @property
    def cells(self) -> List[int]:
        """The count for each window, in table order."""
        return [self.counts[w] for w, _ in WINDOWS]


def connect_summary(now: Optional[datetime.datetime] = None) -> List[ClientConnects]:
    """A row for each app name with connections in the last 30 days, the
    most connections first, then by name. One query."""
    from fighthealthinsurance.models import McpClientConnectCount

    now = now or timezone.now()
    rows = (
        McpClientConnectCount.objects.filter(hour__gte=_hour(now - WINDOWS[-1][1]))
        .values("client")
        .order_by()
        .annotate(last=Max("last_seen_at"), **_window_sums(now))
    )
    found: List[ClientConnects] = []
    for typed_row in rows:
        # The window sums are named at runtime, so read the row as a dict.
        row: Dict[str, Any] = dict(typed_row)
        counts = {name: int(row[name] or 0) for name, _ in WINDOWS}
        found.append(ClientConnects(row["client"], counts, row["last"]))
    longest = WINDOWS[-1][0]
    return sorted(found, key=lambda c: (-c.counts[longest], c.client))

"""MCP tool calls counted per UTC hour, tool and outcome, and connections
per UTC hour and app name (mcp_call_counts.py)."""

import datetime

from django.test import TestCase

from fighthealthinsurance import mcp_call_counts
from fighthealthinsurance.models import McpClientConnectCount, McpToolCallCount

UTC = datetime.timezone.utc
# Half past one in the morning, so the last 24 hours cross midnight.
NOW = datetime.datetime(2026, 10, 10, 1, 30, tzinfo=UTC)


def counts(row, window):
    return row.counts[window]


def hours(n):
    return datetime.timedelta(hours=n)


class BumpTest(TestCase):
    def test_two_calls_in_one_hour_share_a_row_with_the_later_time(self):
        mcp_call_counts.bump("get_state_help", "ok", now=NOW)
        later = NOW + datetime.timedelta(minutes=20)
        mcp_call_counts.bump("get_state_help", "ok", now=later)
        row = McpToolCallCount.objects.get()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.hour, datetime.datetime(2026, 10, 10, 1, tzinfo=UTC))
        self.assertEqual(row.last_call_at, later)

    def test_the_last_call_never_moves_backwards(self):
        later = NOW + datetime.timedelta(minutes=20)
        mcp_call_counts.bump("get_state_help", "ok", now=later)
        # An earlier call whose write lands second, as from a slower pod.
        mcp_call_counts.bump("get_state_help", "ok", now=NOW)
        row = McpToolCallCount.objects.get()
        self.assertEqual((row.count, row.last_call_at), (2, later))

    def test_a_name_that_is_not_a_tool_name_is_stored_as_unknown(self):
        for name in ("No Such Tool", "x" * 65, "get_state_help\n", "", "drop;table"):
            mcp_call_counts.bump(name, "refused", now=NOW)
        self.assertEqual(
            list(McpToolCallCount.objects.values_list("tool", "count")),
            [("unknown", 5)],
        )

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            mcp_call_counts.bump("get_state_help", "maybe", now=NOW)
        self.assertFalse(McpToolCallCount.objects.exists())

    def test_rows_past_90_days_are_swept_when_a_new_hour_starts(self):
        old = NOW - datetime.timedelta(days=91)
        kept = NOW - datetime.timedelta(days=89)
        mcp_call_counts.bump("get_state_help", "ok", now=old)
        mcp_call_counts.bump("get_state_help", "ok", now=kept)
        mcp_call_counts.bump("search_site", "ok", now=NOW)
        self.assertEqual(
            sorted(McpToolCallCount.objects.values_list("tool", "last_call_at")),
            [("get_state_help", kept), ("search_site", NOW)],
        )


class SummaryTest(TestCase):
    def bump_at(self, tool, outcome, ago):
        mcp_call_counts.bump(tool, outcome, now=NOW - ago)

    def test_counts_per_window_across_a_day_boundary(self):
        self.bump_at("get_state_help", "ok", hours(0))  # 01:30 today
        self.bump_at("get_state_help", "ok", hours(2))  # 23:30 yesterday
        self.bump_at("get_state_help", "failed", hours(25))  # 7 days only
        self.bump_at("get_state_help", "refused", hours(8 * 24))  # 30 days only
        self.bump_at("get_state_help", "ok", hours(31 * 24))  # in none
        [row] = mcp_call_counts.summary(["get_state_help"], now=NOW)
        self.assertEqual(counts(row, "24h"), {"ok": 2, "refused": 0, "failed": 0})
        self.assertEqual(counts(row, "7d"), {"ok": 2, "refused": 0, "failed": 1})
        self.assertEqual(counts(row, "30d"), {"ok": 2, "refused": 1, "failed": 1})
        self.assertEqual(row.last_call_at, NOW)

    def test_a_window_counts_every_hour_that_overlaps_it(self):
        # 01:30 yesterday is in the 01:00 hour, which overlaps the last 24
        # hours; 00:30 yesterday is in the 00:00 hour, which doesn't.
        self.bump_at("get_state_help", "ok", hours(24))
        self.bump_at("get_state_help", "failed", hours(25))
        [row] = mcp_call_counts.summary(["get_state_help"], now=NOW)
        self.assertEqual(counts(row, "24h"), {"ok": 1, "refused": 0, "failed": 0})
        self.assertEqual(counts(row, "7d"), {"ok": 1, "refused": 0, "failed": 1})

    def test_a_call_inside_the_window_is_never_dropped_at_its_edge(self):
        # On the hour, the hour that began exactly 24 hours ago is all inside.
        on_the_hour = datetime.datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
        mcp_call_counts.bump(
            "get_state_help", "ok", now=on_the_hour - hours(23.5)
        )
        [row] = mcp_call_counts.summary(["get_state_help"], now=on_the_hour)
        self.assertEqual(counts(row, "24h")["ok"], 1)

    def test_listed_tools_come_first_then_any_other_name_with_calls(self):
        self.bump_at("unknown", "refused", datetime.timedelta(0))
        self.bump_at("retired_tool", "ok", datetime.timedelta(days=3))
        rows = mcp_call_counts.summary(["search_site", "get_page"], now=NOW)
        self.assertEqual(
            [r.tool for r in rows],
            ["search_site", "get_page", "retired_tool", "unknown"],
        )
        no_calls = [(0, o) for _ in range(3) for o in mcp_call_counts.OUTCOMES]
        self.assertEqual(rows[0].cells, no_calls)
        self.assertIsNone(rows[0].last_call_at)

    def test_one_query(self):
        self.bump_at("get_state_help", "ok", datetime.timedelta(0))
        with self.assertNumQueries(1):
            mcp_call_counts.summary(["get_state_help", "search_site"], now=NOW)


def connects():
    """{app name: count}, summed over hours."""
    totals: dict = {}
    for row in McpClientConnectCount.objects.all():
        totals[row.client] = totals.get(row.client, 0) + row.count
    return totals


class BumpConnectTest(TestCase):
    def test_connections_from_one_app_share_a_row_with_the_later_time(self):
        mcp_call_counts.bump_connect("claude-ai", now=NOW)
        later = NOW + datetime.timedelta(minutes=20)
        mcp_call_counts.bump_connect("claude-ai", now=later)
        row = McpClientConnectCount.objects.get()
        self.assertEqual((row.client, row.count), ("claude-ai", 2))
        self.assertEqual(row.hour, datetime.datetime(2026, 10, 10, 1, tzinfo=UTC))
        self.assertEqual(row.last_seen_at, later)

    def test_each_app_name_gets_its_own_row(self):
        mcp_call_counts.bump_connect("claude-ai", now=NOW)
        mcp_call_counts.bump_connect("codex-mcp-client", now=NOW)
        mcp_call_counts.bump_connect("claude-ai", now=NOW)
        self.assertEqual(connects(), {"claude-ai": 2, "codex-mcp-client": 1})

    def test_a_name_is_cleaned_to_a_short_label_before_it_is_kept(self):
        mcp_call_counts.bump_connect("<b>Claude</b>\n", now=NOW)
        mcp_call_counts.bump_connect("x" * 200, now=NOW)
        self.assertEqual(connects(), {"bClaude/b": 1, "x" * 40: 1})

    def test_a_client_that_gave_no_name_counts_under_an_empty_name(self):
        mcp_call_counts.bump_connect("", now=NOW)
        self.assertEqual(connects(), {"": 1})

    def test_a_name_that_cleans_to_nothing_is_not_counted_as_none_given(self):
        mcp_call_counts.bump_connect("クロード", now=NOW)
        self.assertEqual(connects(), {mcp_call_counts.UNREADABLE_CLIENT: 1})

    def test_a_client_named_other_is_kept_apart_from_the_overflow(self):
        mcp_call_counts.bump_connect("other", now=NOW)
        for i in range(mcp_call_counts.MAX_CLIENTS_PER_HOUR):
            mcp_call_counts.bump_connect(f"app-{i}", now=NOW)
        totals = connects()
        self.assertEqual(
            (totals["other"], totals[mcp_call_counts.OTHER_CLIENT]), (1, 1)
        )

    def test_past_the_hourly_cap_a_new_name_counts_as_other(self):
        cap = mcp_call_counts.MAX_CLIENTS_PER_HOUR
        for i in range(cap):
            mcp_call_counts.bump_connect(f"app-{i}", now=NOW)
        mcp_call_counts.bump_connect("one-too-many", now=NOW)
        mcp_call_counts.bump_connect("another-new-one", now=NOW)
        # A name already counted this hour still gets its own row.
        mcp_call_counts.bump_connect("app-0", now=NOW)
        totals = connects()
        self.assertEqual(totals[mcp_call_counts.OTHER_CLIENT], 2)
        self.assertEqual(totals["app-0"], 2)
        self.assertNotIn("one-too-many", totals)
        self.assertEqual(len(totals), cap + 1)

    def test_the_cap_starts_over_each_hour(self):
        for i in range(mcp_call_counts.MAX_CLIENTS_PER_HOUR):
            mcp_call_counts.bump_connect(f"app-{i}", now=NOW)
        mcp_call_counts.bump_connect("next-hour-app", now=NOW + hours(1))
        self.assertEqual(connects()["next-hour-app"], 1)

    def test_rows_past_90_days_are_swept_when_a_new_row_starts(self):
        old = NOW - datetime.timedelta(days=91)
        kept = NOW - datetime.timedelta(days=89)
        mcp_call_counts.bump_connect("claude-ai", now=old)
        mcp_call_counts.bump_connect("claude-ai", now=kept)
        mcp_call_counts.bump_connect("codex-mcp-client", now=NOW)
        self.assertEqual(
            sorted(McpClientConnectCount.objects.values_list("client", "last_seen_at")),
            [("claude-ai", kept), ("codex-mcp-client", NOW)],
        )

    def test_connections_leave_the_call_count_alone(self):
        mcp_call_counts.bump_connect("claude-ai", now=NOW)
        self.assertFalse(McpToolCallCount.objects.exists())


class ConnectSummaryTest(TestCase):
    def connect_at(self, client, ago):
        mcp_call_counts.bump_connect(client, now=NOW - ago)

    def test_counts_per_window_across_a_day_boundary(self):
        self.connect_at("claude-ai", hours(0))  # 01:30 today
        self.connect_at("claude-ai", hours(2))  # 23:30 yesterday
        self.connect_at("claude-ai", hours(25))  # 7 days only
        self.connect_at("claude-ai", hours(8 * 24))  # 30 days only
        self.connect_at("claude-ai", hours(31 * 24))  # in none
        [row] = mcp_call_counts.connect_summary(now=NOW)
        self.assertEqual(row.counts, {"24h": 2, "7d": 3, "30d": 4})
        self.assertEqual(row.cells, [2, 3, 4])
        self.assertEqual(row.last_seen_at, NOW)

    def test_the_most_connections_come_first_then_by_name(self):
        self.connect_at("b-app", hours(0))
        self.connect_at("a-app", hours(0))
        self.connect_at("c-app", hours(0))
        self.connect_at("c-app", hours(3))
        rows = mcp_call_counts.connect_summary(now=NOW)
        self.assertEqual([r.client for r in rows], ["c-app", "a-app", "b-app"])

    def test_no_connections_in_30_days_gives_no_rows(self):
        self.connect_at("claude-ai", hours(31 * 24))
        self.assertEqual(mcp_call_counts.connect_summary(now=NOW), [])

    def test_one_query(self):
        self.connect_at("claude-ai", hours(0))
        with self.assertNumQueries(1):
            mcp_call_counts.connect_summary(now=NOW)

"""ml/llm_usage_ledger.py against the database: usage lands in one row per
day and label set, keyed network rows live only until their week is rolled
up into keyless summaries, and the hourly sweep does the rolling up."""

import datetime
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from fighthealthinsurance.ml import llm_usage, llm_usage_ledger
from fighthealthinsurance.ml.llm_usage_ledger import Amounts
from fighthealthinsurance.models import (
    LLMUsageDaily,
    LLMUsageNetworkDaily,
    LLMUsageNetworkWeek,
    LLMUsageNetworkWeekSummary,
)

UTC = datetime.timezone.utc
MONDAY = datetime.date(2026, 9, 28)
DAY = datetime.date(2026, 9, 30)


def _at(day, hour=0, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=UTC)


def _one(calls=1, prompt=10, completion=2, missing=0):
    return Amounts(
        calls=calls,
        usage_missing_calls=missing,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )


def _week_row(key, calls, tokens, surface="site", network_class="isp", week=MONDAY):
    return LLMUsageNetworkWeek.objects.create(
        week_start=week,
        key=key,
        surface=surface,
        network_class=network_class,
        calls=calls,
        prompt_tokens=tokens,
        completion_tokens=0,
    )


class LedgerTest(TestCase):
    def setUp(self):
        llm_usage_ledger._ledger.reset_for_tests()

    def tearDown(self):
        llm_usage_ledger._ledger.reset_for_tests()

    def test_increments_add_up_in_one_row(self):
        for _ in range(3):
            llm_usage_ledger.add_daily(
                DAY, "site", "chat_reply", "m", "internal", "isp", _one()
            )
        llm_usage_ledger.add_daily(
            DAY, "site", "chat_reply", "m", "internal", "isp", _one(prompt=0, missing=1)
        )
        llm_usage_ledger._ledger.flush_sync_for_tests()
        row = LLMUsageDaily.objects.get()
        self.assertEqual(
            (row.calls, row.usage_missing_calls, row.prompt_tokens, row.completion_tokens),
            (4, 1, 30, 8),
        )

    def test_two_processes_add_to_the_same_row(self):
        other = llm_usage_ledger._Ledger()
        for ledger in (llm_usage_ledger._ledger, other):
            ledger.add(
                llm_usage_ledger.NETWORK,
                (DAY, "site", "isp", "COMCAST-7922", "US"),
                _one(),
            )
            ledger.flush_sync_for_tests()
        row = LLMUsageNetworkDaily.objects.get()
        self.assertEqual((row.calls, row.prompt_tokens), (2, 20))

    def test_a_failed_write_is_retried_and_nothing_is_lost(self):
        llm_usage_ledger.add_daily(DAY, "site", "other", "m", "internal", "isp", _one())
        real_store = llm_usage_ledger._store
        calls = []

        def failing_once(table, key, amounts):
            calls.append(table)
            if len(calls) == 1:
                raise RuntimeError("database away")
            return real_store(table, key, amounts)

        with patch.object(llm_usage_ledger, "_store", side_effect=failing_once):
            llm_usage_ledger._ledger._tick()
            self.assertFalse(LLMUsageDaily.objects.exists())
            llm_usage_ledger._ledger._tick()
        self.assertEqual(LLMUsageDaily.objects.get().calls, 1)
        self.assertEqual(
            llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.DAILY), {}
        )

    def test_a_keyed_entry_for_a_closed_week_is_dropped(self):
        long_ago = MONDAY - datetime.timedelta(days=14)
        llm_usage_ledger.add_week(long_ago, "k" * 64, "site", "isp", _one())
        llm_usage_ledger._ledger.flush_sync_for_tests()
        self.assertFalse(LLMUsageNetworkWeek.objects.exists())

    def test_the_cap_drops_keyed_entries_first(self):
        with patch.object(llm_usage_ledger, "MAX_PENDING", 2):
            this_week = datetime.date.today() - datetime.timedelta(
                days=datetime.date.today().weekday()
            )
            llm_usage_ledger.add_week(this_week, "a" * 64, "site", "isp", _one())
            llm_usage_ledger.add_week(this_week, "b" * 64, "site", "isp", _one())
            llm_usage_ledger.add_daily(DAY, "site", "other", "m", "internal", "isp", _one())
            # Full of daily rows now? No: one week entry made room for it.
            pending = llm_usage_ledger._ledger
            self.assertEqual(len(pending.pending_for_tests(llm_usage_ledger.WEEK)), 1)
            self.assertEqual(len(pending.pending_for_tests(llm_usage_ledger.DAILY)), 1)
            # A new keyed entry past the cap is dropped, not made room for.
            llm_usage_ledger.add_week(this_week, "c" * 64, "site", "isp", _one())
            self.assertEqual(len(pending.pending_for_tests(llm_usage_ledger.WEEK)), 1)

    def test_recording_a_request_writes_all_three_tables(self):
        meta = {"HTTP_CF_CONNECTING_IP": "203.0.113.77", "HTTP_CF_IPCOUNTRY": "US"}
        with patch(
            "fhi_users.audit.peek_network_info", return_value=("COMCAST-7922", "US")
        ):
            where = llm_usage.origin_from_meta(meta)
        with llm_usage.origin(where), llm_usage.llm_task("questions"):
            llm_usage.record_llm_usage(
                model="m",
                tier="internal",
                usage={"prompt_tokens": 7, "completion_tokens": 3},
            )
        llm_usage_ledger._ledger.flush_sync_for_tests()
        daily = LLMUsageDaily.objects.get()
        self.assertEqual((daily.surface, daily.task, daily.network_class), ("site", "questions", "isp"))
        network = LLMUsageNetworkDaily.objects.get()
        self.assertEqual((network.asn_name, network.country), ("COMCAST-7922", "US"))
        week = LLMUsageNetworkWeek.objects.get()
        self.assertNotIn("203.0.113", week.key)
        self.assertEqual(week.prompt_tokens + week.completion_tokens, 10)


class RollupTest(TestCase):
    def test_a_closed_week_is_summarized_and_its_keys_deleted(self):
        _week_row("a" * 64, calls=5, tokens=500)
        _week_row("b" * 64, calls=3, tokens=100)
        _week_row("a" * 64, calls=1, tokens=50, surface="pro")
        result = llm_usage_ledger.rollup_and_sweep(_at(MONDAY + datetime.timedelta(days=7), 2))
        self.assertEqual(result["weeks_rolled_up"], 1)
        self.assertFalse(LLMUsageNetworkWeek.objects.exists())
        overall = LLMUsageNetworkWeekSummary.objects.get(surface="all", network_class="all")
        # Network a is one network across both surfaces: 6 calls, 550 tokens.
        self.assertEqual(
            (overall.networks, overall.calls, overall.tokens),
            (2, 9, 650),
        )
        self.assertEqual((overall.top1_calls, overall.top1_tokens), (6, 550))
        self.assertEqual((overall.top10_calls, overall.top10_tokens), (9, 650))
        site = LLMUsageNetworkWeekSummary.objects.get(surface="site", network_class="isp")
        self.assertEqual((site.networks, site.top1_tokens), (2, 500))

    def test_no_summary_keeps_a_key(self):
        _week_row("a" * 64, calls=5, tokens=500)
        llm_usage_ledger.rollup_and_sweep(_at(MONDAY + datetime.timedelta(days=8)))
        for summary in LLMUsageNetworkWeekSummary.objects.values():
            self.assertNotIn("a" * 64, str(summary))

    def test_the_current_week_and_its_grace_hour_are_left_alone(self):
        _week_row("a" * 64, calls=5, tokens=500)
        next_monday = MONDAY + datetime.timedelta(days=7)
        for now in (_at(MONDAY + datetime.timedelta(days=3)), _at(next_monday, 0, 59)):
            result = llm_usage_ledger.rollup_and_sweep(now)
            self.assertEqual(result["weeks_rolled_up"], 0)
        self.assertEqual(LLMUsageNetworkWeek.objects.count(), 1)

    def test_running_it_twice_changes_nothing(self):
        _week_row("a" * 64, calls=5, tokens=500)
        now = _at(MONDAY + datetime.timedelta(days=7), 2)
        llm_usage_ledger.rollup_and_sweep(now)
        before = list(LLMUsageNetworkWeekSummary.objects.values_list("surface", "network_class", "calls"))
        result = llm_usage_ledger.rollup_and_sweep(now)
        self.assertEqual(result["weeks_rolled_up"], 0)
        after = list(LLMUsageNetworkWeekSummary.objects.values_list("surface", "network_class", "calls"))
        self.assertEqual(before, after)

    def test_late_rows_are_deleted_without_changing_the_summary(self):
        _week_row("a" * 64, calls=5, tokens=500)
        now = _at(MONDAY + datetime.timedelta(days=7), 2)
        llm_usage_ledger.rollup_and_sweep(now)
        _week_row("z" * 64, calls=99, tokens=9999)
        result = llm_usage_ledger.rollup_and_sweep(now)
        self.assertEqual(result["late_rows_deleted"], 1)
        self.assertFalse(LLMUsageNetworkWeek.objects.exists())
        overall = LLMUsageNetworkWeekSummary.objects.get(surface="all", network_class="all")
        self.assertEqual(overall.calls, 5)

    def test_retention_deletes_old_daily_rows(self):
        today = DAY
        old = today - datetime.timedelta(days=401)
        for day in (today, old):
            LLMUsageDaily.objects.create(
                day=day, surface="site", task="other", model="m", tier="internal", network_class="isp"
            )
            LLMUsageNetworkDaily.objects.create(day=day, surface="site", network_class="isp")
        mid = today - datetime.timedelta(days=100)
        LLMUsageNetworkDaily.objects.create(day=mid, surface="site", network_class="isp")
        with override_settings(
            FHI_LLM_USAGE_DAILY_RETENTION_DAYS="400",
            FHI_LLM_USAGE_NETWORK_RETENTION_DAYS="junk",
        ):
            result = llm_usage_ledger.rollup_and_sweep(_at(today, 12))
        self.assertEqual(result["daily_rows_deleted"], 1)
        # "junk" falls back to the 90-day default.
        self.assertEqual(result["network_rows_deleted"], 2)
        self.assertEqual(list(LLMUsageNetworkDaily.objects.values_list("day", flat=True)), [today])

    def test_the_hourly_sweep_rolls_up(self):
        _week_row("a" * 64, calls=5, tokens=500, week=MONDAY - datetime.timedelta(days=28))
        call_command("sweep_assistant_drafts")
        self.assertFalse(LLMUsageNetworkWeek.objects.exists())
        self.assertTrue(LLMUsageNetworkWeekSummary.objects.exists())

    def test_the_manual_command_rolls_up(self):
        _week_row("a" * 64, calls=5, tokens=500, week=MONDAY - datetime.timedelta(days=28))
        call_command("rollup_llm_usage")
        self.assertFalse(LLMUsageNetworkWeek.objects.exists())

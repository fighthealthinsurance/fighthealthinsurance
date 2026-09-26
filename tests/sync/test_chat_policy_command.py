"""Writing chat routing policies: the ChatTurn aggregation, the stored row
and the compute_chat_policy command."""

import datetime
import io

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.ml.chat_policy import (
    aggregate_chat_turns,
    compute_and_store_chat_policy,
    configured_daily_call_caps,
)
from fighthealthinsurance.models import ChatRoutingPolicy, ChatTurn, OngoingChat

# A fixed "now" mid-day UTC, so the UTC-midnight boundary is predictable.
NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)


def _call(model, status="scored", ms=1000, external=False, pass_kind="primary"):
    return {
        "model": model,
        "backend": "",
        "external": external,
        "pass": pass_kind,
        "depth": 0,
        "history": "truncated",
        "variant": "",
        "status": status,
        "error": "",
        "ms": ms if status not in ("late", "skipped") else None,
        "score": 8820.0 if status == "scored" else None,
    }


class _Seeded(TestCase):
    def setUp(self):
        self.chat = OngoingChat.objects.create()

    def _turn(self, at, **fields):
        defaults = dict(
            outcome="ok",
            use_external=True,
            backends=["fhi-local", "claude"],
            winner_model="fhi-local",
            winner_external=False,
            calls=[_call("fhi-local"), _call("claude", external=True)],
        )
        defaults.update(fields)
        turn = ChatTurn.objects.create(chat=self.chat, **defaults)
        ChatTurn.objects.filter(pk=turn.pk).update(created_at=at)
        return turn


class AggregateChatTurnsTest(_Seeded):
    def test_turns_in_the_window_are_counted_and_older_ones_are_not(self):
        self._turn(NOW - datetime.timedelta(minutes=30))
        self._turn(NOW - datetime.timedelta(hours=5))
        self._turn(NOW - datetime.timedelta(hours=30))
        aggregates = aggregate_chat_turns(window_minutes=24 * 60, now=NOW)
        self.assertEqual(aggregates.turns, 2)
        self.assertEqual(aggregates.models["fhi-local"].asked, 2)
        self.assertEqual(aggregates.models["fhi-local"].wins, 2)
        self.assertIs(aggregates.models["claude"].external, True)

    def test_our_time_to_a_usable_answer_is_the_first_one_per_turn(self):
        self._turn(
            NOW - datetime.timedelta(minutes=10),
            calls=[
                _call("fhi-local", ms=9000),
                _call("fhi-local", ms=4000),
                _call("may", "empty", ms=100),
                _call("claude", ms=500, external=True),
                _call("fhi-local", ms=50, pass_kind="retry"),
            ],
        )
        self._turn(
            NOW - datetime.timedelta(minutes=20),
            outcome="failed",
            winner_model="",
            calls=[_call("fhi-local", "error"), _call("fhi-local", "late")],
        )
        aggregates = aggregate_chat_turns(window_minutes=60, now=NOW)
        self.assertEqual(aggregates.internal_ttu_ms, [4000])
        self.assertEqual(
            (aggregates.internal_turns, aggregates.internal_usable_turns), (2, 1)
        )
        self.assertEqual(
            (
                aggregates.recent_internal_turns,
                aggregates.recent_internal_usable_turns,
            ),
            (2, 1),
        )

    def test_the_recent_hour_is_counted_apart_from_the_window(self):
        self._turn(NOW - datetime.timedelta(minutes=30))
        self._turn(
            NOW - datetime.timedelta(minutes=90),
            calls=[_call("fhi-local", "error")],
        )
        aggregates = aggregate_chat_turns(window_minutes=24 * 60, now=NOW)
        self.assertEqual(aggregates.recent_internal_turns, 1)
        self.assertEqual(aggregates.recent_internal_usable_turns, 1)
        self.assertEqual(aggregates.internal_turns, 2)

    def test_calls_held_back_and_never_sent_are_not_counted(self):
        self._turn(
            NOW - datetime.timedelta(minutes=5),
            external_start="skipped",
            calls=[
                _call("fhi-local"),
                _call("claude", "skipped", external=True),
                _call("claude", "skipped", external=True),
            ],
        )
        aggregates = aggregate_chat_turns(window_minutes=60, now=NOW)
        self.assertNotIn("claude", aggregates.calls_today)
        self.assertNotIn("claude", aggregates.models)
        self.assertEqual(aggregates.calls_today, {"fhi-local": 1})

    def test_calls_today_start_at_utc_midnight(self):
        midnight = NOW.replace(hour=0)
        self._turn(midnight + datetime.timedelta(minutes=1))
        self._turn(midnight - datetime.timedelta(minutes=1))
        # A one-hour window still counts every call since midnight.
        aggregates = aggregate_chat_turns(window_minutes=60, now=NOW)
        self.assertEqual(aggregates.calls_today, {"fhi-local": 1, "claude": 1})
        self.assertEqual(aggregates.turns, 0)

    def test_wins_count_on_ok_turns_only(self):
        self._turn(
            NOW - datetime.timedelta(minutes=5),
            winner_model="claude",
            winner_external=True,
        )
        self._turn(
            NOW - datetime.timedelta(minutes=6),
            outcome="timeout",
            winner_model="claude",
            winner_external=True,
        )
        aggregates = aggregate_chat_turns(window_minutes=60, now=NOW)
        self.assertEqual(aggregates.models["claude"].wins, 1)
        self.assertEqual(aggregates.external_wins, 1)
        self.assertEqual(aggregates.timeouts, 1)
        self.assertEqual(aggregates.ok_turns, 1)


class StoreChatPolicyTest(_Seeded):
    def test_a_row_is_appended_and_old_rows_are_pruned(self):
        old = ChatRoutingPolicy.objects.create(source="manual", window_minutes=60)
        recent = ChatRoutingPolicy.objects.create(source="manual", window_minutes=60)
        ChatRoutingPolicy.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=31)
        )
        ChatRoutingPolicy.objects.filter(pk=recent.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=29)
        )
        self._turn(timezone.now() - datetime.timedelta(minutes=5))

        row = compute_and_store_chat_policy(window_minutes=60, source="temporal")

        self.assertEqual(row.source, "temporal")
        self.assertEqual(row.schema_version, 1)
        self.assertEqual((row.window_minutes, row.turns_considered), (60, 1))
        # One turn is below the minimum: the default routing, with a reason.
        self.assertEqual(row.external_excluded, [])
        self.assertEqual(row.external_delay_seconds, 0.0)
        self.assertEqual(row.reason, "few_turns")
        self.assertEqual(
            set(ChatRoutingPolicy.objects.values_list("pk", flat=True)),
            {recent.pk, row.pk},
        )

    @override_settings(FHI_CHAT_DAILY_CALL_CAPS='{"claude": 1, "bad": "x"}')
    def test_configured_caps_mark_a_model_exhausted(self):
        # A second ago, so the call is from today (UTC) whenever this runs.
        self._turn(timezone.now() - datetime.timedelta(seconds=1))
        self.assertEqual(configured_daily_call_caps(), {"claude": 1})
        row = compute_and_store_chat_policy(window_minutes=60)
        self.assertEqual(row.daily_call_caps, {"claude": 1})
        self.assertEqual(row.exhausted, ["claude"])

    def test_malformed_caps_mean_no_caps(self):
        for raw in ("not json", "[1, 2]", ""):
            with override_settings(FHI_CHAT_DAILY_CALL_CAPS=raw):
                self.assertEqual(configured_daily_call_caps(), {})

    def test_the_row_holds_names_and_numbers_only(self):
        self._turn(timezone.now() - datetime.timedelta(seconds=1))
        row = compute_and_store_chat_policy(window_minutes=60)
        for field in ChatRoutingPolicy._meta.concrete_fields:
            if field.get_internal_type() == "TextField":
                self.fail(f"{field.name} is a text field")
        for name in row.external_excluded + row.exhausted:
            self.assertIn(name, {"fhi-local", "claude"})
        self.assertEqual(set(row.calls_today), {"fhi-local", "claude"})


class ComputeChatPolicyCommandTest(_Seeded):
    def test_the_command_stores_one_row(self):
        self._turn(timezone.now() - datetime.timedelta(minutes=5))
        out = io.StringIO()
        call_command("compute_chat_policy", "--window-minutes", "120", stdout=out)
        row = ChatRoutingPolicy.objects.get()
        self.assertEqual(row.source, "manual")
        self.assertEqual(row.window_minutes, 120)
        self.assertIn(f"Stored chat routing policy {row.pk}.", out.getvalue())
        self.assertIn("external_delay_seconds: 0.0", out.getvalue())

    def test_a_dry_run_stores_nothing(self):
        out = io.StringIO()
        call_command("compute_chat_policy", "--dry-run", stdout=out)
        self.assertFalse(ChatRoutingPolicy.objects.exists())
        self.assertIn("Dry run", out.getvalue())
        self.assertIn("reason: few_turns", out.getvalue())

    def test_a_window_out_of_range_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("compute_chat_policy", "--window-minutes", "0")
        self.assertFalse(ChatRoutingPolicy.objects.exists())

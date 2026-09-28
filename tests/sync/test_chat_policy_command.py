"""Writing chat routing policies: the ChatTurn aggregation, the stored row
and the compute_chat_policy command."""

import datetime
import io
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import IntegrityError, OperationalError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.ml import chat_policy
from fighthealthinsurance.ml.chat_policy import (
    aggregate_chat_turns,
    compute_and_store_chat_policy,
    prune_old_chat_policies,
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

    def test_jevs_ranking_scores_are_summed_per_model(self):
        ranked = dict(_call("claude", external=True), jev=0.9)
        self._turn(
            NOW - datetime.timedelta(minutes=5),
            calls=[
                dict(_call("fhi-local"), jev=0.5),
                ranked,
                # Out of range, not a number, or a bool: not counted.
                dict(_call("claude", external=True), jev=1.5),
                dict(_call("claude", external=True), jev="0.9"),
                dict(_call("claude", external=True), jev=True),
            ],
        )
        self._turn(NOW - datetime.timedelta(minutes=6), calls=[ranked])
        aggregates = aggregate_chat_turns(window_minutes=60, now=NOW)
        claude = aggregates.models["claude"]
        self.assertEqual(claude.jev_scored, 2)
        self.assertAlmostEqual(claude.jev_total, 1.8)
        self.assertEqual(aggregates.models["fhi-local"].jev_scored, 1)

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
        self.assertNotIn("claude", aggregates.models)
        self.assertEqual(aggregates.models["fhi-local"].calls, 1)

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
        recent_before = ChatRoutingPolicy.objects.filter(pk=recent.pk).values().get()
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
        # The row it kept is exactly as it was: rows are never edited.
        self.assertEqual(
            ChatRoutingPolicy.objects.filter(pk=recent.pk).values().get(),
            recent_before,
        )

    def test_pruning_never_deletes_the_row_it_is_told_to_keep(self):
        kept = ChatRoutingPolicy.objects.create(source="manual", window_minutes=60)
        old = ChatRoutingPolicy.objects.create(source="manual", window_minutes=60)
        ChatRoutingPolicy.objects.filter(pk__in=[kept.pk, old.pk]).update(
            created_at=timezone.now() - datetime.timedelta(days=45)
        )
        self.assertEqual(prune_old_chat_policies(keep_pk=kept.pk), 1)
        self.assertEqual(
            list(ChatRoutingPolicy.objects.values_list("pk", flat=True)), [kept.pk]
        )

    def test_a_pruning_failure_keeps_the_new_row_and_does_not_fail_the_run(self):
        self._turn(timezone.now() - datetime.timedelta(minutes=5))
        with patch(
            "fighthealthinsurance.ml.chat_policy.prune_old_chat_policies",
            side_effect=OperationalError("statement timeout"),
        ) as prune:
            row = compute_and_store_chat_policy(window_minutes=60)
        prune.assert_called_once_with(keep_pk=row.pk)
        self.assertEqual(
            list(ChatRoutingPolicy.objects.values_list("pk", flat=True)), [row.pk]
        )

    def test_a_run_id_stores_at_most_one_row_for_its_run(self):
        self._turn(timezone.now() - datetime.timedelta(minutes=5))
        with (
            patch.object(
                chat_policy,
                "aggregate_chat_turns",
                side_effect=chat_policy.aggregate_chat_turns,
            ) as read,
            patch.object(
                chat_policy,
                "prune_old_chat_policies",
                side_effect=chat_policy.prune_old_chat_policies,
            ) as prune,
        ):
            first = compute_and_store_chat_policy(
                window_minutes=60, source="temporal", run_id="run-1"
            )
            again = compute_and_store_chat_policy(
                window_minutes=60, source="temporal", run_id="run-1"
            )
            other = compute_and_store_chat_policy(
                window_minutes=60, source="temporal", run_id="run-2"
            )
        self.assertEqual(again.pk, first.pk)
        self.assertNotEqual(other.pk, first.pk)
        self.assertEqual(
            sorted(ChatRoutingPolicy.objects.values_list("run_id", flat=True)),
            ["run-1", "run-2"],
        )
        # The second call for run-1 found its row: no read, no prune.
        self.assertEqual((read.call_count, prune.call_count), (2, 2))

    def test_an_insert_that_meets_the_runs_row_returns_that_row(self):
        """Two attempts of one run racing: both miss the other's row at the
        start, and the one whose insert meets the unique run id returns the
        row the other stored, adding and pruning nothing."""
        stored = ChatRoutingPolicy.objects.create(
            source="temporal", window_minutes=60, run_id="run-1"
        )
        real_lookup = chat_policy._row_for_run
        lookups = []

        def lookup(run_id):
            lookups.append(run_id)
            # The first lookup ran before the other attempt's insert.
            return None if len(lookups) == 1 else real_lookup(run_id)

        with (
            patch.object(chat_policy, "_row_for_run", side_effect=lookup),
            patch.object(chat_policy, "prune_old_chat_policies") as prune,
        ):
            row = compute_and_store_chat_policy(
                window_minutes=60, source="temporal", run_id="run-1"
            )
        self.assertEqual(row.pk, stored.pk)
        self.assertEqual(lookups, ["run-1", "run-1"])
        self.assertEqual(
            list(ChatRoutingPolicy.objects.values_list("pk", flat=True)), [stored.pk]
        )
        prune.assert_not_called()

    def test_without_a_run_id_an_integrity_error_is_raised(self):
        with patch.object(
            ChatRoutingPolicy.objects, "create", side_effect=IntegrityError("x")
        ):
            with self.assertRaises(IntegrityError):
                compute_and_store_chat_policy(window_minutes=60)

    def test_a_run_id_is_unique_and_an_empty_one_may_repeat(self):
        for _ in range(2):
            ChatRoutingPolicy.objects.create(source="manual", window_minutes=60)
        ChatRoutingPolicy.objects.create(
            source="temporal", window_minutes=60, run_id="run-1"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChatRoutingPolicy.objects.create(
                source="temporal", window_minutes=60, run_id="run-1"
            )
        self.assertEqual(
            ChatRoutingPolicy.objects.filter(run_id__isnull=True).count(), 2
        )

    @override_settings(
        FHI_CHAT_OUTSIDE_MODELS=["claude", "deepseek"],
        FHI_CHAT_EXTERNAL_HOLD_SECONDS=6.0,
    )
    def test_the_row_carries_the_roster_order_and_the_hold_setting(self):
        for minutes in range(1, 60):
            self._turn(timezone.now() - datetime.timedelta(minutes=minutes))
        row = compute_and_store_chat_policy(window_minutes=120)
        self.assertEqual(row.outside_order, ["claude", "deepseek"])
        self.assertEqual(row.external_delay_seconds, 6.0)
        # claude was asked on 59 turns and never delivered.
        self.assertEqual(row.order_scores, {"claude": [0.0, 59]})

    def test_the_row_holds_names_and_numbers_only(self):
        self._turn(timezone.now() - datetime.timedelta(seconds=1))
        row = compute_and_store_chat_policy(window_minutes=60)
        for field in ChatRoutingPolicy._meta.concrete_fields:
            if field.get_internal_type() == "TextField":
                self.fail(f"{field.name} is a text field")
        for name in row.external_excluded + row.outside_order:
            self.assertIn(name, {"fhi-local", "claude"})


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
        self.assertIsNone(row.run_id)

    def test_the_command_writes_a_new_row_every_time(self):
        """The command has no run id: each call is a run of its own."""
        self._turn(timezone.now() - datetime.timedelta(minutes=5))
        for _ in range(2):
            call_command(
                "compute_chat_policy", "--window-minutes", "120", stdout=io.StringIO()
            )
        self.assertEqual(
            list(ChatRoutingPolicy.objects.values_list("source", "run_id")),
            [("manual", None), ("manual", None)],
        )

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

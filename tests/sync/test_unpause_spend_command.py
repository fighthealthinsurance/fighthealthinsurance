"""manage.py unpause_spend: lifts a provider's credit or quota pause by hand,
and stores the lift before the short-lived command process exits, so every
pod reads it at its next refresh."""

import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from fighthealthinsurance.management.commands.unpause_spend import Command
from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import SpendCounter


class UnpauseSpendCommandTest(TestCase):
    def setUp(self):
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)
        self.today = datetime.datetime.now(datetime.timezone.utc).date()

    def _paused_elsewhere(self, name):
        # Another pod's pause, as it stands in the database.
        SpendCounter.objects.create(
            day=self.today, name=spend.counter(spend.PAUSED, name), amount=1
        )

    def _stored(self, name):
        return SpendCounter.objects.get(
            day=self.today, name=spend.counter(spend.PAUSED, name)
        ).amount

    def _run(self, *args):
        out = StringIO()
        call_command("unpause_spend", *args, stdout=out)
        return out.getvalue()

    def test_the_command_stores_the_lift_for_every_pod(self):
        self._paused_elsewhere("anthropic:*")
        self._run("anthropic")
        self.assertEqual(self._stored("anthropic:*"), 0)

    def test_the_command_says_it_lifted_the_pause(self):
        self._paused_elsewhere("anthropic:*")
        self.assertIn("Lifted the pause on anthropic:*", self._run("anthropic"))

    def test_the_command_says_when_nothing_was_paused(self):
        self.assertIn("deepinfra:* is not paused today", self._run("deepinfra"))

    def test_the_command_lifts_one_use_with_use(self):
        self._paused_elsewhere("deepinfra:chat")
        self._run("deepinfra", "--use", "chat")
        self.assertEqual(self._stored("deepinfra:chat"), 0)

    def test_the_command_names_the_pauses_it_left(self):
        self._paused_elsewhere("deepinfra:chat")
        self.assertIn("Still paused: deepinfra:chat", self._run("deepinfra"))

    def test_the_command_rejects_an_unknown_provider(self):
        with self.assertRaises(CommandError):
            self._run("openai")

    def test_the_help_lists_every_provider(self):
        self.assertTrue(all(p in Command.help for p in spend.PROVIDERS))

    def test_the_command_fails_when_the_ledger_cannot_be_read(self):
        with patch.object(
            spend._Ledger, "_refresh", side_effect=RuntimeError("database away")
        ):
            with self.assertRaises(CommandError):
                self._run("anthropic")

    def test_a_lift_whose_store_fails_says_to_run_it_again(self):
        """The lift was made here, but whether it reached the other pods is
        unknown: the command says so instead of "nothing lifted"."""
        self._paused_elsewhere("anthropic:*")
        real_sync = spend.sync_now
        calls = []

        def sync_then_fail():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("database away")
            real_sync()

        with patch.object(spend, "sync_now", side_effect=sync_then_fail):
            with self.assertRaisesRegex(CommandError, "run it again"):
                self._run("anthropic")

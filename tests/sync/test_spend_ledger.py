"""ml/spend.py against the database: increments land in one SpendCounter row
per UTC day and name, and a refresh reads this month back."""

import datetime

from django.test import TestCase

from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import SpendCounter


class SpendLedgerTest(TestCase):
    def setUp(self):
        spend._ledger.reset_for_tests()

    def tearDown(self):
        spend._ledger.reset_for_tests()

    def test_increments_add_up_in_one_row_per_day_and_name(self):
        spend.record(spend.DEEPINFRA, spend.CHAT, 1000)
        spend.record(spend.DEEPINFRA, spend.CHAT, 250)
        spend.record(spend.TYPESAFE, spend.LETTERS, 42)
        spend._ledger.flush_sync_for_tests()
        rows = dict(SpendCounter.objects.values_list("name", "amount"))
        self.assertEqual(rows, {"deepinfra:chat": 1250, "typesafe:letters": 42})

    def test_a_refresh_reads_other_pods_spend_and_counts_nothing_twice(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        SpendCounter.objects.create(day=today, name="typesafe:chat", amount=5000)
        spend.record(spend.TYPESAFE, spend.CHAT, 100)
        spend._ledger.flush_sync_for_tests()
        view = spend._ledger.snapshot()
        self.assertEqual(view.day_total("typesafe:chat", today), 5100)
        self.assertEqual(spend.month_summary()["typesafe:chat"], 0.0051)

    def test_last_months_spend_does_not_count(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        last_month = today.replace(day=1) - datetime.timedelta(days=1)
        SpendCounter.objects.create(day=last_month, name="deepinfra:chat", amount=10**9)
        spend._ledger.flush_sync_for_tests()
        self.assertEqual(spend._ledger.snapshot().month_total("deepinfra:chat"), 0)

    def test_a_pause_is_shared_through_the_ledger(self):
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        spend._ledger.flush_sync_for_tests()
        # Another pod: no local pause, only the ledger row.
        spend._ledger._local_pauses.clear()
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))
        self.assertNotIn("paused:deepinfra:chat", spend.month_summary())

    def test_a_failed_write_is_retried_and_nothing_is_lost(self):
        spend.record(spend.DEEPINFRA, spend.CHAT, 100)
        real_store = spend._Ledger._store
        calls = []

        def failing_once(ledger, name, day, amount):
            calls.append(amount)
            if len(calls) == 1:
                raise RuntimeError("database away")
            return real_store(ledger, name, day, amount)

        from unittest.mock import patch

        with patch.object(spend._Ledger, "_store", failing_once):
            with self.assertRaises(RuntimeError):
                spend._ledger.flush_sync_for_tests()
            # More spend while the database was away.
            spend.record(spend.DEEPINFRA, spend.CHAT, 50)
            spend._ledger.flush_sync_for_tests()
        self.assertEqual(SpendCounter.objects.get(name="deepinfra:chat").amount, 150)
        self.assertEqual(spend._ledger._pending, {})

"""ml/spend.py against the database: increments land in one SpendCounter row
per UTC day and name, and a refresh reads this month back."""

import datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import SpendCounter, SpendReservation


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


class UnpauseLedgerTest(TestCase):
    """unpause against the database: the lift is stored as the day's pause
    count set back to 0, which every pod's next refresh reads, and a pause
    made after it is shared again."""

    FLAG = "paused:deepinfra:*"

    def setUp(self):
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)
        self.today = datetime.datetime.now(datetime.timezone.utc).date()

    def _stored(self):
        row = SpendCounter.objects.filter(day=self.today, name=self.FLAG).first()
        return row.amount if row else 0

    def _paused_elsewhere(self):
        # Another pod's pause, as it stands in the database.
        SpendCounter.objects.create(day=self.today, name=self.FLAG, amount=1)
        spend._ledger.flush_sync_for_tests()

    def _as_another_pod(self):
        """Forget what this process knows and read the ledger back, like a
        pod that never saw the refusal or the lift."""
        spend._ledger.reset_for_tests()
        spend._ledger.flush_sync_for_tests()

    def _another_pod_pauses(self):
        """A new refusal on another pod, stored there with its own ledger."""
        pod = spend._Ledger()
        pod.pause("deepinfra:*")
        pod.flush()

    def test_an_unpause_stores_the_days_pause_count_back_at_zero(self):
        spend.pause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        spend.unpause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        self.assertEqual(self._stored(), 0)

    def test_another_pod_reads_a_lifted_pause_as_lifted(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        self._as_another_pod()
        self.assertTrue(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_the_pod_that_paused_drops_its_pause_once_another_pod_lifted_it(self):
        spend.pause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        # Another pod's unpause, as it lands in the database.
        SpendCounter.objects.filter(day=self.today, name=self.FLAG).update(amount=0)
        spend._ledger.flush_sync_for_tests()
        self.assertTrue(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_refresh_read_before_the_lift_was_stored_does_not_restore_it(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        # The rows a refresh reads still hold the count the lift will clear.
        spend._ledger._refresh()
        self.assertTrue(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_pause_not_yet_stored_survives_a_refresh(self):
        spend.pause(spend.DEEPINFRA)
        spend._ledger._refresh()
        self.assertTrue(spend._ledger.paused_locally("deepinfra:*"))

    def test_pausing_again_after_an_unpause_is_shared_with_other_pods(self):
        spend.pause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        spend.unpause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        spend.pause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        self._as_another_pod()
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_pause_made_while_a_lift_waits_to_be_stored_wins(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        spend.pause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        self._as_another_pod()
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_lift_that_fails_to_store_is_retried(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        real_clear = spend._Ledger._clear
        calls = []

        def failing_once(ledger, name, day, seen):
            calls.append(name)
            if len(calls) == 1:
                raise RuntimeError("database away")
            return real_clear(ledger, name, day, seen)

        with patch.object(spend._Ledger, "_clear", failing_once):
            with self.assertRaises(RuntimeError):
                spend._ledger.flush_sync_for_tests()
            spend._ledger.flush_sync_for_tests()
        self.assertEqual(self._stored(), 0)

    def test_a_lift_with_no_newer_pause_stores_the_count_back_at_zero(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        spend._ledger.flush_sync_for_tests()
        self.assertEqual(self._stored(), 0)

    def test_a_pause_another_pod_stores_while_the_lift_is_queued_survives_it(self):
        """The lift takes back only the count this pod read, not the row: a
        refusal another pod stored after the unpause keeps the provider
        paused everywhere."""
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        self._another_pod_pauses()
        spend._ledger.flush_sync_for_tests()
        self._as_another_pod()
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_refresh_before_the_lift_is_stored_still_reads_a_newer_pause(self):
        self._paused_elsewhere()
        spend.unpause(spend.DEEPINFRA)
        self._another_pod_pauses()
        # The rows hold the lifted count and the newer one; only the first
        # is the lift's to discount.
        spend._ledger._refresh()
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))

    def test_a_pause_being_stored_when_the_unpause_lands_is_lifted_with_it(self):
        """The unpause drops the count from pending, but it is already on its
        way to the database and not yet in the view: once it lands, the lift
        takes it back out too."""
        spend.pause(spend.DEEPINFRA)
        real_store = spend._Ledger._store

        def unpause_mid_store(ledger, name, day, amount):
            real_store(ledger, name, day, amount)
            spend.unpause(spend.DEEPINFRA)

        with patch.object(spend._Ledger, "_store", unpause_mid_store):
            spend._ledger.flush_sync_for_tests()
        spend._ledger.flush_sync_for_tests()
        self.assertEqual(self._stored(), 0)


class AssistantChannelTest(TestCase):
    def test_a_denial_comes_from_the_site_unless_told_otherwise(self):
        from fighthealthinsurance.models import Denial

        denial = Denial.objects.create(hashed_email="h", denial_text="letter")
        self.assertEqual(denial.channel, "site")
        self.assertEqual(spend.channel_of(denial), spend.CHANNEL_SITE)
        denial.channel = "assistant"
        denial.save(update_fields=["channel"])
        denial.refresh_from_db()
        self.assertEqual(spend.channel_of(denial), spend.CHANNEL_ASSISTANT)


TODAY = datetime.date(2026, 9, 10)
TOMORROW = datetime.date(2026, 9, 11)
NAME = spend.counter(spend.FHI, spend.ASSISTANT)


class ReservationTest(TestCase):
    """reserve_generation takes one of the day's generations against the
    shared count in one conditional update; a reservation is given back once,
    to its own day."""

    def setUp(self):
        spend._ledger.reset_for_tests()
        self._today = patch.object(spend, "_today", return_value=TODAY)
        self._today.start()

    def tearDown(self):
        self._today.stop()
        spend._ledger.reset_for_tests()

    def _count(self, day=TODAY):
        row = SpendCounter.objects.filter(day=day, name=NAME).first()
        return row.amount if row else 0

    def test_reservations_count_in_the_shared_row_and_stop_at_the_cap(self):
        with override_settings(FHI_SPEND_ASSISTANT_DAILY_APPEALS=2):
            self.assertIsNotNone(spend.reserve_generation())
            self.assertIsNotNone(spend.reserve_generation())
            self.assertIsNone(spend.reserve_generation())
        self.assertEqual(self._count(), 2)
        self.assertEqual(SpendReservation.objects.count(), 2)

    def test_a_pod_whose_copy_is_stale_cannot_take_a_generation_the_row_says_is_gone(
        self,
    ):
        # This process has seen nothing today; the shared row is at the cap.
        SpendCounter.objects.create(day=TODAY, name=NAME, amount=2)
        with override_settings(FHI_SPEND_ASSISTANT_DAILY_APPEALS=2):
            self.assertIsNone(spend.reserve_generation())
        self.assertEqual(self._count(), 2)

    def test_fifty_a_day_by_default_and_none_means_no_cap(self):
        SpendCounter.objects.create(day=TODAY, name=NAME, amount=50)
        self.assertIsNone(spend.reserve_generation())
        with override_settings(FHI_SPEND_ASSISTANT_DAILY_APPEALS=None):
            self.assertIsNotNone(spend.reserve_generation())
        self.assertEqual(self._count(), 51)

    def test_a_reservation_is_given_back_once(self):
        with override_settings(FHI_SPEND_ASSISTANT_DAILY_APPEALS=1):
            held = spend.reserve_generation()
            self.assertIsNotNone(held)
            self.assertIsNone(spend.reserve_generation())
            self.assertTrue(spend.release_generation(held))
            self.assertFalse(spend.release_generation(held))
            self.assertEqual(self._count(), 0)
            self.assertIsNotNone(spend.reserve_generation())
        self.assertEqual(self._count(), 1)

    def test_a_release_after_midnight_credits_the_day_it_was_taken_from(self):
        held = spend.reserve_generation()
        self.assertEqual(held.day, TODAY)
        with patch.object(spend, "_today", return_value=TOMORROW):
            self.assertTrue(spend.release_generation(held))
        self.assertEqual(self._count(TODAY), 0)
        self.assertEqual(self._count(TOMORROW), 0)

    def test_a_reservation_that_is_not_ours_is_not_released(self):
        stranger = spend.Reservation(id=987654, day=TODAY)
        self.assertFalse(spend.release_generation(stranger))


class ReservationRaceTest(TestCase):
    """The day's row may be made by another pod between this pod's look and
    its own insert; get_or_create settles that, and the generation is then
    taken from that row with the same conditional update."""

    def setUp(self):
        spend._ledger.reset_for_tests()

    def tearDown(self):
        spend._ledger.reset_for_tests()

    def test_a_row_another_pod_made_first_is_taken_from_not_duplicated(self):
        from fighthealthinsurance import models as fhi_models

        def another_pod_got_there_first(**kwargs):
            row = SpendCounter.objects.create(day=TODAY, name=NAME, amount=1)
            return row, False

        with patch.object(spend, "_today", return_value=TODAY), override_settings(
            FHI_SPEND_ASSISTANT_DAILY_APPEALS=5
        ), patch.object(
            fhi_models.SpendCounter.objects,
            "get_or_create",
            another_pod_got_there_first,
        ):
            held = spend.reserve_generation()
        self.assertIsNotNone(held)
        self.assertEqual(SpendCounter.objects.get(day=TODAY, name=NAME).amount, 2)
        self.assertEqual(SpendCounter.objects.filter(name=NAME).count(), 1)
        self.assertEqual(SpendReservation.objects.count(), 1)


@override_settings(FHI_SPEND_BACKGROUND=True)
class IdleLedgerTest(TestCase):
    """A process with no traffic keeps its copy loaded, so the first assistant
    request after a quiet spell isn't refused."""

    def setUp(self):
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)
        self.enterContext(patch.object(spend._ledger, "_ensure_worker"))

    def test_an_idle_process_refreshes_on_its_own_and_still_allows_assistant(self):
        import time

        from django.db import connections

        spend._ledger._refreshed_at = time.monotonic() - spend.STALE_SECONDS - 1
        self.assertFalse(spend.allows(spend.FHI, spend.ASSISTANT))
        spend._ledger._refresh_wanted.clear()
        with patch.object(connections, "close_all"):
            spend._ledger._tick()
        self.assertTrue(spend.allows(spend.FHI, spend.ASSISTANT))

    def test_a_cold_process_waits_for_its_first_read(self):
        import threading
        import time

        def land():
            time.sleep(0.1)
            spend._ledger._refreshed_at = time.monotonic()
            spend._ledger._landed.set()

        thread = threading.Thread(target=land)
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(spend._ledger.wait_until_loaded(2.0))

    def test_an_unreadable_ledger_still_refuses_after_the_wait(self):
        self.assertFalse(spend._ledger.wait_until_loaded(0.05))
        self.assertFalse(spend.allows(spend.FHI, spend.ASSISTANT))

    def test_a_refresh_that_lands_before_the_wait_is_not_missed(self):
        import time

        spend._ledger._refreshed_at = time.monotonic()
        spend._ledger._landed.set()
        started = time.monotonic()
        self.assertTrue(spend._ledger.wait_until_loaded(2.0))
        self.assertLess(time.monotonic() - started, 0.5)

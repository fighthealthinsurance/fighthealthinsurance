"""The side-by-side outside model's day budget (spend.side_by_side_budget_left):
under SIDE_BY_SIDE_DAILY_USD of today's DeepInfra spend for the letter's use,
and never when the provider is paused or the ledger can't be read."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from fighthealthinsurance.ml import spend


class _View:
    def __init__(self, spent_usd, loaded=True):
        self.loaded = loaded
        self._spent = round(spent_usd * spend.MICRO)

    def day_total(self, name, day):
        return self._spent if name == spend.counter(spend.DEEPINFRA, spend.OTHER) else 0


class SideBySideBudgetTest(TestCase):
    def left(self, spent_usd, loaded=True, paused=False, allows=True):
        with patch.object(
            spend._ledger, "snapshot", return_value=_View(spent_usd, loaded)
        ), patch.object(spend, "paused", return_value=paused), patch.object(
            spend, "allows", return_value=allows
        ), patch.object(
            spend, "current_use", return_value=spend.OTHER
        ):
            return spend.side_by_side_budget_left()

    def test_the_proposed_amount_is_two_dollars_a_day(self):
        self.assertEqual(spend.SIDE_BY_SIDE_DAILY_USD, 2.0)

    def test_under_the_day_it_may_write(self):
        self.assertTrue(self.left(1.99))

    def test_at_the_day_it_stops(self):
        self.assertFalse(self.left(2.0))

    def test_paused_or_refused_it_stops(self):
        self.assertFalse(self.left(0, paused=True))
        self.assertFalse(self.left(0, allows=False))

    def test_an_unread_ledger_stops_it(self):
        self.assertFalse(self.left(0, loaded=False))

    @override_settings(FHI_SPEND_SIDE_BY_SIDE_DAILY_USD=5)
    def test_the_setting_moves_it(self):
        self.assertTrue(self.left(4.5))

    @override_settings(FHI_SPEND_SIDE_BY_SIDE_DAILY_USD=0)
    def test_zero_removes_it(self):
        self.assertTrue(self.left(500))

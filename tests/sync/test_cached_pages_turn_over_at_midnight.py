"""A cached page never outlives the day it was rendered on.

Every page carries dated copy (the menu links the open enrollment page only
in season), so StaticIshView keys its cache by the day and stops max-age at
the next midnight in Honolulu, where Denial Watch's dates turn over.
"""

import itertools
import time
from datetime import date, datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.cache import get_max_age

from fighthealthinsurance import views
from fighthealthinsurance.views import STATIC_ISH_PAGE_CACHE_SECONDS, page_day

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "cached-pages-turn-over-at-midnight",
    }
}


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=dt_timezone.utc)


class TestPageDay(TestCase):
    def test_the_day_is_honolulus(self):
        # 09:00 UTC on Jan 16 is still 23:00 on Jan 15 in Honolulu.
        with mock.patch("django.utils.timezone.now", return_value=_utc(2027, 1, 16, 9, 0)):
            self.assertEqual(page_day(), (date(2027, 1, 15), _utc(2027, 1, 16, 10, 0).timestamp()))

    def test_a_new_day_ends_at_the_next_midnight(self):
        with mock.patch("django.utils.timezone.now", return_value=_utc(2027, 1, 16, 10, 0)):
            self.assertEqual(page_day(), (date(2027, 1, 16), _utc(2027, 1, 17, 10, 0).timestamp()))


@override_settings(CACHES=LOCMEM_CACHE)
class TestCachedPagesTurnOver(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        original = views.AboutView.get_context_data
        self.renders = 0

        def counted(view, **kwargs):
            self.renders += 1
            return original(view, **kwargs)

        patcher = mock.patch.object(views.AboutView, "get_context_data", counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, day: date, seconds_left: int = 6 * 60 * 60):
        midnight = time.time() + seconds_left
        with mock.patch("fighthealthinsurance.views.page_day", return_value=(day, midnight)):
            return Client().get(reverse("about"))

    def test_the_same_day_is_served_from_the_cache(self):
        self._get(date(2027, 1, 15))
        self._get(date(2027, 1, 15))
        self.assertEqual(self.renders, 1)

    def test_the_next_day_renders_again(self):
        self._get(date(2027, 1, 15))
        self._get(date(2027, 1, 16))
        self.assertEqual(self.renders, 2)

    def test_max_age_is_the_usual_one_early_in_the_day(self):
        response = self._get(date(2027, 1, 15))
        self.assertEqual(get_max_age(response), STATIC_ISH_PAGE_CACHE_SECONDS)
        self.assertIn("public", response["Cache-Control"])

    def _assert_fresh_for(self, response, seconds: int, now: float):
        age = int(response.get("Age", 0))
        self.assertEqual(get_max_age(response) - age, seconds)
        self.assertIn("public", response["Cache-Control"])
        left = parsedate_to_datetime(response["Expires"]).timestamp() - now
        self.assertTrue(seconds - 2 <= left <= seconds + 1, left)

    def test_a_fresh_render_stops_at_midnight(self):
        response = self._get(date(2027, 1, 15), seconds_left=600)
        self.assertEqual(self.renders, 1)
        self.assertNotIn("Age", response)
        self._assert_fresh_for(response, 600, time.time())

    def test_a_cached_copy_stops_at_midnight(self):
        now = time.time()
        with mock.patch("time.time", return_value=now):
            self._get(date(2027, 1, 15))
        # Twenty minutes later, five before midnight: Age counts the twenty.
        with mock.patch("time.time", return_value=now + 1200):
            response = self._get(date(2027, 1, 15), seconds_left=300)
        self.assertEqual(self.renders, 1)
        self.assertEqual(response["Age"], "1200")
        self.assertEqual(get_max_age(response), 1500)
        self._assert_fresh_for(response, 300, now + 1200)

    def test_a_render_that_crosses_midnight_is_not_fresh(self):
        now = time.time()
        # The request arrives a second before midnight; the headers go out two seconds after.
        clock = itertools.chain([now - 1], itertools.repeat(now + 2))
        with mock.patch("time.time", side_effect=lambda: next(clock)):
            response = self._get(date(2027, 1, 15), seconds_left=1)
        self.assertEqual(get_max_age(response), 0)
        self.assertLessEqual(parsedate_to_datetime(response["Expires"]).timestamp(), now + 2)

    def test_a_cached_copy_keeps_its_own_end_when_that_comes_first(self):
        now = time.time()
        with mock.patch("time.time", return_value=now):
            self._get(date(2027, 1, 15))
        with mock.patch("time.time", return_value=now + 1200):
            response = self._get(date(2027, 1, 15))
        self.assertEqual(get_max_age(response), STATIC_ISH_PAGE_CACHE_SECONDS)
        self.assertEqual(response["Age"], "1200")

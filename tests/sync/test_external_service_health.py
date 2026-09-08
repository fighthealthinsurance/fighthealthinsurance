"""ExternalServiceHealth: the cross-pod "last outcome" record behind the
status page's letter scoring row.

Writes overlap (several drafts score at once, on several pods), so the one
property that matters is that a timestamp only ever moves forward: a slow
write from an older call must not land on top of a newer outcome and fake a
recovery. And a write must never raise into the call it records.
"""

import datetime
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.models import ExternalServiceHealth


def _at(instant):
    return mock.patch("django.utils.timezone.now", return_value=instant)


class ExternalServiceHealthTest(TestCase):
    def _row(self):
        return ExternalServiceHealth.objects.get(service="typesafe")

    def test_the_first_call_creates_the_row(self):
        async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 402")
        row = self._row()
        self.assertEqual(row.last_failure, "HTTP 402")
        self.assertIsNotNone(row.last_failure_at)
        self.assertIsNone(row.last_success_at)

    def test_a_success_and_a_failure_keep_their_own_columns(self):
        async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "timeout")
        async_to_sync(ExternalServiceHealth.anote_success)("typesafe")
        row = self._row()
        self.assertEqual(row.last_failure, "timeout")
        self.assertIsNotNone(row.last_failure_at)
        self.assertIsNotNone(row.last_success_at)

    def test_a_stale_failure_cannot_undo_a_newer_one(self):
        """Failure at T1 stalls; success at T2; failure at T3; then T1 lands.
        The row must still say: failure T3, so no false recovery."""
        t1 = timezone.now() - datetime.timedelta(minutes=3)
        t2 = t1 + datetime.timedelta(minutes=1)
        t3 = t2 + datetime.timedelta(minutes=1)
        with _at(t2):
            async_to_sync(ExternalServiceHealth.anote_success)("typesafe")
        with _at(t3):
            async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 503")
        with _at(t1):
            async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 402")
        row = self._row()
        self.assertEqual(row.last_failure_at, t3)
        self.assertEqual(row.last_failure, "HTTP 503")
        self.assertEqual(row.last_success_at, t2)

    def test_a_stale_success_cannot_undo_a_newer_one(self):
        t1 = timezone.now() - datetime.timedelta(minutes=2)
        t2 = t1 + datetime.timedelta(minutes=1)
        with _at(t2):
            async_to_sync(ExternalServiceHealth.anote_success)("typesafe")
        with _at(t1):
            async_to_sync(ExternalServiceHealth.anote_success)("typesafe")
        self.assertEqual(self._row().last_success_at, t2)

    def test_a_row_created_between_the_two_statements_still_takes_the_newer_outcome(self):
        """Before the row exists: a failure's conditional update finds nothing,
        another pod's OLDER success creates the row, and the failure's
        get-or-create comes back with created=False. The failure must still
        land (review)."""
        t2 = timezone.now() - datetime.timedelta(minutes=2)
        t3 = t2 + datetime.timedelta(minutes=1)

        async def racing_get_or_create(**kwargs):
            row = await ExternalServiceHealth.objects.acreate(
                service=kwargs["service"], last_success_at=t2
            )
            return row, False

        with _at(t3), mock.patch.object(
            ExternalServiceHealth.objects, "aget_or_create", new=racing_get_or_create
        ):
            async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 402")
        row = self._row()
        self.assertEqual(row.last_failure, "HTTP 402")
        self.assertEqual(row.last_failure_at, t3)
        self.assertEqual(row.last_success_at, t2)

    def test_a_stale_write_arriving_during_creation_still_loses(self):
        t1 = timezone.now() - datetime.timedelta(minutes=3)
        t3 = t1 + datetime.timedelta(minutes=2)

        async def racing_get_or_create(**kwargs):
            row = await ExternalServiceHealth.objects.acreate(
                service=kwargs["service"], last_failure_at=t3, last_failure="HTTP 503"
            )
            return row, False

        with _at(t1), mock.patch.object(
            ExternalServiceHealth.objects, "aget_or_create", new=racing_get_or_create
        ):
            async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 402")
        row = self._row()
        self.assertEqual(row.last_failure, "HTTP 503")
        self.assertEqual(row.last_failure_at, t3)

    def test_the_summary_is_cut_to_the_column(self):
        async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "x" * 200)
        self.assertEqual(len(self._row().last_failure), 80)

    def test_a_write_error_never_reaches_the_caller(self):
        with mock.patch.object(
            ExternalServiceHealth.objects, "filter", side_effect=RuntimeError("db down")
        ):
            async_to_sync(ExternalServiceHealth.anote_failure)("typesafe", "HTTP 402")
            async_to_sync(ExternalServiceHealth.anote_success)("typesafe")
        self.assertFalse(ExternalServiceHealth.objects.filter(service="typesafe").exists())

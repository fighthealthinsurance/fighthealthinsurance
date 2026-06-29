"""Tests for the ScheduledEmail queue and ScheduledEmailSender.

Covers enqueueing (timezone resolution + send_after), candidate selection, and
the send / defer-out-of-window / send-failure paths the EmailPollingActor drives.
"""

import datetime
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.models import ScheduledEmail
from fighthealthinsurance.scheduled_emails import (
    MAX_SEND_ATTEMPTS,
    ScheduledEmailSender,
    enqueue_scheduled_email,
)

_MODULE = "fighthealthinsurance.scheduled_emails"


def _make_scheduled(**kwargs) -> ScheduledEmail:
    defaults = {
        "to_email": "pro@example.com",
        "subject": "Hi",
        "template_name": "proconnector_intro",
        "context": {"body": "Hello there", "name": "Pat"},
        "cc": ["professional@fighthealthinsurance.com"],
    }
    defaults.update(kwargs)
    return ScheduledEmail.objects.create(**defaults)


class EnqueueTest(TestCase):
    def test_enqueue_with_business_phone_uses_specific_timezone(self):
        sentinel = timezone.now() + datetime.timedelta(hours=3)
        with patch(f"{_MODULE}.next_business_hours_start", return_value=sentinel):
            se = enqueue_scheduled_email(
                to_email="pro@example.com",
                subject="Intro",
                template_name="proconnector_intro",
                context={"body": "b", "name": "n"},
                cc=["professional@fighthealthinsurance.com"],
                phone="212-555-1234",
                purpose="proconnector_intro",
            )
        se.refresh_from_db()
        self.assertEqual(se.send_timezone, "America/New_York")
        self.assertTrue(se.timezone_is_specific)
        self.assertEqual(se.send_after, sentinel)
        self.assertEqual(se.purpose, "proconnector_intro")
        self.assertEqual(se.cc, ["professional@fighthealthinsurance.com"])
        self.assertFalse(se.sent)

    def test_enqueue_without_phone_uses_conservative_pacific(self):
        se = enqueue_scheduled_email(
            to_email="pro@example.com",
            subject="Intro",
            template_name="proconnector_intro",
            context={},
            phone="",
        )
        self.assertEqual(se.send_timezone, "America/Los_Angeles")
        self.assertFalse(se.timezone_is_specific)


class FindCandidatesTest(TestCase):
    def test_returns_due_unsent_oldest_first_and_excludes_others(self):
        now = timezone.now()
        past1 = _make_scheduled(send_after=now - datetime.timedelta(hours=2))
        past2 = _make_scheduled(send_after=now - datetime.timedelta(hours=1))
        # Excluded: not yet due, already sent, given up.
        _make_scheduled(send_after=now + datetime.timedelta(hours=1))
        _make_scheduled(send_after=now - datetime.timedelta(hours=1), sent=True)
        _make_scheduled(
            send_after=now - datetime.timedelta(hours=1),
            attempts=MAX_SEND_ATTEMPTS,
        )

        candidates = ScheduledEmailSender().find_candidates()
        self.assertEqual([c.pk for c in candidates], [past1.pk, past2.pk])


class DoSendTest(TestCase):
    def setUp(self):
        self.sender = ScheduledEmailSender()
        self.se = _make_scheduled(
            send_after=timezone.now() - datetime.timedelta(minutes=5)
        )

    def test_sends_when_within_window(self):
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email"
        ) as mock_send:
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertTrue(result)
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "pro@example.com")
        self.assertEqual(kwargs["template_name"], "proconnector_intro")
        self.assertEqual(kwargs["cc"], ["professional@fighthealthinsurance.com"])
        self.se.refresh_from_db()
        self.assertTrue(self.se.sent)
        self.assertIsNotNone(self.se.sent_at)
        self.assertEqual(self.se.attempts, 1)

    def test_defers_when_outside_window(self):
        future = timezone.now() + datetime.timedelta(days=1)
        with patch(f"{_MODULE}.is_within_business_hours", return_value=False), patch(
            f"{_MODULE}.next_business_hours_start", return_value=future
        ), patch(f"{_MODULE}.send_fallback_email") as mock_send:
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertFalse(result)
        mock_send.assert_not_called()
        self.se.refresh_from_db()
        self.assertFalse(self.se.sent)
        self.assertEqual(self.se.send_after, future)
        self.assertEqual(self.se.attempts, 0)

    def test_send_failure_backs_off_and_records_error(self):
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email", side_effect=RuntimeError("smtp boom")
        ):
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertFalse(result)
        self.se.refresh_from_db()
        self.assertFalse(self.se.sent)
        self.assertEqual(self.se.attempts, 1)
        self.assertIn("smtp boom", self.se.last_error or "")
        self.assertGreater(self.se.send_after, timezone.now())

    def test_already_sent_is_noop(self):
        self.se.sent = True
        self.se.save()
        with patch(f"{_MODULE}.send_fallback_email") as mock_send:
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertFalse(result)
        mock_send.assert_not_called()

    def test_soft_claim_blocks_concurrent_second_send(self):
        # Simulate another poller having soft-claimed the row first (its
        # send_after pushed into the future): this dosend must not double-send.
        ScheduledEmail.objects.filter(pk=self.se.pk).update(
            send_after=timezone.now() + datetime.timedelta(minutes=30)
        )
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email"
        ) as mock_send:
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertFalse(result)
        mock_send.assert_not_called()
        self.se.refresh_from_db()
        self.assertFalse(self.se.sent)

    def test_send_advances_send_after_as_soft_claim(self):
        # A successful send soft-claims by pushing send_after ~an hour out, so a
        # concurrent poller scanning due rows won't also pick it up.
        before = timezone.now()
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email"
        ):
            self.sender.dosend(scheduled_email=self.se)
        self.se.refresh_from_db()
        self.assertGreater(self.se.send_after, before + datetime.timedelta(minutes=30))

    def test_send_failure_backoff_is_exponential(self):
        # A row that has already failed several times backs off much longer than
        # the initial hour (exponential, not a fixed interval).
        self.se.attempts = 3
        self.se.save()
        before = timezone.now()
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email", side_effect=RuntimeError("boom")
        ):
            result = self.sender.dosend(scheduled_email=self.se)
        self.assertFalse(result)
        self.se.refresh_from_db()
        self.assertEqual(self.se.attempts, 4)
        # attempts=3 -> ~1h * 2**3 = 8h, far beyond the 1h initial backoff.
        self.assertGreater(self.se.send_after, before + datetime.timedelta(hours=4))


class AsendAllTest(TestCase):
    def test_asend_all_sends_due_candidates(self):
        now = timezone.now()
        _make_scheduled(send_after=now - datetime.timedelta(hours=1))
        _make_scheduled(send_after=now - datetime.timedelta(hours=1))
        sender = ScheduledEmailSender()
        with patch(f"{_MODULE}.is_within_business_hours", return_value=True), patch(
            f"{_MODULE}.send_fallback_email"
        ) as mock_send:
            sent = async_to_sync(sender.asend_all)(count=10)
        self.assertEqual(sent, 2)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(ScheduledEmail.objects.filter(sent=True).count(), 2)

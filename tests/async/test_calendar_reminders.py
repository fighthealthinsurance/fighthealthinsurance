"""Tests for calendar reminder functionality."""

import datetime
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

import pytest
from fighthealthinsurance.calendar_emails import CalendarReminderSender
from fighthealthinsurance.calendar_utils import generate_followup_ics
from fighthealthinsurance.models import CalendarReminder, Denial


class CalendarUtilsTest(TestCase):
    """Test calendar .ics file generation."""

    def setUp(self):
        """Create a test denial for calendar generation."""
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance Co",
            procedure="Medical Procedure",
            denial_date=datetime.date.today(),
            denial_text="Test denial",
            hashed_email="test-hashed",
            raw_email="test@example.com",
        )

    def test_generate_followup_ics_creates_file(self):
        """Test that .ics file is generated successfully."""
        ics_content = generate_followup_ics(self.denial, "test@example.com")

        # Should return bytes
        self.assertIsInstance(ics_content, bytes)

        # Should contain iCalendar headers
        ics_str = ics_content.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", ics_str)
        self.assertIn("END:VCALENDAR", ics_str)
        self.assertIn("VERSION:2.0", ics_str)

    def test_generate_followup_ics_creates_three_events(self):
        """Test that .ics file contains exactly 3 events (2, 30, 90 days)."""
        ics_content = generate_followup_ics(self.denial, "test@example.com")
        ics_str = ics_content.decode("utf-8")

        # Count VEVENT occurrences
        vevent_count = ics_str.count("BEGIN:VEVENT")
        self.assertEqual(vevent_count, 3, "Should have exactly 3 events")

    def test_generate_followup_ics_includes_insurance_company(self):
        """Test that insurance company name appears in event summaries."""
        ics_content = generate_followup_ics(self.denial, "test@example.com")
        ics_str = ics_content.decode("utf-8")

        self.assertIn("Test Insurance Co", ics_str)

    def test_generate_followup_ics_includes_action_items(self):
        """Test that event descriptions include action items."""
        ics_content = generate_followup_ics(self.denial, "test@example.com")
        ics_str = ics_content.decode("utf-8")

        # Check for action items in description
        self.assertIn("Call", ics_str)
        self.assertIn("reference number", ics_str.lower())

    def test_generate_followup_ics_includes_alarms(self):
        """Test that events include reminder alarms."""
        ics_content = generate_followup_ics(self.denial, "test@example.com")
        ics_str = ics_content.decode("utf-8")

        # Check for alarm/reminder
        self.assertIn("BEGIN:VALARM", ics_str)
        self.assertIn("ACTION:DISPLAY", ics_str)

    def test_generate_followup_ics_with_no_denial_date(self):
        """Test calendar generation when denial has no denial_date."""
        denial_no_date = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test",
            hashed_email="test2",
            raw_email="test2@example.com",
        )

        ics_content = generate_followup_ics(denial_no_date, "test2@example.com")

        # Should still generate successfully
        self.assertIsInstance(ics_content, bytes)
        ics_str = ics_content.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", ics_str)


class CalendarReminderModelTest(TestCase):
    """Test CalendarReminder model functionality."""

    def setUp(self):
        """Create test denial and reminder."""
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test denial",
            hashed_email="test-hash",
            raw_email="test@example.com",
        )

    def test_create_calendar_reminder(self):
        """Test creating a calendar reminder."""
        reminder = CalendarReminder.objects.create(
            email="test@example.com",
            denial=self.denial,
            reminder_date=datetime.date.today() + datetime.timedelta(days=2),
            reminder_type="2day",
        )

        self.assertEqual(reminder.email, "test@example.com")
        self.assertEqual(reminder.reminder_type, "2day")
        self.assertFalse(reminder.reminder_sent)
        self.assertIsNone(reminder.reminder_sent_date)

    def test_calendar_reminder_types(self):
        """Test all three reminder types can be created."""
        for days, reminder_type in [(2, "2day"), (30, "30day"), (90, "90day")]:
            reminder = CalendarReminder.objects.create(
                email="test@example.com",
                denial=self.denial,
                reminder_date=datetime.date.today() + datetime.timedelta(days=days),
                reminder_type=reminder_type,
            )
            self.assertEqual(reminder.reminder_type, reminder_type)

    def test_calendar_reminder_cascade_delete(self):
        """Test that reminders are deleted when denial is deleted."""
        reminder = CalendarReminder.objects.create(
            email="test@example.com",
            denial=self.denial,
            reminder_date=datetime.date.today() + datetime.timedelta(days=2),
            reminder_type="2day",
        )

        # Delete the denial
        denial_id = self.denial.denial_id
        reminder_id = reminder.id
        self.denial.delete()

        # Reminder should be deleted too
        with pytest.raises(CalendarReminder.DoesNotExist):
            CalendarReminder.objects.get(id=reminder_id)


class CalendarReminderSenderTest(TestCase):
    """Test CalendarReminderSender email sending functionality."""

    def setUp(self):
        """Create test data."""
        self.denial = Denial.objects.create(
            insurance_company="Test Insurance",
            denial_text="Test",
            hashed_email="test-hash",
            raw_email="test@example.com",
        )

        # Create reminders - some overdue, some future
        self.overdue_reminder = CalendarReminder.objects.create(
            email="test@example.com",
            denial=self.denial,
            reminder_date=datetime.date.today() - datetime.timedelta(days=1),
            reminder_type="2day",
            reminder_sent=False,
        )

        self.due_today_reminder = CalendarReminder.objects.create(
            email="test2@example.com",
            denial=self.denial,
            reminder_date=datetime.date.today(),
            reminder_type="30day",
            reminder_sent=False,
        )

        self.future_reminder = CalendarReminder.objects.create(
            email="test3@example.com",
            denial=self.denial,
            reminder_date=datetime.date.today() + datetime.timedelta(days=5),
            reminder_type="90day",
            reminder_sent=False,
        )

    def test_find_candidates_includes_overdue(self):
        """Test that sender finds overdue reminders."""
        sender = CalendarReminderSender()
        candidates = sender.find_candidates()

        # Should find overdue and due today, but not future
        emails = [c.email for c in candidates]
        self.assertIn(self.overdue_reminder.email, emails)
        self.assertIn(self.due_today_reminder.email, emails)
        self.assertNotIn(self.future_reminder.email, emails)

    def test_find_candidates_excludes_already_sent(self):
        """Test that already-sent reminders are excluded."""
        # Mark one as sent
        self.overdue_reminder.reminder_sent = True
        self.overdue_reminder.save()

        sender = CalendarReminderSender()
        candidates = sender.find_candidates()

        emails = [c.email for c in candidates]
        self.assertNotIn(self.overdue_reminder.email, emails)
        self.assertIn(self.due_today_reminder.email, emails)

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_dosend_marks_as_sent(self, mock_send):
        """Test that sending a reminder marks it as sent."""
        mock_send.return_value = None

        sender = CalendarReminderSender()
        result = sender.dosend(calendar_reminder=self.overdue_reminder)

        self.assertTrue(result)

        # Refresh from DB
        self.overdue_reminder.refresh_from_db()
        self.assertTrue(self.overdue_reminder.reminder_sent)
        self.assertIsNotNone(self.overdue_reminder.reminder_sent_date)

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_dosend_by_email(self, mock_send):
        """Test sending by email lookup."""
        mock_send.return_value = None

        sender = CalendarReminderSender()
        result = sender.dosend(email="test@example.com")

        self.assertTrue(result)

    @patch("fighthealthinsurance.utils.send_fallback_email")
    def test_send_all_with_count_limit(self, mock_send):
        """Test sending with a count limit."""
        mock_send.return_value = None

        sender = CalendarReminderSender()
        sent_count = sender.send_all(count=1)

        # Should only send 1 even though 2 are due
        self.assertEqual(sent_count, 1)

    def test_dosend_requires_email_or_reminder(self):
        """Test that dosend raises exception without email or reminder."""
        sender = CalendarReminderSender()

        with pytest.raises(Exception):
            sender.dosend()

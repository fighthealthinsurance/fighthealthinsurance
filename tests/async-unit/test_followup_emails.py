"""Tests for follow-up email functionality."""

import datetime
import pytest
from unittest.mock import patch, MagicMock
from django.core import mail
from django.template.loader import render_to_string
from django.utils import timezone

from fighthealthinsurance.models import Denial, FollowUpSched, FollowUpType
from fighthealthinsurance.followup_emails import FollowUpEmailSender
from fighthealthinsurance.common_view_logic import schedule_follow_ups


@pytest.fixture
def test_denial(db):
    """Create a test denial with raw email set."""
    email = "test@example.com"
    hashed_email = Denial.get_hashed_email(email)
    denial = Denial.objects.create(
        denial_text="Test denial text",
        hashed_email=hashed_email,
        raw_email=email,
        health_history="",
    )
    return denial


@pytest.fixture
def test_followup_sched(test_denial):
    """Create a test follow-up schedule."""
    followup = FollowUpSched.objects.create(
        email=test_denial.raw_email,
        denial_id=test_denial,
        follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
        follow_up_sent=False,
    )
    return followup


@pytest.fixture
def followup_types(db):
    """Get or create the three standard FollowUpType records."""
    fut_7, _ = FollowUpType.objects.get_or_create(
        name="followup_7day",
        defaults={
            "template_name": "followup_7day",
            "subject": "Fight Health Insurance: Confirm Your Appeal Was Received",
            "text": "7-day check-in",
            "duration": datetime.timedelta(days=7),
        },
    )
    fut_30, _ = FollowUpType.objects.get_or_create(
        name="followup_30day",
        defaults={
            "template_name": "followup_30day",
            "subject": "Fight Health Insurance: Have You Heard Back on Your Appeal?",
            "text": "30-day check-in",
            "duration": datetime.timedelta(days=30),
        },
    )
    fut_90, _ = FollowUpType.objects.get_or_create(
        name="followup_90day",
        defaults={
            "template_name": "followup_90day",
            "subject": "Fight Health Insurance: 90-Day Appeal Check-In",
            "text": "90-day check-in",
            "duration": datetime.timedelta(days=90),
        },
    )
    return fut_7, fut_30, fut_90


@pytest.mark.django_db
class TestFollowUpEmailSender:
    """Test the FollowUpEmailSender class."""

    def test_find_candidates_returns_unsent_past_followups(self, test_followup_sched):
        """Test that find_candidates returns follow-ups that are past due and not sent."""
        sender = FollowUpEmailSender()
        candidates = sender.find_candidates()
        assert len(candidates) == 1
        assert candidates[0] == test_followup_sched

    def test_find_candidates_excludes_future_followups(self, test_denial):
        """Test that find_candidates excludes future follow-ups."""
        # Create a follow-up for the future
        FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_date=datetime.date.today() + datetime.timedelta(days=5),
            follow_up_sent=False,
        )
        sender = FollowUpEmailSender()
        candidates = sender.find_candidates()
        assert len(candidates) == 0

    def test_find_candidates_excludes_already_sent(self, test_denial):
        """Test that find_candidates excludes already sent follow-ups."""
        FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=True,
            follow_up_sent_date=timezone.now() - datetime.timedelta(days=1),
        )
        sender = FollowUpEmailSender()
        candidates = sender.find_candidates()
        assert len(candidates) == 0

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_sends_email_and_updates_followup(
        self, mock_send_email, test_followup_sched
    ):
        """Test that dosend sends the email and updates the follow-up schedule."""
        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=test_followup_sched)

        assert result is True
        mock_send_email.assert_called_once()

        # Refresh from database
        test_followup_sched.refresh_from_db()
        assert test_followup_sched.follow_up_sent is True
        assert test_followup_sched.follow_up_sent_date is not None

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_handles_email_failure(self, mock_send_email, test_followup_sched):
        """Test that dosend handles email sending failures gracefully."""
        mock_send_email.side_effect = Exception("Email sending failed")

        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=test_followup_sched)

        assert result is False

        # Refresh from database - should not be marked as sent
        test_followup_sched.refresh_from_db()
        assert test_followup_sched.follow_up_sent is False

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_send_all_sends_to_all_candidates(self, mock_send_email, test_denial):
        """Test that send_all sends to all candidates."""
        # Create multiple follow-ups
        for i in range(3):
            FollowUpSched.objects.create(
                email=f"test{i}@example.com",
                denial_id=test_denial,
                follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
                follow_up_sent=False,
            )

        sender = FollowUpEmailSender()
        count = sender.send_all()

        assert count == 3
        assert mock_send_email.call_count == 3

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_send_all_respects_count_limit(self, mock_send_email, test_denial):
        """Test that send_all respects the count limit."""
        # Create multiple follow-ups
        for i in range(5):
            FollowUpSched.objects.create(
                email=f"test{i}@example.com",
                denial_id=test_denial,
                follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
                follow_up_sent=False,
            )

        sender = FollowUpEmailSender()
        count = sender.send_all(count=2)

        assert count == 2
        assert mock_send_email.call_count == 2

    def test_dosend_by_email(self, test_followup_sched):
        """Test that dosend can find follow-up by email."""
        sender = FollowUpEmailSender()

        with patch("fighthealthinsurance.followup_emails.send_fallback_email"):
            result = sender.dosend(email=test_followup_sched.email)
            assert result is True

    def test_find_candidates_excludes_old_followups(self, test_denial):
        """Test that find_candidates excludes follow-ups older than six months."""
        followup = FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )
        # Backdate the initial field beyond six months
        FollowUpSched.objects.filter(pk=followup.pk).update(
            initial=datetime.date.today() - datetime.timedelta(days=200)
        )
        sender = FollowUpEmailSender()
        candidates = sender.find_candidates()
        assert len(candidates) == 0

    def test_dosend_raises_when_no_params(self):
        """Test that dosend raises when neither email nor follow_up_sched provided."""
        sender = FollowUpEmailSender()

        with pytest.raises(
            Exception, match="One of email and follow_up_sched must be set"
        ):
            sender.dosend()

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_with_both_params_uses_follow_up_sched(
        self, mock_send_email, test_followup_sched
    ):
        """Test that dosend uses follow_up_sched email when both params provided."""
        sender = FollowUpEmailSender()
        result = sender.dosend(
            follow_up_sched=test_followup_sched, email="different@example.com"
        )

        assert result is True
        # Should use the email from follow_up_sched, not the provided one
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args[1]
        assert call_kwargs["to_email"] == test_followup_sched.email

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_uses_followup_type_template(
        self, mock_send_email, test_denial, followup_types
    ):
        """Test that dosend uses template_name and subject from FollowUpType."""
        fut_7, _, _ = followup_types
        sched = FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_type=fut_7,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )

        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=sched)

        assert result is True
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args[1]
        assert call_kwargs["template_name"] == "followup_7day"
        assert call_kwargs["subject"] == fut_7.subject

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_falls_back_for_null_type(
        self, mock_send_email, test_followup_sched
    ):
        """Test that dosend uses generic template when follow_up_type is None."""
        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=test_followup_sched)

        assert result is True
        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args[1]
        assert call_kwargs["template_name"] == "followup"
        assert call_kwargs["subject"] == "Following up from Fight Health Insurance"

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_skips_stale_followup_when_later_already_sent(
        self, mock_send_email, test_denial, followup_types
    ):
        """Test that 7-day email is suppressed if 30-day was already sent."""
        fut_7, fut_30, _ = followup_types

        # Create the 30-day follow-up as already sent
        FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_type=fut_30,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=True,
            follow_up_sent_date=timezone.now() - datetime.timedelta(days=1),
        )

        # Create the 7-day follow-up as unsent
        sched_7 = FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_type=fut_7,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )

        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=sched_7)

        assert result is True
        # No email should have been sent
        mock_send_email.assert_not_called()
        # But it should be marked as sent
        sched_7.refresh_from_db()
        assert sched_7.follow_up_sent is True

    @patch("fighthealthinsurance.followup_emails.send_fallback_email")
    def test_dosend_sends_when_no_later_followup_sent(
        self, mock_send_email, test_denial, followup_types
    ):
        """Test that 7-day email is sent when no later follow-up was sent yet."""
        fut_7, fut_30, _ = followup_types

        # 30-day follow-up exists but is NOT sent yet
        FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_type=fut_30,
            follow_up_date=datetime.date.today() + datetime.timedelta(days=23),
            follow_up_sent=False,
        )

        sched_7 = FollowUpSched.objects.create(
            email=test_denial.raw_email,
            denial_id=test_denial,
            follow_up_type=fut_7,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )

        sender = FollowUpEmailSender()
        result = sender.dosend(follow_up_sched=sched_7)

        assert result is True
        mock_send_email.assert_called_once()


@pytest.mark.django_db
class TestScheduleFollowUps:
    """Test the schedule_follow_ups helper function."""

    def test_creates_three_records_for_new_denial(self, test_denial, followup_types):
        """Test that schedule_follow_ups creates 3 FollowUpSched records."""
        schedule_follow_ups(test_denial.raw_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        assert scheds.count() == 3

        type_names = set(s.follow_up_type.name for s in scheds)
        assert type_names == {"followup_7day", "followup_30day", "followup_90day"}

    def test_correct_follow_up_dates(self, test_denial, followup_types):
        """Test that follow-up dates are correctly calculated."""
        schedule_follow_ups(test_denial.raw_email, test_denial)

        for sched in FollowUpSched.objects.filter(denial_id=test_denial):
            expected_date = test_denial.date + sched.follow_up_type.duration
            assert sched.follow_up_date == expected_date

    def test_prevents_duplicates(self, test_denial, followup_types):
        """Test that calling schedule_follow_ups twice doesn't create duplicates."""
        schedule_follow_ups(test_denial.raw_email, test_denial)
        schedule_follow_ups(test_denial.raw_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        assert scheds.count() == 3

    def test_skips_past_dates_for_old_denials(self, test_denial, followup_types):
        """Test that follow-ups with past dates are skipped for old denials."""
        # Backdate the denial to 45 days ago so 7-day and 30-day are in the past
        old_date = datetime.date.today() - datetime.timedelta(days=45)
        Denial.objects.filter(pk=test_denial.pk).update(date=old_date)
        test_denial.refresh_from_db()

        schedule_follow_ups(test_denial.raw_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        # Only the 90-day follow-up should be created (45 + 90 = 135 days from now)
        assert scheds.count() == 1
        assert scheds[0].follow_up_type.name == "followup_90day"

    def test_skips_all_for_very_old_denial(self, test_denial, followup_types):
        """Test that no follow-ups are created for very old denials."""
        old_date = datetime.date.today() - datetime.timedelta(days=100)
        Denial.objects.filter(pk=test_denial.pk).update(date=old_date)
        test_denial.refresh_from_db()

        schedule_follow_ups(test_denial.raw_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        assert scheds.count() == 0

    def test_from_date_schedules_relative_to_given_date(
        self, test_denial, followup_types
    ):
        """Test that from_date overrides denial.date for scheduling."""
        today = datetime.date.today()
        schedule_follow_ups(test_denial.raw_email, test_denial, from_date=today)

        for sched in FollowUpSched.objects.filter(denial_id=test_denial):
            expected_date = today + sched.follow_up_type.duration
            assert sched.follow_up_date == expected_date

    def test_from_date_reschedules_old_denial_from_today(
        self, test_denial, followup_types
    ):
        """Test that passing from_date=today for an old denial creates all 3."""
        old_date = datetime.date.today() - datetime.timedelta(days=45)
        Denial.objects.filter(pk=test_denial.pk).update(date=old_date)
        test_denial.refresh_from_db()

        # Without from_date, only 90-day would be created
        schedule_follow_ups(test_denial.raw_email, test_denial)
        assert FollowUpSched.objects.filter(denial_id=test_denial).count() == 1

        # With from_date=today, all 3 should be created
        schedule_follow_ups(
            test_denial.raw_email,
            test_denial,
            from_date=datetime.date.today(),
        )
        assert FollowUpSched.objects.filter(denial_id=test_denial).count() == 3

    def test_upsert_updates_email_on_second_call(self, test_denial, followup_types):
        """Test that calling with a different email updates existing records."""
        schedule_follow_ups(test_denial.raw_email, test_denial)
        new_email = "updated@example.com"
        schedule_follow_ups(new_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        assert scheds.count() == 3
        for sched in scheds:
            assert sched.email == new_email

    def test_creates_nothing_when_no_matching_followup_types(self, test_denial):
        """Test that nothing is created when the expected FollowUpType records don't exist."""
        # Remove any matching types (may exist from data migration)
        FollowUpType.objects.filter(
            name__in=["followup_7day", "followup_30day", "followup_90day"]
        ).delete()

        schedule_follow_ups(test_denial.raw_email, test_denial)

        scheds = FollowUpSched.objects.filter(denial_id=test_denial)
        assert scheds.count() == 0


@pytest.mark.django_db
class TestFollowUpEmailTemplates:
    """Test that follow-up email templates render correctly and contain expected content."""

    def test_followup_html_contains_how_to_help_link(self):
        """Test that the HTML follow-up email contains the how-to-help link."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        html_content = render_to_string("emails/followup.html", context)

        assert "fighthealthinsurance.com/how-to-help" in html_content
        assert "Thank you for being part of our community" in html_content
        assert "Share your experience" in html_content
        assert "Follow us on social media" in html_content

    def test_followup_txt_contains_how_to_help_link(self):
        """Test that the plain text follow-up email contains the how-to-help link."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        txt_content = render_to_string("emails/followup.txt", context)

        assert "fighthealthinsurance.com/how-to-help" in txt_content
        assert "Thank you for being part of our community" in txt_content

    def test_fax_followup_html_contains_how_to_help_link(self):
        """Test that the HTML fax follow-up email contains the how-to-help link."""
        context = {
            "name": "Test User",
            "success": True,
            "fax_redo_link": "https://example.com/fax/redo/123",
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "fighthealthinsurance.com/how-to-help" in html_content
        assert "Thank you for being part of our community" in html_content

    def test_fax_followup_txt_contains_how_to_help_link(self):
        """Test that the plain text fax follow-up email contains the how-to-help link."""
        context = {
            "name": "Test User",
            "success": True,
            "fax_redo_link": "https://example.com/fax/redo/123",
        }
        txt_content = render_to_string("emails/fax_followup.txt", context)

        assert "fighthealthinsurance.com/how-to-help" in txt_content
        assert "Thank you for being part of our community" in txt_content

    def test_followup_html_shows_correct_message_for_selected_appeal(self):
        """Test that the HTML email shows the correct message when appeal was selected."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        html_content = render_to_string("emails/followup.html", context)

        assert "checking in to see if you were able to submit" in html_content

    def test_followup_html_shows_correct_message_for_no_appeal(self):
        """Test that the HTML email shows the correct message when no appeal was selected."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": False,
        }
        html_content = render_to_string("emails/followup.html", context)

        assert "didn't generate an appeal that worked" in html_content

    def test_fax_followup_success_message(self):
        """Test that the fax follow-up shows success message when fax succeeded."""
        context = {
            "name": "Test User",
            "success": True,
            "fax_redo_link": "https://example.com/fax/redo/123",
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "fax was sent successfully" in html_content

    def test_fax_followup_failure_message(self):
        """Test that the fax follow-up shows failure message when fax failed."""
        context = {
            "name": "Test User",
            "success": False,
            "fax_redo_link": "https://example.com/fax/redo/123",
        }
        html_content = render_to_string("emails/fax_followup.html", context)

        assert "problem sending the fax" in html_content

    # --- New template tests ---

    def test_followup_7day_html_content(self):
        """Test 7-day HTML template has receipt confirmation messaging."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        html = render_to_string("emails/followup_7day.html", context)

        assert "sent your appeal" in html
        assert "confirm" in html.lower()
        assert "fax" in html.lower()
        assert "mail" in html.lower()
        assert "fighthealthinsurance.com/how-to-help" in html

    def test_followup_7day_txt_content(self):
        """Test 7-day TXT template has receipt confirmation messaging."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        txt = render_to_string("emails/followup_7day.txt", context)

        assert "sent your appeal" in txt
        assert "confirm" in txt.lower()
        assert "fighthealthinsurance.com/how-to-help" in txt

    def test_followup_7day_no_appeal_message(self):
        """Test 7-day template shows different message when no appeal selected."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": False,
        }
        html = render_to_string("emails/followup_7day.html", context)

        assert "may not have generated an appeal that worked" in html

    def test_followup_30day_html_content(self):
        """Test 30-day HTML template asks about hearing back."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        html = render_to_string("emails/followup_30day.html", context)

        assert "heard back" in html.lower()
        assert "external review" in html.lower()
        assert "fighthealthinsurance.com/how-to-help" in html

    def test_followup_30day_txt_content(self):
        """Test 30-day TXT template asks about hearing back."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        txt = render_to_string("emails/followup_30day.txt", context)

        assert "heard back" in txt.lower()
        assert "fighthealthinsurance.com/how-to-help" in txt

    def test_followup_90day_html_content(self):
        """Test 90-day HTML template is a final check-in."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        html = render_to_string("emails/followup_90day.html", context)

        assert "90 days" in html
        assert "outcome" in html.lower()
        assert "fighthealthinsurance.com/how-to-help" in html

    def test_followup_90day_txt_content(self):
        """Test 90-day TXT template is a final check-in."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": True,
        }
        txt = render_to_string("emails/followup_90day.txt", context)

        assert "90 days" in txt
        assert "outcome" in txt.lower()
        assert "fighthealthinsurance.com/how-to-help" in txt

    def test_followup_90day_no_appeal_message(self):
        """Test 90-day template shows help message when no appeal selected."""
        context = {
            "followup_link": "https://example.com/followup/123",
            "selected_appeal": False,
        }
        html = render_to_string("emails/followup_90day.html", context)

        assert "here for you" in html

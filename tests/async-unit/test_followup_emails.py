"""Tests for follow-up email functionality."""

import datetime
import pytest
from unittest.mock import patch, MagicMock
from django.core import mail
from django.template.loader import render_to_string
from django.utils import timezone

from fighthealthinsurance.models import Denial, FollowUpSched
from fighthealthinsurance.followup_emails import FollowUpEmailSender


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

"""Tests for the clear_expired_emails management command."""

import datetime
import pytest
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.utils import timezone

from fighthealthinsurance.models import Denial, FollowUpSched


@pytest.fixture
def email_test_setup(db):
    """Set up test data for email clearing tests."""
    # Create a denial with raw email that has an old follow-up sent
    email1 = "old_followup@example.com"
    hashed_email1 = Denial.get_hashed_email(email1)
    denial1 = Denial.objects.create(
        denial_text="Test denial with old followup",
        hashed_email=hashed_email1,
        raw_email=email1,
        health_history="",
    )
    
    # Create old follow-up that was sent 40 days ago
    old_sent_date = timezone.now() - datetime.timedelta(days=40)
    FollowUpSched.objects.create(
        email=email1,
        denial_id=denial1,
        follow_up_date=old_sent_date.date() - datetime.timedelta(days=1),
        follow_up_sent=True,
        follow_up_sent_date=old_sent_date,
    )
    
    # Create a denial with recent follow-up (should NOT be cleared)
    email2 = "recent_followup@example.com"
    hashed_email2 = Denial.get_hashed_email(email2)
    denial2 = Denial.objects.create(
        denial_text="Test denial with recent followup",
        hashed_email=hashed_email2,
        raw_email=email2,
        health_history="",
    )
    
    # Create recent follow-up that was sent 10 days ago
    recent_sent_date = timezone.now() - datetime.timedelta(days=10)
    FollowUpSched.objects.create(
        email=email2,
        denial_id=denial2,
        follow_up_date=recent_sent_date.date() - datetime.timedelta(days=1),
        follow_up_sent=True,
        follow_up_sent_date=recent_sent_date,
    )
    
    # Create a denial with pending follow-up (should NOT be cleared)
    email3 = "pending_followup@example.com"
    hashed_email3 = Denial.get_hashed_email(email3)
    denial3 = Denial.objects.create(
        denial_text="Test denial with pending followup",
        hashed_email=hashed_email3,
        raw_email=email3,
        health_history="",
    )
    
    # Create pending follow-up
    FollowUpSched.objects.create(
        email=email3,
        denial_id=denial3,
        follow_up_date=datetime.date.today() + datetime.timedelta(days=5),
        follow_up_sent=False,
    )
    
    return {
        'denial_old': denial1,
        'denial_recent': denial2,
        'denial_pending': denial3,
    }


@pytest.mark.django_db
class TestClearExpiredEmailsCommand:
    """Test the clear_expired_emails management command."""

    def test_clears_old_followup_emails(self, email_test_setup):
        """Test that command clears emails for old follow-ups."""
        out = StringIO()
        call_command('clear_expired_emails', stdout=out)
        
        # Refresh from database
        email_test_setup['denial_old'].refresh_from_db()
        email_test_setup['denial_recent'].refresh_from_db()
        email_test_setup['denial_pending'].refresh_from_db()
        
        # Old follow-up email should be cleared
        assert email_test_setup['denial_old'].raw_email is None
        
        # Recent and pending should NOT be cleared
        assert email_test_setup['denial_recent'].raw_email == "recent_followup@example.com"
        assert email_test_setup['denial_pending'].raw_email == "pending_followup@example.com"

    def test_dry_run_does_not_clear_emails(self, email_test_setup):
        """Test that dry-run mode does not clear emails."""
        out = StringIO()
        call_command('clear_expired_emails', '--dry-run', stdout=out)
        
        # Refresh from database
        email_test_setup['denial_old'].refresh_from_db()
        
        # Email should NOT be cleared in dry-run mode
        assert email_test_setup['denial_old'].raw_email == "old_followup@example.com"
        assert "Dry run mode" in out.getvalue()

    def test_custom_days_parameter(self, email_test_setup):
        """Test that custom days parameter works."""
        # With 50 days, the 40-day-old follow-up should NOT be cleared
        out = StringIO()
        call_command('clear_expired_emails', '--days=50', stdout=out)
        
        email_test_setup['denial_old'].refresh_from_db()
        
        # Should not be cleared because it's less than 50 days old
        assert email_test_setup['denial_old'].raw_email == "old_followup@example.com"
        assert "No emails to clear" in out.getvalue()

    def test_no_emails_to_clear(self, db):
        """Test message when no emails need clearing."""
        out = StringIO()
        call_command('clear_expired_emails', stdout=out)
        
        assert "No emails to clear" in out.getvalue()

    def test_clears_followup_sched_emails(self, email_test_setup):
        """Test that command also clears emails from FollowUpSched entries."""
        call_command('clear_expired_emails')
        
        # Check that the FollowUpSched email was also cleared
        followup_sched = FollowUpSched.objects.filter(
            denial_id=email_test_setup['denial_old']
        ).first()
        
        assert followup_sched.email == ''


@pytest.mark.django_db
class TestSendFollowupEmailsCommand:
    """Test the send_followup_emails management command."""

    def test_no_pending_emails(self, db):
        """Test message when no pending emails."""
        out = StringIO()
        call_command('send_followup_emails', stdout=out)
        
        assert "No pending follow-up emails" in out.getvalue()

    @patch('fighthealthinsurance.followup_emails.send_fallback_email')
    def test_sends_pending_emails(self, mock_send_email, db):
        """Test that command sends pending follow-up emails."""
        # Create a denial with follow-up
        email = "test@example.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email=hashed_email,
            raw_email=email,
            health_history="",
        )
        FollowUpSched.objects.create(
            email=email,
            denial_id=denial,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )
        
        out = StringIO()
        call_command('send_followup_emails', stdout=out)
        
        assert "Successfully sent 1 follow-up emails" in out.getvalue()
        mock_send_email.assert_called_once()

    @patch('fighthealthinsurance.followup_emails.send_fallback_email')
    def test_dry_run_does_not_send(self, mock_send_email, db):
        """Test that dry-run mode does not send emails."""
        # Create a denial with follow-up
        email = "test@example.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email=hashed_email,
            raw_email=email,
            health_history="",
        )
        FollowUpSched.objects.create(
            email=email,
            denial_id=denial,
            follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
            follow_up_sent=False,
        )
        
        out = StringIO()
        call_command('send_followup_emails', '--dry-run', stdout=out)
        
        assert "Dry run mode" in out.getvalue()
        mock_send_email.assert_not_called()

    @patch('fighthealthinsurance.followup_emails.send_fallback_email')
    def test_respects_count_limit(self, mock_send_email, db):
        """Test that count parameter limits emails sent."""
        # Create multiple follow-ups
        for i in range(5):
            email = f"test{i}@example.com"
            hashed_email = Denial.get_hashed_email(email)
            denial = Denial.objects.create(
                denial_text=f"Test denial {i}",
                hashed_email=hashed_email,
                raw_email=email,
                health_history="",
            )
            FollowUpSched.objects.create(
                email=email,
                denial_id=denial,
                follow_up_date=datetime.date.today() - datetime.timedelta(days=1),
                follow_up_sent=False,
            )
        
        out = StringIO()
        call_command('send_followup_emails', '--count=2', stdout=out)
        
        assert "Successfully sent 2 follow-up emails" in out.getvalue()
        assert mock_send_email.call_count == 2

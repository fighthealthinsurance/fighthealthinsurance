"""
Tests for the weekly followup digest email functionality.
Tests email generation, sending, and management command.
"""

import io
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from fhi_users.models import PatientUser
from fighthealthinsurance.followup_digest import FollowupDigestSender
from fighthealthinsurance.models import Appeal, Denial, InsuranceCallLog

User = get_user_model()


class FollowupDigestSenderTests(TestCase):
    """Tests for the FollowupDigestSender class."""

    def setUp(self) -> None:
        # Create user with active patient record
        self.user = User.objects.create_user(
            username="digest_test",
            password="TestPass123!",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
            is_active=True,
        )
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_find_candidates_with_active_appeals(self) -> None:
        """Test that users with active appeals are found as candidates."""
        # Create an active appeal
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test denial",
            patient_user=self.patient,
            procedure="MRI",
            diagnosis="Back pain",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test appeal",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        candidates = sender._find_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].user.email, self.user.email)

    def test_find_candidates_with_upcoming_followups(self) -> None:
        """Test that users with upcoming follow-ups are found."""
        tomorrow = date.today() + timedelta(days=1)

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test denial",
            patient_user=self.patient,
            procedure="Surgery",
            diagnosis="Condition",
            next_follow_up_date=tomorrow,
        )

        sender = FollowupDigestSender()
        candidates = sender._find_candidates()

        self.assertEqual(len(candidates), 1)

    def test_find_candidates_with_overdue_decisions(self) -> None:
        """Test that users with overdue decisions are found."""
        yesterday = date.today() - timedelta(days=1)

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test denial",
            patient_user=self.patient,
            procedure="Procedure",
            diagnosis="Diagnosis",
            decision_expected_date=yesterday,
        )

        sender = FollowupDigestSender()
        candidates = sender._find_candidates()

        self.assertEqual(len(candidates), 1)

    def test_find_candidates_excludes_fake_emails(self) -> None:
        """Test that fake email accounts are excluded."""
        fake_user = User.objects.create_user(
            username="fake_user",
            email="test-fake@fighthealthinsurance.com",
            password="pass",
        )
        PatientUser.objects.create(user=fake_user, active=True)

        # Create appeal for fake user
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(fake_user.email),
            denial_text="Test",
            patient_user=fake_user.patient_user,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(fake_user.email),
            patient_user=fake_user.patient_user,
            appeal_text="Test",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        candidates = sender._find_candidates()

        # Should not include fake email
        self.assertEqual(len([c for c in candidates if "fake@" in c.user.email]), 0)

    def test_find_candidates_excludes_inactive_patients(self) -> None:
        """Test that inactive patient accounts are excluded."""
        self.patient.active = False
        self.patient.save()

        # Create appeal
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        candidates = sender._find_candidates()

        self.assertEqual(len(candidates), 0)

    def test_generate_digest_context_with_all_sections(self) -> None:
        """Test digest context generation with all possible sections."""
        today = date.today()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)

        # Create denial with upcoming follow-up
        denial1 = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Denial 1",
            patient_user=self.patient,
            procedure="MRI",
            diagnosis="Back pain",
            next_follow_up_date=tomorrow,
        )

        # Create denial with overdue decision
        denial2 = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Denial 2",
            patient_user=self.patient,
            procedure="Surgery",
            diagnosis="Condition",
            decision_expected_date=yesterday,
        )

        # Create active appeal
        Appeal.objects.create(
            for_denial=denial1,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Appeal text",
            patient_visible=True,
        )

        # Create recent call log
        InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now() - timedelta(days=3),
            call_type="claim_status",
            reason_for_call="Check status",
            outcome="pending",
        )

        sender = FollowupDigestSender()
        context = sender._generate_digest_context(self.patient)

        self.assertIsNotNone(context)
        self.assertEqual(context["patient_name"], "Test")
        self.assertEqual(len(context["overdue_decisions"]), 1)
        self.assertEqual(len(context["upcoming_followups"]), 1)
        self.assertEqual(len(context["active_appeals"]), 1)
        self.assertEqual(len(context["recent_calls"]), 1)
        self.assertIn("dashboard_url", context)

    def test_generate_digest_context_no_activity(self) -> None:
        """Test that context is None when patient has no activity."""
        sender = FollowupDigestSender()
        context = sender._generate_digest_context(self.patient)

        self.assertIsNone(context)

    def test_send_digest_email_success(self) -> None:
        """Test sending a digest email."""
        # Create some activity
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test Procedure",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test appeal",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        context = sender._generate_digest_context(self.patient)
        result = sender._send_digest_email(self.patient, context)

        self.assertTrue(result)
        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertEqual(email.to, [self.user.email])
        self.assertIn("Weekly Follow-up Digest", email.subject)
        self.assertIn("Test", email.body)  # Should contain patient name

    def test_send_digest_email_html_and_text(self) -> None:
        """Test that email contains both HTML and plain text versions."""
        # Create activity
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        context = sender._generate_digest_context(self.patient)
        sender._send_digest_email(self.patient, context)

        email = mail.outbox[0]
        self.assertEqual(len(email.alternatives), 1)
        html_content, content_type = email.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("<html>", html_content)

    def test_send_all_dry_run(self) -> None:
        """Test send_all in dry-run mode doesn't send emails."""
        # Create activity
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        sent, skipped = sender.send_all(dry_run=True)

        self.assertEqual(sent, 0)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(mail.outbox), 0)  # No emails sent

    def test_send_all_real_run(self) -> None:
        """Test send_all actually sends emails."""
        # Create activity
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test",
            patient_visible=True,
        )

        sender = FollowupDigestSender()
        sent, skipped = sender.send_all(dry_run=False)

        self.assertEqual(sent, 1)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_send_all_with_limit(self) -> None:
        """Test send_all respects the count limit."""
        # Create two patients with activity
        user2 = User.objects.create_user(
            username="patient2",
            email="patient2@example.com",
            password="pass",
            first_name="Patient",
            last_name="Two",
        )
        patient2 = PatientUser.objects.create(user=user2, active=True)

        for patient in [self.patient, patient2]:
            denial = Denial.objects.create(
                hashed_email=Denial.get_hashed_email(patient.user.email),
                denial_text="Test",
                patient_user=patient,
                procedure="Test",
            )
            Appeal.objects.create(
                for_denial=denial,
                hashed_email=Denial.get_hashed_email(patient.user.email),
                patient_user=patient,
                appeal_text="Test",
                patient_visible=True,
            )

        sender = FollowupDigestSender()
        sent, skipped = sender.send_all(dry_run=False, max_count=1)

        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)


class SendFollowupDigestCommandTests(TestCase):
    """Tests for the send_followup_digest management command."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="cmd_test",
            email="cmdtest@example.com",
            password="pass",
            first_name="Command",
            last_name="Test",
        )
        self.patient = PatientUser.objects.create(user=self.user, active=True)

        # Create activity
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(self.user.email),
            denial_text="Test",
            patient_user=self.patient,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(self.user.email),
            patient_user=self.patient,
            appeal_text="Test",
            patient_visible=True,
        )

    def test_command_dry_run(self) -> None:
        """Test command with --dry-run flag."""
        out = io.StringIO()
        call_command("send_followup_digest", "--dry-run", stdout=out)

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertEqual(len(mail.outbox), 0)

    def test_command_real_run(self) -> None:
        """Test command without --dry-run sends emails."""
        out = io.StringIO()
        call_command("send_followup_digest", stdout=out)

        output = out.getvalue()
        self.assertIn("Sent", output)
        self.assertEqual(len(mail.outbox), 1)

    def test_command_with_count_limit(self) -> None:
        """Test command with --count flag."""
        # Create second patient
        user2 = User.objects.create_user(
            username="user2",
            email="user2@example.com",
            password="pass",
            first_name="User",
            last_name="Two",
        )
        patient2 = PatientUser.objects.create(user=user2, active=True)

        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(user2.email),
            denial_text="Test",
            patient_user=patient2,
            procedure="Test",
        )
        Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(user2.email),
            patient_user=patient2,
            appeal_text="Test",
            patient_visible=True,
        )

        out = io.StringIO()
        call_command("send_followup_digest", "--count", "1", stdout=out)

        output = out.getvalue()
        self.assertIn("1", output)
        self.assertEqual(len(mail.outbox), 1)

    def test_command_no_candidates(self) -> None:
        """Test command when no candidates exist."""
        # Delete all appeals
        Appeal.objects.all().delete()
        Denial.objects.all().delete()

        out = io.StringIO()
        call_command("send_followup_digest", stdout=out)

        output = out.getvalue()
        self.assertIn("0", output)
        self.assertEqual(len(mail.outbox), 0)

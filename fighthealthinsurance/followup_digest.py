"""Email sender for weekly follow-up digests to patient users.

This module handles sending weekly digest emails to logged-in patients with:
1. Upcoming follow-ups (next 7 days)
2. Overdue decision alerts (decision_expected_date has passed)
3. Recent call log activity
4. Tips for staying organized
"""

from datetime import date, timedelta
from typing import List, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from fhi_users.models import PatientUser
from fighthealthinsurance.models import Appeal, InsuranceCallLog
from loguru import logger


class FollowupDigestSender:
    """Sends weekly follow-up digest emails to patient users."""

    def _find_candidates(self):
        """Find patient users who should receive digest emails.

        Returns patient users who have:
        - At least one active appeal OR
        - At least one upcoming follow-up in next 7 days OR
        - At least one overdue decision

        Returns:
            QuerySet of PatientUser objects
        """
        today = date.today()
        next_week = today + timedelta(days=7)

        # Get all active patient users with valid emails
        candidates = (
            PatientUser.objects.filter(active=True, user__email__isnull=False)
            .exclude(user__email="")
            .exclude(user__email__endswith="-fake@fighthealthinsurance.com")
        )

        # Filter to users who have something to report
        candidates_with_activity = []
        for patient in candidates:
            user = patient.user

            # Check for active appeals
            active_appeals = Appeal.filter_to_allowed_appeals(user).filter(
                sent=True, success__isnull=True
            )

            # Check for upcoming follow-ups
            upcoming_followups = InsuranceCallLog.filter_to_allowed_call_logs(
                user
            ).filter(follow_up_date__gte=today, follow_up_date__lte=next_week)

            # Check for overdue decisions
            overdue_appeals = Appeal.filter_to_allowed_appeals(user).filter(
                decision_expected_date__lt=today,
                appeal_status__in=["sent", "awaiting_decision"],
                success__isnull=True,
            )

            if (
                active_appeals.exists()
                or upcoming_followups.exists()
                or overdue_appeals.exists()
            ):
                candidates_with_activity.append(patient.id)

        return PatientUser.objects.filter(id__in=candidates_with_activity)

    def _generate_digest_context(self, patient_user: PatientUser) -> dict:
        """Generate template context for a patient's weekly digest.

        Args:
            patient_user: PatientUser to generate digest for

        Returns:
            dict: Template context with upcoming_followups, overdue_decisions, etc.
        """
        user = patient_user.user
        today = date.today()
        next_week = today + timedelta(days=7)

        # Upcoming follow-ups (next 7 days)
        upcoming_followups = list(
            InsuranceCallLog.filter_to_allowed_call_logs(user)
            .filter(follow_up_date__gte=today, follow_up_date__lte=next_week)
            .order_by("follow_up_date")[:10]
        )

        # Overdue decisions
        overdue_decisions = list(
            Appeal.filter_to_allowed_appeals(user)
            .filter(
                decision_expected_date__lt=today,
                appeal_status__in=["sent", "awaiting_decision"],
                success__isnull=True,
            )
            .select_related("for_denial")
            .order_by("decision_expected_date")[:10]
        )

        # Active appeals
        active_appeals = list(
            Appeal.filter_to_allowed_appeals(user)
            .filter(sent=True, success__isnull=True)
            .select_related("for_denial")
            .order_by("-creation_date")[:5]
        )

        # Recent call logs (last 7 days)
        last_week = today - timedelta(days=7)
        recent_calls = list(
            InsuranceCallLog.filter_to_allowed_call_logs(user)
            .filter(call_date__gte=last_week)
            .order_by("-call_date")[:5]
        )

        # Dashboard URL
        dashboard_url = "https://www.fighthealthinsurance.com" + reverse(
            "patient-dashboard"
        )

        return {
            "user": user,
            "patient_user": patient_user,
            "upcoming_followups": upcoming_followups,
            "overdue_decisions": overdue_decisions,
            "active_appeals": active_appeals,
            "recent_calls": recent_calls,
            "dashboard_url": dashboard_url,
            "today": today,
            "upcoming_count": len(upcoming_followups),
            "overdue_count": len(overdue_decisions),
            "active_count": len(active_appeals),
        }

    def _send_digest_email(self, patient_user: PatientUser) -> bool:
        """Send weekly digest email to a patient user.

        Args:
            patient_user: PatientUser to send digest to

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        user = patient_user.user
        to_email = user.email

        if not to_email or to_email.endswith("-fake@fighthealthinsurance.com"):
            return False

        try:
            context = self._generate_digest_context(patient_user)

            # Skip if nothing to report (should be caught by _find_candidates but double-check)
            if (
                context["upcoming_count"] == 0
                and context["overdue_count"] == 0
                and context["active_count"] == 0
            ):
                logger.info(f"Skipping digest for {to_email} - no activity to report")
                return False

            # Render plain text content
            text_content = render_to_string(
                "emails/followup_digest.txt",
                context=context,
            )

            # Render HTML content
            html_content = render_to_string(
                "emails/followup_digest.html",
                context=context,
            )

            # Create subject line
            subject = "Your Weekly Appeal Follow-up Digest"
            if context["overdue_count"] > 0:
                subject = f"⏰ {context['overdue_count']} Decision(s) Expected - Weekly Digest"
            elif context["upcoming_count"] > 0:
                subject = (
                    f"📅 {context['upcoming_count']} Follow-up(s) This Week - Digest"
                )

            # Create multipart email
            msg = EmailMultiAlternatives(
                subject,
                text_content,
                settings.DEFAULT_FROM_EMAIL,
                to=[to_email],
            )

            logger.debug(
                f"Sending follow-up digest to {to_email} with subject {subject}"
            )

            # Attach HTML content
            msg.attach_alternative(html_content, "text/html")

            # Send email
            msg.send()

            logger.info(f"Successfully sent follow-up digest to {to_email}")
            return True

        except Exception as e:
            logger.error(f"Failed to send follow-up digest to {to_email}: {e}")
            return False

    def send_all(self, count: Optional[int] = None) -> int:
        """Send digest emails to all candidate patient users.

        Args:
            count: Optional maximum number of emails to send

        Returns:
            int: Number of emails successfully sent
        """
        candidates = self._find_candidates()
        total = candidates.count()

        if total == 0:
            logger.info("No patient users need follow-up digests")
            return 0

        logger.info(f"Found {total} patient users for follow-up digests")

        sent_count = 0
        for patient_user in candidates[:count] if count else candidates:
            if self._send_digest_email(patient_user):
                sent_count += 1

        logger.info(f"Sent {sent_count} out of {total} follow-up digest emails")
        return sent_count

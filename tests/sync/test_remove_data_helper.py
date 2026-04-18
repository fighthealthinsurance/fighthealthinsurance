"""Tests for fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.

`RemoveDataHelper.remove_data_for_email` is the GDPR / privacy-compliance
deletion entry point invoked by the data-removal views. It is mocked in every
caller-side test but never directly exercised. These tests create real rows
across every model touched by the helper and verify that matching rows are
deleted while rows for unrelated emails are untouched.
"""

from datetime import date, timedelta

from django.test import TestCase

from fighthealthinsurance.helpers import RemoveDataHelper
from fighthealthinsurance.models import (
    Appeal,
    ChatLeads,
    DemoRequests,
    Denial,
    FaxesToSend,
    FollowUp,
    FollowUpSched,
    MailingListSubscriber,
    OngoingChat,
)


class TestRemoveDataHelper(TestCase):
    """End-to-end coverage of RemoveDataHelper.remove_data_for_email."""

    target_email = "test@example.com"
    other_email = "other@example.com"

    def setUp(self):
        self.target_hash = Denial.get_hashed_email(self.target_email)
        self.other_hash = Denial.get_hashed_email(self.other_email)

        # --- Target rows (should be deleted) ---------------------------
        self.target_denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=self.target_hash,
        )
        self.target_appeal = Appeal.objects.create(
            hashed_email=self.target_hash,
            for_denial=self.target_denial,
            appeal_text="target appeal",
        )
        FollowUp.objects.create(
            hashed_email=self.target_hash,
            denial_id=self.target_denial,
        )
        FollowUpSched.objects.create(
            email=self.target_email,
            denial_id=self.target_denial,
            follow_up_date=date.today() + timedelta(days=7),
        )
        # Two fax rows exercising both deletion filters in the helper:
        # one matched by hashed_email, one matched by email.
        FaxesToSend.objects.create(
            hashed_email=self.target_hash,
            email="unused@example.com",
            appeal_text="fax by hash",
            paid=False,
        )
        FaxesToSend.objects.create(
            hashed_email=self.other_hash,
            email=self.target_email,
            appeal_text="fax by email",
            paid=False,
        )
        OngoingChat.objects.create(hashed_email=self.target_hash)
        ChatLeads.objects.create(
            name="Target Lead",
            email=self.target_email,
            phone="555-0001",
            company="ACME",
            consent_to_contact=True,
            agreed_to_terms=True,
        )
        MailingListSubscriber.objects.create(email=self.target_email)
        DemoRequests.objects.create(email=self.target_email)

        # --- Control rows (should survive) -----------------------------
        self.other_denial = Denial.objects.create(
            denial_id=2,
            semi_sekret="sekret2",
            hashed_email=self.other_hash,
        )
        Appeal.objects.create(
            hashed_email=self.other_hash,
            for_denial=self.other_denial,
            appeal_text="other appeal",
        )
        MailingListSubscriber.objects.create(email=self.other_email)
        ChatLeads.objects.create(
            name="Other Lead",
            email=self.other_email,
            phone="555-0002",
            company="OtherCo",
            consent_to_contact=True,
            agreed_to_terms=True,
        )
        OngoingChat.objects.create(hashed_email=self.other_hash)

    def _target_row_counts(self) -> dict:
        """Count the rows that should have been deleted for target_email."""
        return {
            "denial": Denial.objects.filter(hashed_email=self.target_hash).count(),
            "appeal": Appeal.objects.filter(hashed_email=self.target_hash).count(),
            "followup": FollowUp.objects.filter(hashed_email=self.target_hash).count(),
            "followup_sched": FollowUpSched.objects.filter(
                email__iexact=self.target_email
            ).count(),
            "faxes_by_hash": FaxesToSend.objects.filter(
                hashed_email=self.target_hash
            ).count(),
            "faxes_by_email": FaxesToSend.objects.filter(
                email__iexact=self.target_email
            ).count(),
            "ongoing_chat": OngoingChat.objects.filter(
                hashed_email=self.target_hash
            ).count(),
            "chat_leads": ChatLeads.objects.filter(
                email__iexact=self.target_email
            ).count(),
            "mailing_list": MailingListSubscriber.objects.filter(
                email__iexact=self.target_email
            ).count(),
            "demo_requests": DemoRequests.objects.filter(
                email__iexact=self.target_email
            ).count(),
        }

    def test_remove_data_deletes_all_target_rows(self):
        pre = self._target_row_counts()
        self.assertTrue(
            all(v >= 1 for v in pre.values()),
            f"Precondition failed: some target rows were not created: {pre}",
        )

        RemoveDataHelper.remove_data_for_email(self.target_email)

        post = self._target_row_counts()
        self.assertEqual(
            post,
            {k: 0 for k in pre},
            f"Not all rows were deleted: {post}",
        )

    def test_remove_data_leaves_unrelated_rows_intact(self):
        pre_other_denials = Denial.objects.filter(hashed_email=self.other_hash).count()
        pre_other_mailing = MailingListSubscriber.objects.filter(
            email__iexact=self.other_email
        ).count()
        pre_other_chats = OngoingChat.objects.filter(
            hashed_email=self.other_hash
        ).count()
        pre_other_leads = ChatLeads.objects.filter(
            email__iexact=self.other_email
        ).count()

        RemoveDataHelper.remove_data_for_email(self.target_email)

        self.assertEqual(
            Denial.objects.filter(hashed_email=self.other_hash).count(),
            pre_other_denials,
        )
        self.assertEqual(
            MailingListSubscriber.objects.filter(
                email__iexact=self.other_email
            ).count(),
            pre_other_mailing,
        )
        self.assertEqual(
            OngoingChat.objects.filter(hashed_email=self.other_hash).count(),
            pre_other_chats,
        )
        self.assertEqual(
            ChatLeads.objects.filter(email__iexact=self.other_email).count(),
            pre_other_leads,
        )

    def test_remove_data_normalizes_whitespace_and_case(self):
        """The helper strips whitespace and lowercases before matching, so
        ' TEST@Example.com ' removes rows keyed to 'test@example.com'."""
        RemoveDataHelper.remove_data_for_email("  TEST@Example.com  ")

        post = self._target_row_counts()
        self.assertEqual(post, {k: 0 for k in post})

    def test_remove_data_deletes_faxes_via_both_filters(self):
        """FaxesToSend rows are deleted whether matched by hashed_email or
        by plaintext email."""
        # Sanity-check: we created two fax rows — one matched only by hash,
        # the other only by email.
        self.assertEqual(
            FaxesToSend.objects.filter(hashed_email=self.target_hash).count(),
            1,
        )
        self.assertEqual(
            FaxesToSend.objects.filter(email__iexact=self.target_email).count(),
            1,
        )
        # And the hash-matched row does NOT have the target email, and the
        # email-matched row does NOT have the target hash.
        self.assertFalse(
            FaxesToSend.objects.filter(
                hashed_email=self.target_hash, email__iexact=self.target_email
            ).exists()
        )

        RemoveDataHelper.remove_data_for_email(self.target_email)

        self.assertEqual(
            FaxesToSend.objects.filter(hashed_email=self.target_hash).count(), 0
        )
        self.assertEqual(
            FaxesToSend.objects.filter(email__iexact=self.target_email).count(), 0
        )

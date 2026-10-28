"""Tests for fighthealthinsurance.helpers.data_helpers.RemoveDataHelper.

`RemoveDataHelper.remove_data_for_email` is the data protection deletion
entry point invoked by the data-removal views. It is mocked in every
caller-side test but never directly exercised. These tests create real rows
across every model touched by the helper and verify that matching rows are
deleted while rows for unrelated emails are untouched.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from fighthealthinsurance.helpers import RemoveDataHelper
from fighthealthinsurance.models import (
    Appeal,
    ChatLeads,
    ConsentRecord,
    DemoRequests,
    Denial,
    FaxesToSend,
    FollowUp,
    FollowUpSched,
    InterestedProfessional,
    LostStripeSession,
    MailingListSubscriber,
    OngoingChat,
    PriorAuthRequest,
    ScheduledEmail,
)
from fhi_users.models import ProfessionalUser

User = get_user_model()


class TestRemoveDataHelper(TestCase):
    """End-to-end coverage of RemoveDataHelper.remove_data_for_email."""

    target_email = "test@example.com"
    other_email = "other@example.com"

    @classmethod
    def setUpTestData(cls):
        cls.target_hash = Denial.get_hashed_email(cls.target_email)
        cls.other_hash = Denial.get_hashed_email(cls.other_email)

        # --- Target rows (should be deleted) ---------------------------
        cls.target_denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=cls.target_hash,
        )
        cls.target_appeal = Appeal.objects.create(
            hashed_email=cls.target_hash,
            for_denial=cls.target_denial,
            appeal_text="target appeal",
        )
        ConsentRecord.objects.create(
            denial=cls.target_denial,
            terms_version="2026-09-08",
            privacy_version="2026-09-12",
            boxes=[{"name": "tos", "label": "x", "ticked": True}],
        )
        FollowUp.objects.create(
            hashed_email=cls.target_hash,
            denial_id=cls.target_denial,
        )
        FollowUpSched.objects.create(
            email=cls.target_email,
            denial_id=cls.target_denial,
            follow_up_date=date.today() + timedelta(days=7),
        )
        # Two fax rows exercising both deletion filters in the helper:
        # one matched by hashed_email, one matched by email.
        FaxesToSend.objects.create(
            hashed_email=cls.target_hash,
            email="unused@example.com",
            appeal_text="fax by hash",
            paid=False,
        )
        FaxesToSend.objects.create(
            hashed_email=cls.other_hash,
            email=cls.target_email,
            appeal_text="fax by email",
            paid=False,
        )
        cls.target_chat = OngoingChat.objects.create(hashed_email=cls.target_hash)
        # A prior auth the chat made on its own: it outlives the chat
        # (SET_NULL) unless the helper deletes it first.
        PriorAuthRequest.objects.create(
            chat=cls.target_chat,
            patient_name="Rebecca Lee Crumpler",
            diagnosis="back pain",
            treatment="MRI",
        )
        # Stored mixed-case, as a sign-up form may have saved it.
        InterestedProfessional.objects.create(
            name="Rebecca Lee Crumpler",
            email="Test@Example.com",
            phone_number="555-0003",
        )
        ScheduledEmail.objects.create(
            to_email="Test@Example.com",
            subject="Introduction",
            template_name="proconnector_intro",
        )
        LostStripeSession.objects.create(
            email="Test@Example.com",
            payment_type="fax",
            ip_address="203.0.113.7",
            metadata={"fax_request_uuid": "target-fax"},
        )
        ChatLeads.objects.create(
            name="Target Lead",
            email=cls.target_email,
            phone="555-0001",
            company="ACME",
            consent_to_contact=True,
            agreed_to_terms=True,
        )
        MailingListSubscriber.objects.create(email=cls.target_email)
        DemoRequests.objects.create(email=cls.target_email)

        # --- Control rows (should survive) -----------------------------
        cls.other_denial = Denial.objects.create(
            denial_id=2,
            semi_sekret="sekret2",
            hashed_email=cls.other_hash,
        )
        Appeal.objects.create(
            hashed_email=cls.other_hash,
            for_denial=cls.other_denial,
            appeal_text="other appeal",
        )
        MailingListSubscriber.objects.create(email=cls.other_email)
        ChatLeads.objects.create(
            name="Other Lead",
            email=cls.other_email,
            phone="555-0002",
            company="OtherCo",
            consent_to_contact=True,
            agreed_to_terms=True,
        )
        OngoingChat.objects.create(hashed_email=cls.other_hash)
        InterestedProfessional.objects.create(
            name="Elizabeth Blackwell", email=cls.other_email
        )
        LostStripeSession.objects.create(
            email=cls.other_email, payment_type="fax", ip_address="203.0.113.8"
        )
        ScheduledEmail.objects.create(
            to_email=cls.other_email,
            subject="Introduction",
            template_name="proconnector_intro",
        )

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
            "interested_professional": InterestedProfessional.objects.filter(
                email__iexact=self.target_email
            ).count(),
            "scheduled_email": ScheduledEmail.objects.filter(
                to_email__iexact=self.target_email
            ).count(),
            "chat_prior_auth": PriorAuthRequest.objects.filter(
                patient_name="Rebecca Lee Crumpler"
            ).count(),
            "lost_stripe_session": LostStripeSession.objects.filter(
                email__iexact=self.target_email
            ).count(),
        }

    def test_remove_data_deletes_the_consent_record_with_the_denial(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertFalse(ConsentRecord.objects.filter(denial_id=1).exists())

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

    def test_remove_data_deletes_a_professional_signup_stored_in_mixed_case(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertFalse(
            InterestedProfessional.objects.filter(
                email__iexact=self.target_email
            ).exists()
        )

    def test_remove_data_deletes_queued_emails_not_yet_sent(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertFalse(
            ScheduledEmail.objects.filter(
                to_email__iexact=self.target_email
            ).exists()
        )

    def test_remove_data_leaves_other_signups_and_queued_emails(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertEqual(
            (
                InterestedProfessional.objects.filter(email=self.other_email).count(),
                ScheduledEmail.objects.filter(to_email=self.other_email).count(),
            ),
            (1, 1),
        )

    def test_remove_data_deletes_the_prior_auth_their_chat_made(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertFalse(
            PriorAuthRequest.objects.filter(
                patient_name="Rebecca Lee Crumpler"
            ).exists()
        )

    def test_remove_data_keeps_a_professionals_prior_auth_from_their_chat(self):
        user = User.objects.create_user(
            username="virginia_apgar", email="apgar@example.com", password="x"
        )
        professional = ProfessionalUser.objects.create(user=user, active=True)
        kept = PriorAuthRequest.objects.create(
            chat=self.target_chat,
            creator_professional_user=professional,
            diagnosis="asthma",
            treatment="inhaler",
        )
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertTrue(PriorAuthRequest.objects.filter(pk=kept.pk).exists())

    def test_remove_data_deletes_their_expired_checkout_record(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertFalse(
            LostStripeSession.objects.filter(
                email__iexact=self.target_email
            ).exists()
        )

    def test_remove_data_keeps_other_peoples_expired_checkout_records(self):
        RemoveDataHelper.remove_data_for_email(self.target_email)
        self.assertTrue(
            LostStripeSession.objects.filter(email=self.other_email).exists()
        )

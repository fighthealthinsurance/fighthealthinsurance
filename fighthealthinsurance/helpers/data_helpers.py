"""
Data management helpers for Fight Health Insurance.

Provides utilities for data removal and privacy compliance.
"""

from django.db import transaction
from django.utils import timezone
from loguru import logger

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
    PolicyDocument,
)


class RemoveDataHelper:
    """Helper class for removing user data for privacy compliance."""

    @classmethod
    def _record_removal(cls, email: str, hashed_email: str) -> None:
        """Add what is about to be removed to the running totals.

        Runs inside the deletion's transaction (see remove_data_for_email)
        so the counts and the deletion commit or roll back together: a
        deletion that fails halfway and is retried is counted once. The
        totals row is locked for the rest of that transaction, which
        serializes two overlapping requests for the same person. Best
        effort by design: everything sits in its own savepoint, so a
        bookkeeping failure (the table missing mid-rollout, a database
        error) rolls back only the bookkeeping and never blocks or poisons
        the deletion, which is an obligation (review).
        """
        try:
            with transaction.atomic():
                from django.db.models import F, Q, Value
                from django.db.models.functions import Coalesce

                from fighthealthinsurance.models import (
                    DataRemovalTotals,
                    FaxesToSend,
                    ProposedAppeal,
                )

                DataRemovalTotals.objects.get_or_create(
                    pk=DataRemovalTotals.SINGLETON_ID
                )
                DataRemovalTotals.objects.select_for_update().get(
                    pk=DataRemovalTotals.SINGLETON_ID
                )
                # The person's denials and EVERY draft on them (speculative
                # included), locked for the rest of the transaction: the
                # generator promotes a speculative draft to a real one with
                # a plain UPDATE, and a promotion landing between this count
                # and the cascade delete would lose the person from the
                # lifetime total for good; FOR UPDATE makes that promotion
                # wait until the rows are gone (no-op on sqlite) (review).
                denial_ids = list(
                    Denial.objects.select_for_update(of=("self",))
                    .filter(hashed_email=hashed_email)
                    .values_list("denial_id", flat=True)
                )
                draft_is_speculative = list(
                    ProposedAppeal.objects.select_for_update(of=("self",))
                    .filter(for_denial_id__in=denial_ids)
                    .values_list("speculative", flat=True)
                )
                real_drafts = sum(1 for spec in draft_is_speculative if not spec)
                # Everything the deletes below take: rows keyed by this
                # person's hash or email, AND rows that go by cascade
                # through their denial -- a professional's fax staged for a
                # patient's denial carries the professional's email (review).
                # Locked, all of them, not just the delivered ones: the fax
                # worker commits fax_success=True on its own connection, and
                # a delivery landing between this count and the delete
                # would be gone from the lifetime total for good. FOR UPDATE
                # makes that finalize wait for this transaction (no-op on
                # sqlite). ``of=("self",)``: the OR reaches the denial through
                # a nullable FK, an outer join, and Postgres refuses to lock
                # the nullable side of an outer join; lock only the fax rows
                # (review).
                candidates = list(
                    FaxesToSend.objects.select_for_update(of=("self",))
                    .filter(
                        Q(hashed_email=hashed_email)
                        | Q(email__iexact=email)
                        | Q(denial_id__hashed_email=hashed_email)
                    )
                    .values_list("fax_success", flat=True)
                )
                delivered_count = sum(1 for ok in candidates if ok)
                DataRemovalTotals.objects.filter(
                    pk=DataRemovalTotals.SINGLETON_ID
                ).update(
                    requests=F("requests") + 1,
                    denials=F("denials") + len(denial_ids),
                    drafts=F("drafts") + real_drafts,
                    faxes_delivered=F("faxes_delivered") + delivered_count,
                    people_with_draft=F("people_with_draft")
                    + (1 if real_drafts else 0),
                    since=Coalesce(F("since"), Value(timezone.now())),
                )
        except Exception:
            logger.opt(exception=True).warning(
                "Could not record data-removal totals; deleting anyway"
            )

    @classmethod
    def remove_data_for_email(cls, email: str) -> None:
        """
        Remove all data associated with an email address.

        Used for GDPR/privacy compliance data deletion requests.

        Args:
            email: Email address to remove data for
        """
        email = email.strip().lower()
        hashed_email: str = Denial.get_hashed_email(email)
        with transaction.atomic():
            cls._record_removal(email, hashed_email)
            cls._delete_rows(email, hashed_email)

    @classmethod
    def _delete_rows(cls, email: str, hashed_email: str) -> None:
        # Core denial/appeal data
        Denial.objects.filter(hashed_email=hashed_email).delete()
        Appeal.objects.filter(hashed_email=hashed_email).delete()
        # Follow-up related — use __iexact for plaintext email fields so
        # mixed-case stored addresses are reliably matched
        FollowUpSched.objects.filter(email__iexact=email).delete()
        FollowUp.objects.filter(hashed_email=hashed_email).delete()
        FaxesToSend.objects.filter(hashed_email=hashed_email).delete()
        FaxesToSend.objects.filter(email__iexact=email).delete()
        # Chat data - use find_chats_by_email to catch all related chats
        # (covers hashed_email, user__email, and professional_user__user__email)
        OngoingChat.find_chats_by_email(email).delete()
        ChatLeads.objects.filter(email__iexact=email).delete()
        # Policy documents (encrypted files cleaned up via post_delete signal)
        PolicyDocument.objects.filter(hashed_email=hashed_email).delete()
        # Mailing list and demo requests
        MailingListSubscriber.objects.filter(email__iexact=email).delete()
        DemoRequests.objects.filter(email__iexact=email).delete()

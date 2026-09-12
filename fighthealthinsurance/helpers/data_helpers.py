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
    def _record_removal(cls, hashed_email: str, denials_removed: int) -> None:
        """Count this request and the denials it took, in the running totals.

        Runs inside the deletion's transaction (see remove_data_for_email)
        so the count and the deletion commit or roll back together, and the
        totals row is locked so overlapping requests serialize. The number
        recorded is what the delete actually removed, not a count taken
        before it: a denial created in between would otherwise be deleted
        and never counted (review). Best effort
        by design: its own savepoint, so a bookkeeping failure rolls back
        only itself and never blocks a deletion, which is an obligation.
        The lifetime numbers themselves are NOT touched here: they are
        counters that only go up (lifetime_counters.py), which is what makes
        them deletion-proof.
        """
        try:
            with transaction.atomic():
                from django.db.models import F, Value
                from django.db.models.functions import Coalesce

                from fighthealthinsurance.models import DataRemovalTotals

                pk = DataRemovalTotals.SINGLETON_ID
                DataRemovalTotals.objects.get_or_create(pk=pk)
                DataRemovalTotals.objects.select_for_update().get(pk=pk)
                DataRemovalTotals.objects.filter(pk=pk).update(
                    requests=F("requests") + 1,
                    denials=F("denials") + denials_removed,
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
        from fighthealthinsurance.lifetime_counters import person_lock

        email = email.strip().lower()
        hashed_email: str = Denial.get_hashed_email(email)
        with transaction.atomic():
            # Take this person's lock before touching any of their rows,
            # the order every writer of person_counted uses
            # (lifetime_counters.person_lock). Deleting a denial locks rows
            # one at a time as the cascade walks them, including the
            # SET_NULL back-references; a first-draft count locking the
            # same person's rows in the other order would deadlock, and
            # PostgreSQL would abort one of them -- possibly this deletion
            # (review).
            person_lock(hashed_email)
            denials_removed = cls._delete_rows(email, hashed_email)
            cls._record_removal(hashed_email, denials_removed)

    @classmethod
    def _delete_rows(cls, email: str, hashed_email: str) -> int:
        """Delete everything for this person; return how many denials went."""
        # Core denial/appeal data (the person_counted flag goes with the
        # denials; the lifetime counter it fed stays, which is the point).
        _total, per_model = Denial.objects.filter(hashed_email=hashed_email).delete()
        denials_removed = per_model.get(Denial._meta.label, 0)
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
        return denials_removed

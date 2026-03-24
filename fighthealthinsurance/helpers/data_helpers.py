"""
Data management helpers for Fight Health Insurance.

Provides utilities for data removal and privacy compliance.
"""

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


class RemoveDataHelper:
    """Helper class for removing user data for privacy compliance."""

    @classmethod
    def remove_data_for_email(cls, email: str) -> None:
        """
        Remove all data associated with an email address.

        Used for GDPR/privacy compliance data deletion requests.

        Args:
            email: Email address to remove data for
        """
        hashed_email: str = Denial.get_hashed_email(email)
        # Core denial/appeal data (hashed lookups are case-agnostic since
        # get_hashed_email lowercases before hashing)
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
        # Mailing list and demo requests
        MailingListSubscriber.objects.filter(email__iexact=email).delete()
        DemoRequests.objects.filter(email__iexact=email).delete()

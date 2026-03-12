"""Utility for mailing list subscription operations with typed exception handling."""

from django.db import DatabaseError, IntegrityError
from loguru import logger

from fighthealthinsurance.models import MailingListSubscriber


def subscribe_to_mailing_list(
    email: str,
    source: str,
    name: str = "",
    phone: str = "",
    referral_source: str = "",
    referral_source_details: str = "",
) -> None:
    """Subscribe an email to the mailing list, handling duplicates gracefully.

    Catches IntegrityError (race-condition duplicates) and falls back to update.
    Logs and re-raises DatabaseError for operational issues.
    All other exceptions propagate naturally.
    """
    defaults: dict[str, str] = {
        "comments": source,
    }
    if name:
        defaults["name"] = name
    if phone:
        defaults["phone"] = phone
    if referral_source:
        defaults["referral_source"] = referral_source
    if referral_source_details:
        defaults["referral_source_details"] = referral_source_details

    try:
        MailingListSubscriber.objects.get_or_create(
            email=email,
            defaults=defaults,
        )
    except IntegrityError:
        # Race condition: another request created the record between check and insert.
        logger.info(
            f"Duplicate subscription detected, updating instead | email={email} source={source} action=update"
        )
        MailingListSubscriber.objects.filter(email=email).update(**defaults)
    except DatabaseError:
        logger.opt(exception=True).error(
            f"Database error during subscription | email={email} source={source} action=create"
        )
        raise

"""Staff actions on a new professional signup, taken from the team notification.

A new signup no longer emails the submitter on its own. The team notification
(``notify_interested_professional``) carries three buttons, and nothing goes
to the professional until a staff member presses one:

* send the thank-you email (:func:`send_thankyou`),
* send the Cofactor AI introduction (``ProConnectorQuickIntroView``),
* block the address (:func:`block_address`), after which nothing more is sent
  to it and later submissions from it are logged and dropped.

Each action replies to the notification's email thread
(:func:`post_to_signup_thread`) so the other admins on the inbox can see it
was handled and don't act on it twice.
"""

from typing import Optional

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.followup_emails import ThankyouEmailSender
from fighthealthinsurance.models import InterestedProfessional, ScheduledEmail
from fighthealthinsurance.pro_signups import intro_was_sent
from fighthealthinsurance.utils import (
    notify_professional_signup,
    signup_thread_message_id,
)

THANKYOU = "thankyou"
BLOCK = "block"
ACTIONS = (THANKYOU, BLOCK)


def staff_display_name(user) -> str:
    """Who to name in a thread note: full name, else username."""
    full_name = (user.get_full_name() or "").strip()
    return full_name or user.get_username()


def _thread_subject(pro: InterestedProfessional) -> str:
    """The notification's subject, so a reply lands in the same thread.

    Leads from before the subject was stored fall back to a generic one; their
    reply still carries the signup number, it just may not thread.
    """
    subject = (pro.notification_subject or "").strip() or f"Pro signup #{pro.id}"
    return f"Re: {subject}"


def post_to_signup_thread(pro: InterestedProfessional, note: str) -> None:
    """Reply to the lead's team notification thread with ``note``.

    Best-effort: the action it reports on has already happened, so a mail or
    build failure is logged and never raised.
    """
    try:
        admin_path = reverse(
            "admin:fighthealthinsurance_interestedprofessional_change",
            args=[pro.id],
        )
        admin_url = f"https://{settings.FIGHT_HEALTH_INSURANCE_DOMAIN}{admin_path}"
        root = signup_thread_message_id(pro)
        body = f"{note}\n\nSignup #{pro.id}: {admin_url}\n"
    except Exception:
        logger.opt(exception=True).error(
            f"Error building signup thread note (interested_pro_id={pro.id})"
        )
        return
    notify_professional_signup(
        _thread_subject(pro),
        body,
        headers={"In-Reply-To": root, "References": root},
    )


def action_block_reason(pro: InterestedProfessional, action: str) -> Optional[str]:
    """Why ``action`` can't be taken on ``pro`` right now, or ``None``."""
    if pro.blocked:
        when = (
            f" on {timezone.localtime(pro.blocked_at):%Y-%m-%d}"
            if pro.blocked_at
            else ""
        )
        return f"This address was blocked{when}."
    if action == BLOCK:
        return None
    if pro.thankyou_email_sent:
        return "The thank-you email was already sent."
    if pro.unsubscribed:
        return (
            "This professional has unsubscribed from Fight Health Insurance "
            "emails, so the thank-you cannot be sent."
        )
    if intro_was_sent(pro.email):
        return (
            "The Cofactor AI introduction already went to this address, and it "
            "thanks them too."
        )
    return None


def send_thankyou(pro: InterestedProfessional) -> bool:
    """Send the thank-you email to ``pro``; True when it went out.

    Claims the record first with a conditional update, so a double press or a
    second admin pressing at the same moment can't send it twice; the claim
    is released when the send fails, so it can be retried.
    """
    claimed = InterestedProfessional.objects.filter(
        pk=pro.pk, thankyou_email_sent=False, blocked=False, unsubscribed=False
    ).update(thankyou_email_sent=True)
    if not claimed:
        return False
    if ThankyouEmailSender().dosend(interested_pro=pro):
        return True
    InterestedProfessional.objects.filter(pk=pro.pk).update(thankyou_email_sent=False)
    return False


def block_address(pro: InterestedProfessional) -> int:
    """Block every lead with ``pro``'s email (any case); returns how many.

    Also drops any email still queued to the address, such as an introduction
    waiting for business hours. ``0`` means it was already blocked.
    """
    email = (pro.email or "").strip()
    with transaction.atomic():
        blocked = InterestedProfessional.objects.filter(
            email__iexact=email, blocked=False
        ).update(blocked=True, blocked_at=timezone.now())
        if blocked:
            ScheduledEmail.objects.filter(to_email__iexact=email, sent=False).delete()
    return blocked

"""The emailed link back to letters drafted for an AI assistant.

When the person agrees on the assistant terms page we email them, once, a
link to finish on our site instead. It is built like the intake resume link
(intake_resume.py), with two differences: the token rides after ``#`` in
the link, so it never reaches a server log, and it lives 30 days, as long
as the drafts are worth coming back to.

- 32 random bytes; only a digest is stored, on the case's
  AssistantContinueLink row, which goes with the denial.
- Opening the case means typing the email address the case was started
  with. Five wrong addresses revoke the link.
- It opens the saved drafts on the site's appeals page, in the browser that
  typed the address (assistant_terms_views.ContinueView).
"""

import datetime
import hashlib
import hmac
import secrets
from typing import Any, Optional, Tuple

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from loguru import logger

LINK_TTL = datetime.timedelta(days=30)
MAX_WRONG_EMAILS = 5
TOKEN_BYTES = 32
_DIGEST_LABEL = b"fhi-assistant-continue-v1:"
_MAX_TOKEN_LENGTH = 128

OPENED = "opened"
WRONG_EMAIL = "wrong_email"
DEAD = "dead"

SUBJECT = "Your appeal letters on Fight Health Insurance"
BODY = (
    "You agreed to Fight Health Insurance's terms so your AI assistant "
    "could bring appeal letters back to your chat. If you'd rather finish "
    "on our site, this link opens the letters we drafted. To keep your case "
    "private, it asks for this email address, and it works for {days} days:"
    "\n\n{url}\n\nNothing has been sent to your insurer. If you didn't ask "
    "for this, you can ignore this email."
)


def link_days() -> int:
    return LINK_TTL.days


def digest(token: str) -> str:
    return hashlib.sha256(_DIGEST_LABEL + token.encode("utf-8")).hexdigest()


def plausible_token(token: Any) -> bool:
    return isinstance(token, str) and 0 < len(token) <= _MAX_TOKEN_LENGTH


def mint(denial: Any) -> str:
    """Mint the case's link and return its token, which only the email
    carries. Replaces any earlier link for the case."""
    from fighthealthinsurance.models import AssistantContinueLink

    token = secrets.token_urlsafe(TOKEN_BYTES)
    AssistantContinueLink.objects.update_or_create(
        denial=denial,
        defaults={
            "token_digest": digest(token),
            "expires_at": timezone.now() + LINK_TTL,
            "wrong_email_attempts": 0,
        },
    )
    return token


def url_for(token: str) -> str:
    base = getattr(
        settings, "FHI_PUBLIC_BASE_URL", "https://www.fighthealthinsurance.com"
    )
    return f"{base.rstrip('/')}{reverse('assistant_continue')}#{token}"


def send(email: str, token: str) -> bool:
    """Email the link. Best effort: the letters are still drafted without it."""
    try:
        send_mail(
            SUBJECT,
            BODY.format(url=url_for(token), days=link_days()),
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            [email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.warning(f"assistant continue email failed: {type(e).__name__}")
        return False


def _live(token_digest: str) -> Any:
    from fighthealthinsurance.models import AssistantContinueLink

    return AssistantContinueLink.objects.filter(
        token_digest=token_digest,
        expires_at__gt=timezone.now(),
        wrong_email_attempts__lt=MAX_WRONG_EMAILS,
    )


def live_link(token: Any) -> Optional[Any]:
    if not plausible_token(token):
        return None
    return _live(digest(token)).select_related("denial").first()


def open_case(token: Any, email: str) -> Tuple[str, Optional[Any]]:
    """Check the typed address against the case the link names: (OPENED,
    denial), (WRONG_EMAIL, None) or (DEAD, None). Concurrent tries are each
    counted against the row as it stands, as in intake_resume.open_case."""
    from fighthealthinsurance.models import AssistantContinueLink, Denial

    link = live_link(token)
    if link is None:
        return DEAD, None
    token_digest = digest(token)
    typed = Denial.get_hashed_email(email or "")
    right = hmac.compare_digest(typed, link.denial.hashed_email or "")
    with transaction.atomic():
        still_live = (
            _live(token_digest)
            .filter(pk=link.pk)
            .select_for_update()
            .values_list("pk", flat=True)
            .first()
        )
        if still_live is None:
            return DEAD, None
        if right:
            return OPENED, link.denial
        counted = (
            _live(token_digest)
            .filter(pk=link.pk)
            .update(wrong_email_attempts=F("wrong_email_attempts") + 1)
        )
        if not counted:
            return DEAD, None
        revoked = AssistantContinueLink.objects.filter(
            pk=link.pk,
            token_digest=token_digest,
            wrong_email_attempts__gte=MAX_WRONG_EMAILS,
        ).update(token_digest=None)
    if revoked:
        logger.info(
            f"assistant continue: link for denial {link.denial_id} revoked after "
            f"{MAX_WRONG_EMAILS} wrong email addresses"
        )
        return DEAD, None
    return WRONG_EMAIL, None


def sweep_expired(now: Optional[datetime.datetime] = None) -> int:
    """Delete links past their expiry or revoked. Returns how many went."""
    from django.db.models import Q

    from fighthealthinsurance.models import AssistantContinueLink

    deleted, _ = AssistantContinueLink.objects.filter(
        Q(expires_at__lte=now or timezone.now()) | Q(token_digest__isnull=True)
    ).delete()
    return deleted

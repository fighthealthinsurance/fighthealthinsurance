"""The way back into an unfinished appeal from the "you left before finishing" email.

The abandonment nudge (``intake_journey_core.send_abandonment_nudge``) links
to ``/continue/<token>``. A link in an email is a credential for whoever holds
the email, and mail is forwarded, previewed, scanned and logged, so the link
is built to be worth as little as possible on its own:

- Single purpose. The token opens one unfinished case at the step it had
  reached, through the resume pages and nothing else. It is not a back-link
  reference and nothing else accepts it.
- It says nothing. The token is 32 random bytes (``secrets.token_urlsafe``).
  The URL holds no email address, hashed email, denial id, uuid or case
  secret, and nothing can be decoded out of it.
- Stored hashed. The server keeps only the SHA-256 digest of the token, on
  the case's ``IntakeResumePoint`` row. The token itself exists only in the
  email, so the database cannot be read back into working links.
- The email address is the second half. Opening the case means typing the
  email address the case was started with. The denial is keyed by the hash
  of that address, so the check is a hash comparison and costs the person
  one line of typing. A leaked or logged link is useless without it. Five
  wrong addresses revoke the link.
- Expiring. A link works for ``RESUME_LINK_TTL``, 48 hours from minting.
  The nudge goes out 24 hours into the journey and the journey closes at 3
  days, so 48 hours is exactly the time the case can still be resumed from
  the email. The journey's closure deletes the link as well, whichever
  comes first.
- Dead once the case moves on. Reaching a later step (``note_step``)
  revokes the link, a recorded form completion refuses it, and the person
  deleting their data deletes the row with the denial. Until one of those,
  the link can be opened again, so somebody who starts on a phone can pick
  it up on a laptop from the same email.
- Out of the address bar at once. ``/continue/<token>`` puts the token's
  digest in the session and redirects to ``/continue``, so the page that
  renders (and anything it loads, analytics included) never sees the token.
  Opening the case cycles the session key, like a sign in.

Everything is inert while the intake journey is off: no step is recorded, no
link is minted, and the resume pages answer 404.

Once the email address matches, the session is bound to the case the same
way starting it binds it (``views.remember_denial_ref_email``), and the
person is sent to the step with an ordinary back-link reference, so the rest
of the flow works exactly as it does for somebody who never left. Full
statement: ``docs/back-link-references.md``.
"""

import datetime
import hashlib
import hmac
import secrets
import typing

from django.db.models import F
from django.utils import timezone

from loguru import logger

# The pages a case can be reopened at, in the order the form reaches them.
# Each is the name of a route that renders its step from a back-link
# reference: health history, plan documents, the extraction step, the review
# of what was extracted, and the questions page.
RESUME_STEPS = ("hh", "dvc", "eev", "categorize_review", "find_next_steps")
FIRST_STEP = RESUME_STEPS[0]

# See the module docstring: the time between the 24 hour nudge and the
# journey closing at 3 days.
RESUME_LINK_TTL = datetime.timedelta(hours=48)
# Wrong email addresses typed against one link; reaching this revokes it.
RESUME_LINK_MAX_WRONG_EMAILS = 5
RESUME_TOKEN_BYTES = 32
# A token_urlsafe(32) is 43 characters. Anything much longer is not ours.
_MAX_TOKEN_LENGTH = 128

OPENED = "opened"
WRONG_EMAIL = "wrong_email"
DEAD = "dead"


def enabled() -> bool:
    """Whether the intake journey, and so everything here, is on."""
    from fighthealthinsurance.temporal_client import _intake_enabled

    return bool(_intake_enabled())


def link_days() -> int:
    """The link's lifetime in whole days, for the email and the pages."""
    return int(RESUME_LINK_TTL.total_seconds() // 86400)


def token_digest(token: str) -> str:
    """What the server keeps of a token: its SHA-256, as hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def plausible_token(token: typing.Any) -> bool:
    return isinstance(token, str) and 0 < len(token) <= _MAX_TOKEN_LENGTH


def note_step(denial_id: typing.Any, step: str) -> None:
    """Record that a case has reached ``step``. Never raises.

    Only ever moves forward: a person who goes back a page and leaves is
    brought back to the furthest page they reached, where everything they
    entered before it is already saved. Moving forward revokes any resume
    link minted for the earlier step, because the case has moved on. Does
    nothing while the intake journey is off.
    """
    from fighthealthinsurance.models import Denial, IntakeResumePoint

    if step not in RESUME_STEPS or not denial_id:
        return
    try:
        if not enabled():
            return
        if not Denial.objects.filter(pk=denial_id).exists():
            return
        point, created = IntakeResumePoint.objects.get_or_create(
            denial_id=denial_id, defaults={"step": step}
        )
        if created:
            return
        earlier = RESUME_STEPS[: RESUME_STEPS.index(step)]
        # One conditional UPDATE, so two requests racing can only ever move
        # the step forward.
        IntakeResumePoint.objects.filter(pk=point.pk, step__in=earlier).update(
            step=step,
            token_digest=None,
            token_expires_at=None,
            wrong_email_attempts=0,
            updated_at=timezone.now(),
        )
    except Exception:
        logger.opt(exception=True).warning(
            f"intake resume: could not record step {step} for denial {denial_id}"
        )


async def amint_link(denial: typing.Any) -> str:
    """Mint the resume link for this case and return its token.

    Replaces any earlier link for the case. The token goes into the email
    and nowhere else; only its digest is stored. The caller checks that the
    intake journey is on.
    """
    from fighthealthinsurance.models import IntakeResumePoint

    token = secrets.token_urlsafe(RESUME_TOKEN_BYTES)
    now = timezone.now()
    point, _ = await IntakeResumePoint.objects.aget_or_create(
        denial=denial, defaults={"step": FIRST_STEP}
    )
    await IntakeResumePoint.objects.filter(pk=point.pk).aupdate(
        token_digest=token_digest(token),
        token_expires_at=now + RESUME_LINK_TTL,
        wrong_email_attempts=0,
        updated_at=now,
    )
    return token


def live_point(digest: typing.Any) -> typing.Optional[typing.Any]:
    """The resume point a token digest still opens, or None.

    None for a digest nobody holds (never minted, replaced, revoked, or
    deleted with the case or at the journey's closure), one past its
    expiry, and a case whose form has been completed.
    """
    from fighthealthinsurance.models import IntakeJourneyEvent, IntakeResumePoint

    if not isinstance(digest, str) or not digest or not enabled():
        return None
    point = (
        IntakeResumePoint.objects.select_related("denial")
        .filter(token_digest=digest, token_expires_at__gt=timezone.now())
        .first()
    )
    if point is None:
        return None
    if IntakeJourneyEvent.objects.filter(
        denial_id=point.denial_id, event_type=IntakeJourneyEvent.FORM_COMPLETED
    ).exists():
        return None
    return point


def open_case(
    digest: typing.Any, email: str
) -> typing.Tuple[str, typing.Optional[typing.Any]]:
    """Check the typed email address against the case a link names.

    Returns ``(OPENED, point)`` when it matches, ``(WRONG_EMAIL, None)``
    when it does not, and ``(DEAD, None)`` when the link opens nothing,
    including the wrong address that used up its last try. Opening does not
    use the link up; see the module docstring for what does.
    """
    from fighthealthinsurance.models import Denial, IntakeResumePoint

    point = live_point(digest)
    if point is None:
        return DEAD, None
    typed = Denial.get_hashed_email(email or "")
    if hmac.compare_digest(typed, point.denial.hashed_email or ""):
        return OPENED, point
    IntakeResumePoint.objects.filter(pk=point.pk, token_digest=digest).update(
        wrong_email_attempts=F("wrong_email_attempts") + 1
    )
    revoked = IntakeResumePoint.objects.filter(
        pk=point.pk,
        token_digest=digest,
        wrong_email_attempts__gte=RESUME_LINK_MAX_WRONG_EMAILS,
    ).update(token_digest=None, token_expires_at=None, updated_at=timezone.now())
    if revoked:
        logger.info(
            f"intake resume: link for denial {point.denial_id} revoked after "
            f"{RESUME_LINK_MAX_WRONG_EMAILS} wrong email addresses"
        )
        return DEAD, None
    return WRONG_EMAIL, None


async def aforget(denial: typing.Any) -> int:
    """Delete the case's resume point (its step and any link). Returns rows deleted."""
    from fighthealthinsurance.models import IntakeResumePoint

    deleted, _ = await IntakeResumePoint.objects.filter(denial=denial).adelete()
    return deleted

"""Professional interest sign-ups: the submission log, repeats, the cap and spam.

Every intake (the /pro_version form, the fightpaperwork.com classic form, direct
posts to the thank-you page and the retired REST endpoint) goes through
:func:`record_professional_submission`. A person is one InterestedProfessional
lead, matched by email (any case) or by phone digits. Their first submission
creates the lead as before; the second and third get one short "we already
have your details" email instead of another thank-you, only when the address
matches the one on file; from the fourth on nothing is sent. Every outcome
lands on the same thank-you page. Spam is logged and dropped before anything is sent.
"""

import re
from dataclasses import dataclass
from typing import Callable, Optional

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Case, F, Func, OuterRef, Q, Subquery, Value, When
from django.db.models.expressions import Combinable
from django.db.models.functions import Lower, Replace

from loguru import logger

from fighthealthinsurance.constants import FHI_SUPPORT_EMAIL
from fighthealthinsurance.followup_emails import ThankyouEmailSender
from fighthealthinsurance.models import (
    InterestedProfessional,
    ModelHealthAlertState,
    ProfessionalInquiry,
    ScheduledEmail,
)
from fighthealthinsurance.utils import (
    RETURNING_LEAD_NOTIFY_COOLDOWN_SECONDS,
    mask_email_for_logging,
    notify_interested_professional,
    send_fallback_email,
    should_notify_returning_lead,
)

Outcome = ProfessionalInquiry.Outcome

# Submissions a person gets a reply to; later ones are logged and dropped.
MAX_ANSWERED_SUBMISSIONS = 3

ALREADY_HAVE_DETAILS_SUBJECT = "We already have your details"

# A message is logged up to this length; a bot's post is unvalidated.
MAX_LOGGED_MESSAGE_CHARS = 10000

# A full US number; a 7-digit local number can't tell two practices apart.
MIN_PHONE_DIGITS = 10
# The longest international number (E.164); anything longer isn't a phone.
MAX_PHONE_DIGITS = 15

# Characters stripped from a stored phone number before comparing digits.
_PHONE_PUNCTUATION = (" ", "-", "(", ")", ".", "+", "/")

_LINK = re.compile(r"(?:https?://|\bwww\.)\S+", re.IGNORECASE)


def normalize_phone(value: str) -> str:
    """The phone number's digits without a leading US 1, or "" if not a number."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS else ""


# Spam rules. A strong rule flags a submission on its own; soft rules need two
# hits, so one sales-sounding phrase in a real practice's message passes.


@dataclass(frozen=True)
class Submission:
    """The text a spam rule can look at."""

    email: str
    name: str
    text: str  # every free-text field, joined
    honeypot: bool


@dataclass(frozen=True)
class SpamRule:
    reason: str
    strong: bool
    matches: Callable[[Submission], bool]


def _phrase(pattern: str) -> Callable[[Submission], bool]:
    compiled = re.compile(pattern, re.IGNORECASE)
    return lambda s: bool(compiled.search(s.text))


def _spam_tld(s: Submission) -> bool:
    # The pro-connector queue already treats these domains as spam.
    from fighthealthinsurance.proconnector import SPAM_EMAIL_TLDS

    return s.email.strip().lower().endswith(SPAM_EMAIL_TLDS)


SPAM_RULES: tuple[SpamRule, ...] = (
    SpamRule("honeypot field filled", True, lambda s: s.honeypot),
    SpamRule("link in name", True, lambda s: bool(_LINK.search(s.name))),
    SpamRule("3 or more links", True, lambda s: len(_LINK.findall(s.text)) >= 3),
    SpamRule("email domain on spam list", True, _spam_tld),
    SpamRule(
        "SEO pitch",
        True,
        _phrase(r"\bseo\b|\bsearch engine optimi[sz]ation\b"),
    ),
    SpamRule(
        "backlinks pitch", True, _phrase(r"\bback[\s-]?links?\b|\bguest posts?\b")
    ),
    SpamRule(
        "search ranking pitch",
        True,
        _phrase(
            r"\b(?:first|top|1st) page (?:of|on) google\b|\bgoogle rankings?\b"
            r"|\brank(?:s|ed|ing)?\s+(?:higher|first|#?1)\b"
        ),
    ),
    SpamRule(
        "crypto pitch",
        True,
        _phrase(r"\bcrypto(?:currenc(?:y|ies))?\b|\bbitcoin\b|\bforex\b"),
    ),
    SpamRule(
        "guaranteed results pitch",
        True,
        _phrase(r"\bguarantee[ds]?\s+(?:results?|rankings?|traffic|leads|roi|sales)\b"),
    ),
    SpamRule("two links", False, lambda s: len(_LINK.findall(s.text)) == 2),
    SpamRule("lead generation pitch", False, _phrase(r"\blead[\s-]?gen(?:eration)?\b")),
    SpamRule(
        "web design pitch",
        False,
        _phrase(r"\bweb(?:site)?\s*(?:design|development|redesign)\b"),
    ),
    SpamRule(
        "marketing services pitch",
        False,
        _phrase(
            r"\b(?:digital|social media|online|internet|email)\s+marketing\b"
            r"|\bmarketing\s+(?:services|agency|solutions|campaigns?)\b"
        ),
    ),
    SpamRule(
        "outsourcing pitch",
        False,
        _phrase(
            r"\boutsourc\w*\s+(?:services?|solutions?|company|companies"
            r"|agency|partner|team)\b"
        ),
    ),
    SpamRule(
        "virtual assistants pitch",
        False,
        _phrase(r"\bvirtual (?:assistants?|staff(?:ing)?)\b"),
    ),
)


def spam_reason(pro: InterestedProfessional, *, honeypot: bool = False) -> str:
    """Why this submission looks like spam, or "" when it doesn't."""
    fields = (
        pro.name,
        pro.business_name,
        pro.job_title_or_provider_type,
        pro.most_common_denial,
        pro.address,
        pro.comments,
    )
    submission = Submission(
        email=pro.email or "",
        name=pro.name or "",
        text="\n".join(f or "" for f in fields),
        honeypot=honeypot,
    )
    hits = [rule for rule in SPAM_RULES if rule.matches(submission)]
    strong = [rule.reason for rule in hits if rule.strong]
    if strong:
        return "; ".join(strong)
    if len(hits) >= 2:
        return "; ".join(rule.reason for rule in hits)
    return ""


def _stored_phone_digits() -> Combinable:
    """Stored phone numbers with common punctuation removed, as SQL."""
    expression: Combinable = F("phone_number")
    for character in _PHONE_PUNCTUATION:
        expression = Replace(expression, Value(character), Value(""))
    return expression


def find_lead(email: str, phone_digits: str) -> Optional[InterestedProfessional]:
    """The existing lead this submission belongs to, if any.

    Email first (any case), then an earlier submission from this email that
    was matched to a lead, then the phone number by digits. Spam rows aren't
    evidence: a bot can put anyone's details in one.
    """
    email = (email or "").strip()
    if email:
        lead = (
            InterestedProfessional.objects.filter(email__iexact=email)
            .order_by("id")
            .first()
        )
        if lead is not None:
            return lead
        inquiry = (
            ProfessionalInquiry.objects.filter(
                email__iexact=email, interested_professional__isnull=False
            )
            .exclude(outcome=Outcome.SPAM)
            .select_related("interested_professional")
            .order_by("id")
            .first()
        )
        if inquiry is not None:
            return inquiry.interested_professional
    if not phone_digits:
        return None
    inquiry = (
        ProfessionalInquiry.objects.filter(
            phone_digits=phone_digits, interested_professional__isnull=False
        )
        .exclude(outcome=Outcome.SPAM)
        .select_related("interested_professional")
        .order_by("id")
        .first()
    )
    if inquiry is not None:
        return inquiry.interested_professional
    return (
        InterestedProfessional.objects.annotate(digits=_stored_phone_digits())
        .filter(Q(digits=phone_digits) | Q(digits=f"1{phone_digits}"))
        .order_by("id")
        .first()
    )


def prior_submissions(lead: InterestedProfessional) -> int:
    """Non-spam submissions so far, plus one for a lead made before the log."""
    logged = lead.inquiries.exclude(outcome=Outcome.SPAM)
    unlogged_first = not logged.filter(created_lead=True).exists()
    return logged.count() + int(unlogged_first)


def _log(
    pro: InterestedProfessional,
    *,
    lead: Optional[InterestedProfessional],
    source: str,
    outcome: str,
    phone_digits: str,
    spam_reason: str = "",
    created_lead: bool = False,
) -> ProfessionalInquiry:
    def clip(value: Optional[str], field: str) -> str:
        limit = getattr(ProfessionalInquiry._meta.get_field(field), "max_length", None)
        return (value or "")[:limit] if limit else (value or "")

    return ProfessionalInquiry.objects.create(
        interested_professional=lead,
        name=clip(pro.name, "name"),
        email=clip((pro.email or "").strip(), "email"),
        phone_number=clip(pro.phone_number, "phone_number"),
        phone_digits=phone_digits,
        business_name=clip(pro.business_name, "business_name"),
        message=(pro.comments or "")[:MAX_LOGGED_MESSAGE_CHARS],
        source=clip(source, "source"),
        outcome=outcome,
        spam_reason=clip(spam_reason, "spam_reason"),
        created_lead=created_lead,
    )


def intro_was_sent(email: str) -> bool:
    """Whether a Cofactor AI introduction already went to this address."""
    return (
        InterestedProfessional.objects.filter(
            email__iexact=email, proconnector_sent_at__isnull=False
        ).exists()
        or ScheduledEmail.objects.filter(
            to_email__iexact=email, purpose="proconnector_intro", sent=True
        ).exists()
    )


def should_reply_to_returning_lead(lead: InterestedProfessional) -> bool:
    """At most one "already have your details" email per lead per window.

    Fails closed: a missed courtesy email beats mailing an address on repeat.
    """
    try:
        return ModelHealthAlertState.try_claim(
            f"prr:{lead.id}", RETURNING_LEAD_NOTIFY_COOLDOWN_SECONDS
        )
    except Exception:
        logger.opt(exception=True).warning(
            "Returning-lead reply cooldown check failed; not replying"
        )
        return False


def send_already_have_details_email(pro: InterestedProfessional) -> None:
    """The short reply to a second or third submission. Never raises."""
    try:
        send_fallback_email(
            template_name="professional_already_have_details",
            subject=ALREADY_HAVE_DETAILS_SUBJECT,
            context={
                "name": pro.name,
                "support_email": FHI_SUPPORT_EMAIL,
                "intro_sent": intro_was_sent(pro.email),
            },
            to_email=pro.email,
        )
    except Exception as e:
        # Only the type: SMTP refusals carry the full address in their text.
        logger.warning(
            "Error sending pro signup repeat reply to "
            f"{mask_email_for_logging(pro.email)}: {type(e).__name__}"
        )


def _send_thankyou(pro: InterestedProfessional) -> None:
    try:
        ThankyouEmailSender().dosend(interested_pro=pro)
    except Exception as e:
        logger.warning(
            f"Error sending immediate pro signup thank-you "
            f"(interested_professional_id={pro.id}): {type(e).__name__}"
        )


def record_professional_submission(
    pro: InterestedProfessional,
    *,
    source: str,
    subject_prefix: str,
    honeypot: bool = False,
    email_submitter: bool = True,
) -> str:
    """Log one submission and do what its place in the person's history calls
    for; returns the outcome.

    ``pro`` is unsaved and holds what was submitted. Only a first submission
    saves it; a repeat never overwrites the stored lead. ``email_submitter``
    False sends the submitter nothing (the REST endpoint never did). Mail
    failures are logged, not raised.
    """
    phone_digits = normalize_phone(pro.phone_number)
    lead = find_lead(pro.email, phone_digits)
    reason = spam_reason(pro, honeypot=honeypot)
    if reason:
        _log(
            pro,
            lead=lead,
            source=source,
            outcome=Outcome.SPAM,
            phone_digits=phone_digits,
            spam_reason=reason,
        )
        logger.info(f"Pro signup from {source} logged as spam: {reason}")
        return Outcome.SPAM

    if lead is None:
        pro.clicked_for_paid = False
        pro.save()
        _log(
            pro,
            lead=pro,
            source=source,
            outcome=Outcome.NEW,
            phone_digits=phone_digits,
            created_lead=True,
        )
        notify_interested_professional(
            pro, source=source, subject=f"{subject_prefix} #{pro.id}"
        )
        if email_submitter:
            _send_thankyou(pro)
        return Outcome.NEW

    # Lock the lead so two sends at once can't both land under the cap.
    with transaction.atomic():
        list(
            InterestedProfessional.objects.select_for_update()
            .filter(pk=lead.pk)
            .values_list("pk", flat=True)
        )
        outcome = (
            Outcome.CAPPED
            if prior_submissions(lead) >= MAX_ANSWERED_SUBMISSIONS
            else Outcome.REPEAT
        )
        _log(pro, lead=lead, source=source, outcome=outcome, phone_digits=phone_digits)
    if outcome == Outcome.CAPPED:
        return Outcome.CAPPED

    # Borrow the lead's pk so the notification's admin link resolves; never save.
    pro.id = lead.id
    if should_notify_returning_lead(lead.email):
        notify_interested_professional(
            pro, source=source, subject=f"{subject_prefix} #{lead.id} (returning)"
        )
    # Reply only to the address on file: a phone match with a new address
    # would tell whoever typed it that the number is already a lead.
    same_address = (pro.email or "").strip().lower() == (
        lead.email or ""
    ).strip().lower()
    if email_submitter and same_address and should_reply_to_returning_lead(lead):
        send_already_have_details_email(pro)
    return Outcome.REPEAT


def mark_not_spam(inquiry: ProfessionalInquiry) -> bool:
    """Clear a spam-flagged submission and put the person in as a lead.

    Links the matching lead, or creates one from what was submitted. Sends
    nothing. Returns False when the row wasn't flagged as spam.
    """
    if inquiry.outcome != Outcome.SPAM:
        return False
    lead = inquiry.interested_professional or find_lead(
        inquiry.email, inquiry.phone_digits
    )
    created = lead is None and _is_valid_email(inquiry.email)
    if created:
        lead = InterestedProfessional.objects.create(
            name=inquiry.name,
            email=inquiry.email,
            phone_number=inquiry.phone_number,
            business_name=inquiry.business_name,
            comments=inquiry.message,
            clicked_for_paid=False,
        )
    inquiry.interested_professional = lead
    inquiry.outcome = Outcome.CLEARED
    inquiry.created_lead = created
    inquiry.save(update_fields=["interested_professional", "outcome", "created_lead"])
    return True


def _is_valid_email(value: str) -> bool:
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def forget_submissions(email: str) -> None:
    """Delete this address's logged submissions (Remove my data)."""
    ProfessionalInquiry.objects.filter(email__iexact=(email or "").strip()).delete()


def person_aggregate(function: str, field: str, output_field):
    """One person's aggregate over the log, for annotating an inquiry list.

    A person is the linked lead, or the email for rows with no lead.
    """

    def over(rows):
        return Subquery(
            rows.order_by()
            .annotate(
                value=Func(F(field), function=function, output_field=output_field)
            )
            .values("value")[:1],
            output_field=output_field,
        )

    by_lead = ProfessionalInquiry.objects.filter(
        interested_professional=OuterRef("interested_professional")
    )
    by_email = ProfessionalInquiry.objects.annotate(lower_email=Lower("email")).filter(
        interested_professional__isnull=True, lower_email=Lower(OuterRef("email"))
    )
    return Case(
        When(interested_professional__isnull=True, then=over(by_email)),
        default=over(by_lead),
        output_field=output_field,
    )

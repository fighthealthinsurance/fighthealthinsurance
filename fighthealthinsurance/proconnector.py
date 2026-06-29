"""Pro-connector (Cofactor AI sourcing agreement) workflow helpers.

Fight Health Insurance initially launched a professional product (Fight
Paperwork) but has since refocused on its consumer mission. Under a sourcing
agreement, FHI introduces interested professionals who may benefit from Cofactor
AI's AI-powered support for appeals, prior authorization, and related backend
workflows.

This module builds the introduction email -- optionally personalized by an
(external) LLM and always falling back to a safe approved base template -- and
sends it with the professional contact address CC'd. Staff review and edit every
draft before it is sent; nothing here sends automatically.

Wording constraints (enforced by ``_is_safe_intro_draft``):
  * Never describe Cofactor AI as a "partner" or say FHI "partnered" with them.
    We are NOT announcing a partnership; this is a sourcing agreement to
    introduce interested professionals.
  * Always include the plain-language compensation disclosure.
"""

import re
import urllib.parse
from typing import Optional

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Lower
from django.utils import timezone

from loguru import logger

from fighthealthinsurance.email_utils import get_email_domain, is_blocked_email
from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import InterestedProfessional, ScheduledEmail
from fighthealthinsurance.scheduled_emails import enqueue_scheduled_email
from fighthealthinsurance.utils import send_fallback_email

# Fallback CC address when no professional/support email setting is configured.
DEFAULT_PROFESSIONAL_CC_EMAIL = "professional@fighthealthinsurance.com"

# Obvious test / spam signups we never introduce. These are filtered out of the
# processing queue and the CSV export entirely (never shown, never counted)
# rather than skipped, so they don't clutter the staff workflow. Includes FHI's
# own internal test accounts (mirroring charts' signup-analytics exclusion) so
# outreach and the export never target ourselves.
FILTERED_EMAILS: frozenset[str] = frozenset(
    {
        "testing@example.com",
        "farts@farts.com",
        "holden@pigscanfly.ca",
    }
)
# Signups on these TLDs are treated as spam / out of scope and filtered out.
SPAM_EMAIL_TLDS: tuple[str, ...] = (".ru", ".ua")

# Known personal / free-email provider domains. Professionals using a business
# or organizational domain tend to be the strongest fit for Cofactor AI, so the
# processing queue prioritizes them; these personal domains are still processed,
# just later in the order (see get_next_interested_professional). This is purely
# a sort signal -- nobody is excluded.
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.co.uk",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "rocketmail.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "fastmail.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "bellsouth.net",
        "cox.net",
    }
)

PROCONNECTOR_INTRO_SUBJECT = (
    "An introduction to Cofactor AI from Fight Health Insurance"
)

# The approved base email. Staff can edit it before sending, the AI draft is
# personalized from it, and it is the always-safe fallback when AI drafting is
# unavailable or produces unsafe output. ``{greeting_name}`` is filled with the
# professional's name or a neutral greeting when the name is missing.
BASE_INTRO_EMAIL = """Dear {greeting_name},

Thank you so much for your interest in the professional version of Fight Health Insurance.

We're excited to introduce you to Cofactor AI, who are cc'd here. After our initial work launching Fight Paperwork, we've refocused Fight Health Insurance on our consumer mission, and we now have a sourcing agreement with Cofactor AI to introduce interested professionals who may benefit from their AI-powered support for appeals, prior authorization, and related backend workflows.

As part of this sourcing agreement, Fight Health Insurance may receive compensation if you choose to work with Cofactor AI; that support helps us continue our consumer-focused mission.

We've spent a lot of time talking with the Cofactor AI team, and they've built some truly impressive AI agents for appeals and administrative tasks. We think they may be a strong fit for the kinds of professional workflows many of you reached out to us about.

If you're interested in talking with them, feel free to reply here. Cofactor AI may also reach out directly to see whether they can help.

Thank you again for your interest and for trusting us with this work.

Sincerely,

Holden Karau, Melanie Warrick, and the Fight Health Insurance team"""


INTRO_SYSTEM_PROMPT = (
    "You are an assistant for Fight Health Insurance (FHI) drafting a warm, "
    "professional, concise introduction email to a healthcare professional who "
    "expressed interest in FHI's professional product. FHI has refocused on its "
    "consumer mission and has a SOURCING AGREEMENT to introduce interested "
    "professionals to Cofactor AI, who will be CC'd on the email.\n\n"
    "Hard rules:\n"
    "- Do NOT call Cofactor AI a 'partner' and do NOT say FHI 'partnered' with "
    "them. Use 'sourcing agreement' or 'agreement to introduce interested "
    "professionals' instead. We are NOT announcing a partnership.\n"
    "- Include a brief, plain-language disclosure that FHI may receive "
    "compensation if the recipient chooses to work with Cofactor AI, and that "
    "this support helps FHI continue its consumer-focused mission. Do NOT "
    "include exact financial terms.\n"
    "- Do NOT imply FHI is transferring customer obligations, providing "
    "professional services through Cofactor AI, or jointly delivering services "
    "with Cofactor AI.\n"
    "- Do NOT invent facts about the recipient. Personalize only from the "
    "provided known information; if a detail is missing, simply leave it out.\n"
    "- Keep the warm tone, overall structure, and the sign-off. Output only the "
    "email body (no subject line and no commentary)."
)


def get_professional_cc_email() -> str:
    """CC address for intro emails: the configured ``PROFESSIONAL_CC_EMAIL``
    setting if set, otherwise the ``professional@`` default."""
    value = getattr(settings, "PROFESSIONAL_CC_EMAIL", None)
    return str(value) if value else DEFAULT_PROFESSIONAL_CC_EMAIL


def _greeting_name(pro: InterestedProfessional) -> str:
    """Name for the salutation, or a neutral greeting when unknown."""
    name = (pro.name or "").strip()
    return name if name else "there"


def build_base_intro_email(pro: InterestedProfessional) -> str:
    """Render the approved base email for ``pro`` (the always-safe fallback)."""
    return BASE_INTRO_EMAIL.format(greeting_name=_greeting_name(pro))


def build_search_links(pro: InterestedProfessional) -> dict[str, Optional[str]]:
    """Google + LinkedIn search URLs for the person's name plus organization.

    Organization (``business_name``) is included only when present. Returns
    ``None`` for each link when there is nothing to search on.
    """
    parts = [p.strip() for p in (pro.name, pro.business_name) if p and p.strip()]
    terms = " ".join(parts).strip()
    if not terms:
        return {"google": None, "linkedin": None}
    q = urllib.parse.quote(terms)
    return {
        "google": f"https://www.google.com/search?q={q}",
        "linkedin": f"https://www.linkedin.com/search/results/all/?keywords={q}",
    }


def describe_known_info(pro: InterestedProfessional) -> str:
    """Plain-text summary of the known fields for the AI prompt.

    Only non-empty fields are included so the model is never tempted to fill in
    a blank. The model has no 'specialty'/'signup source' columns, so the
    closest available signal (most common denial / stated needs) is surfaced
    and anything missing is simply omitted.
    """
    candidate_fields = [
        ("Name", pro.name),
        ("Organization", pro.business_name),
        ("Role / provider type", pro.job_title_or_provider_type),
        ("Most common denial / stated needs", pro.most_common_denial),
        ("Notes", pro.comments),
    ]
    lines = [
        f"- {label}: {(value or '').strip()}"
        for label, value in candidate_fields
        if value and value.strip()
    ]
    if not lines:
        return "(No additional details beyond their email address are known.)"
    return "\n".join(lines)


def _claims_cofactor_relationship(text: str) -> bool:
    """True if the draft uses "partner" wording close to a "Cofactor" mention.

    The prohibition is specifically about describing *Cofactor AI* as a partner
    / saying FHI "partnered" with them. We flag any ``partner*`` token within a
    few words of a ``cofactor`` token (either order). Proximity matching means
    benign, unrelated uses -- a practice named "... Partners", a "billing
    partner" -- don't needlessly discard an otherwise-good draft.
    """
    tokens = re.findall(r"[a-z]+", text.lower())
    partner_positions = [i for i, t in enumerate(tokens) if t.startswith("partner")]
    if not partner_positions:
        return False
    cofactor_positions = [i for i, t in enumerate(tokens) if t == "cofactor"]
    return any(abs(p - c) <= 6 for p in partner_positions for c in cofactor_positions)


def _is_safe_intro_draft(text: Optional[str]) -> bool:
    """Guard an AI draft against the two hard wording requirements.

    Rejects drafts that (a) describe Cofactor AI as a "partner" / claim a
    partnership with them, or (b) omit the plain-language compensation
    disclosure. A rejected draft falls back to the always-safe base email.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 100:
        return False
    lowered = stripped.lower()
    if _claims_cofactor_relationship(lowered):
        return False
    if "compensat" not in lowered:  # compensation / compensated
        return False
    return True


async def agenerate_intro_email(pro: InterestedProfessional) -> str:
    """Return an AI-personalized intro draft, falling back to the base email.

    External models are preferred here because FHI's internal models are tuned
    for adversarial appeals work rather than warm outreach. Any failure -- no
    external models available, timeout, error, or output that violates the
    wording rules -- falls back to the safe approved base email.
    """
    base = build_base_intro_email(pro)
    try:
        models = list(ml_router.external_models_by_cost)
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Proconnector: external model lookup failed, using base email: {e}"
        )
        return base
    if not models:
        logger.info("Proconnector: no external models available, using base email")
        return base

    prompt = (
        "Personalize the following base email lightly for this professional, "
        "using only the known information. Do not invent anything.\n\n"
        f"Known information:\n{describe_known_info(pro)}\n\n"
        f"Base email:\n{base}\n"
    )
    # Reuse the shared model-fallback helper (timeout + per-model retry +
    # validator), pointing it at external models and our safety validator. It
    # returns the first safe draft, or None if every model fails -> base email.
    result = await infer_with_fallback(
        system_prompts=[INTRO_SYSTEM_PROMPT],
        prompt=prompt,
        temperature=0.4,
        timeout=30.0,
        label="proconnector intro",
        validator=_is_safe_intro_draft,
        models=models[:4],
    )
    return result or base


def generate_intro_email(pro: InterestedProfessional) -> str:
    """Synchronous wrapper around :func:`agenerate_intro_email` for sync views."""
    return async_to_sync(agenerate_intro_email)(pro)


def _intro_cc_recipients(extra_cc: Optional[list[str]] = None) -> list[str]:
    """CC list for an intro email: the professional contact address first, then
    any caller-supplied extras, deduplicated case-insensitively in order."""
    professional_cc = get_professional_cc_email()
    recipients = [professional_cc]
    seen = {professional_cc.lower()}
    for addr in extra_cc or []:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            recipients.append(addr)
    return recipients


def send_proconnector_intro_email(
    pro: InterestedProfessional,
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
) -> None:
    """Send the (edited) intro email to ``pro`` now, always CC'ing the
    professional address.

    The professional/support address is always included; any caller-supplied
    ``cc`` is treated as *additional* recipients, deduplicated while preserving
    order. Raises on send failure -- including a blocked/unsendable recipient,
    which ``send_fallback_email`` would otherwise skip silently -- so the caller
    can avoid marking the record attempted.
    """
    if is_blocked_email(pro.email):
        # send_fallback_email silently skips blocked recipients; fail closed so
        # the caller doesn't record a send that never actually happened.
        raise ValueError("Recipient email is blocked or unsendable")
    send_fallback_email(
        subject=subject,
        template_name="proconnector_intro",
        context={"body": body, "name": pro.name},
        to_email=pro.email,
        cc=_intro_cc_recipients(cc),
    )


def queue_proconnector_intro_email(
    pro: InterestedProfessional,
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
) -> ScheduledEmail:
    """Queue the (edited) intro email to send during the recipient's likely
    business hours instead of immediately.

    Mirrors :func:`send_proconnector_intro_email` (same CC handling and blocked
    check) but enqueues a :class:`ScheduledEmail` gated on the business-hours
    window derived from the professional's phone area code -- defaulting to the
    conservative cross-US Pacific overlap when no usable phone is known (see
    ``business_hours.py``). Raises on a blocked/unsendable recipient so the
    caller can avoid marking the record attempted.
    """
    if is_blocked_email(pro.email):
        raise ValueError("Recipient email is blocked or unsendable")
    return enqueue_scheduled_email(
        to_email=pro.email,
        subject=subject,
        template_name="proconnector_intro",
        context={"body": body, "name": pro.name},
        cc=_intro_cc_recipients(cc),
        phone=pro.phone_number,
        purpose="proconnector_intro",
    )


def is_personal_email_domain(email: Optional[str]) -> bool:
    """Whether ``email`` uses a known personal / free-email provider domain."""
    return get_email_domain(email) in PERSONAL_EMAIL_DOMAINS


def _personal_domain_q() -> Q:
    """Q matching records whose email is on a personal/free-email domain.

    Anchored on the ``@`` so e.g. ``foo@notgmail.com`` does not match
    ``gmail.com``. Kept in sync with :data:`PERSONAL_EMAIL_DOMAINS`.
    """
    q = Q()
    for domain in PERSONAL_EMAIL_DOMAINS:
        q |= Q(email__iendswith=f"@{domain}")
    return q


def _exclude_filtered(
    qs: "QuerySet[InterestedProfessional]",
) -> "QuerySet[InterestedProfessional]":
    """Drop obvious test/spam signups from a queryset.

    Shared by the processing queue and the CSV export: excludes a known test
    address, spam-associated TLDs (.ru/.ua), and records whose name field
    contains a URL ("http", catching http:// and https://) -- a reliable spam
    signal.
    """
    qs = qs.exclude(name__icontains="http")
    for email in FILTERED_EMAILS:
        qs = qs.exclude(email__iexact=email)
    for tld in SPAM_EMAIL_TLDS:
        qs = qs.exclude(email__iendswith=tld)
    return qs


def non_spam_interested_professionals() -> "QuerySet[InterestedProfessional]":
    """All interested professionals except filtered test/spam signups.

    Used by the CSV export -- includes records regardless of whether they have
    been introduced yet.
    """
    return _exclude_filtered(InterestedProfessional.objects.all())


def processable_queryset() -> "QuerySet[InterestedProfessional]":
    """Interested professionals eligible for pro-connector processing.

    Excludes already attempted/skipped records and filtered test/spam signups.
    """
    return _exclude_filtered(
        InterestedProfessional.objects.filter(
            proconnector_attempted=False,
            proconnector_skipped=False,
        )
    )


def get_next_interested_professional() -> Optional[InterestedProfessional]:
    """Next interested professional to process, or ``None``.

    Non-personal (business / organizational) email domains are prioritized
    because they tend to be the strongest fit for Cofactor AI; personal
    free-email domains (gmail, outlook, etc.) still appear, just later. Within
    each group we go oldest-first. Records sharing an email are collapsed by
    processing them together (see mark_email_sent / mark_email_skipped), so the
    same address is never shown twice.
    """
    return (
        processable_queryset()
        .annotate(
            _personal_domain=Case(
                When(_personal_domain_q(), then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("_personal_domain", "signup_date", "id")
        .first()
    )


def remaining_interested_professionals_count() -> int:
    """Count of distinct unprocessed emails still awaiting an introduction.

    Counted by unique (case-insensitive) email since duplicate signups for the
    same address are processed as one.
    """
    return (
        processable_queryset()
        .annotate(_lower_email=Lower("email"))
        .values("_lower_email")
        .distinct()
        .count()
    )


def claim_email_for_send(email: str) -> int:
    """Atomically claim every unprocessed record sharing ``email`` for sending.

    Two staff sessions are handed the *same* next record by
    :func:`get_next_interested_professional`, so a plain "is it attempted yet?"
    check leaves a window where both pass and both send. This collapses the
    check-and-mark into a single conditional UPDATE: only the first caller flips
    ``proconnector_attempted`` and gets a non-zero count; a concurrent caller
    sees ``0`` and must skip. Released via :func:`release_email_claim` if the
    subsequent send/queue fails. Returns the number of rows claimed.
    """
    return InterestedProfessional.objects.filter(
        email__iexact=email,
        proconnector_attempted=False,
        proconnector_skipped=False,
    ).update(proconnector_attempted=True)


def release_email_claim(email: str) -> int:
    """Undo a :func:`claim_email_for_send` claim after a failed send/queue.

    Only releases rows that were never actually delivered (``sent_at`` null) and
    not skipped, so an already-sent or skipped record is left untouched. This
    returns the address to the queue so staff can retry. Returns the number of
    rows released.
    """
    return InterestedProfessional.objects.filter(
        email__iexact=email,
        proconnector_attempted=True,
        proconnector_sent_at__isnull=True,
        proconnector_skipped=False,
    ).update(proconnector_attempted=False)


def mark_email_sent(email: str, body: str) -> int:
    """Mark every record sharing ``email`` as sent (attempted) with ``body``.

    Duplicate signups for the same address are all resolved by a single send,
    so the address never resurfaces in the queue. Returns the number updated.
    """
    return InterestedProfessional.objects.filter(email__iexact=email).update(
        proconnector_attempted=True,
        proconnector_sent_at=timezone.now(),
        proconnector_email_body=body,
    )


def mark_email_queued(email: str, body: str) -> int:
    """Mark every record sharing ``email`` as attempted (queued) with ``body``.

    Like :func:`mark_email_sent` but leaves ``proconnector_sent_at`` null: the
    intro has been handed off to the business-hours send queue but not delivered
    yet. Setting ``attempted`` removes it from the staff queue so it isn't shown
    twice; the actual delivery time lives on the ``ScheduledEmail`` row. Returns
    the number updated.
    """
    return InterestedProfessional.objects.filter(email__iexact=email).update(
        proconnector_attempted=True,
        proconnector_email_body=body,
    )


def mark_email_skipped(email: str, reason: Optional[str]) -> int:
    """Mark every *unprocessed* record sharing ``email`` as skipped.

    Filtered on not-yet-attempted/skipped so a concurrent send/queue (which
    claims the address via ``proconnector_attempted``) isn't clobbered into a
    contradictory sent+skipped state. Returns the number updated; ``0`` means
    another action already claimed the address, so the caller should just
    advance.
    """
    return InterestedProfessional.objects.filter(
        email__iexact=email,
        proconnector_attempted=False,
        proconnector_skipped=False,
    ).update(
        proconnector_skipped=True,
        proconnector_skip_reason=reason or None,
    )

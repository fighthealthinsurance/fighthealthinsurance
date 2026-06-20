"""Partner-introduction (Cofactor AI sourcing agreement) workflow helpers.

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

import asyncio
import re
import urllib.parse
from typing import Optional

from asgiref.sync import async_to_sync
from django.conf import settings

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import InterestedProfessional
from fighthealthinsurance.utils import send_fallback_email

# Fallback CC address when no professional/support email setting is configured.
DEFAULT_PROFESSIONAL_CC_EMAIL = "professional@fighthealthinsurance.com"

PARTNER_INTRO_SUBJECT = "An introduction to Cofactor AI from Fight Health Insurance"

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
    """CC address for intro emails.

    Prefer an existing configured professional/support setting; otherwise fall
    back to ``professional@``. We check a couple of likely setting names so a
    later standardization on either picks up automatically.
    """
    for attr in ("PROFESSIONAL_CC_EMAIL", "PROFESSIONAL_EMAIL"):
        value = getattr(settings, attr, None)
        if value:
            return str(value)
    return DEFAULT_PROFESSIONAL_CC_EMAIL


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


def _is_safe_intro_draft(text: Optional[str]) -> bool:
    """Guard an AI draft against the two hard wording requirements.

    Rejects drafts that (a) use any form of "partner" (we are NOT announcing a
    partnership) or (b) omit the plain-language compensation disclosure. A
    rejected draft falls back to the always-safe base email.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 100:
        return False
    lowered = stripped.lower()
    if re.search(r"partner", lowered):  # partner / partnered / partnership
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
            f"Partner intro: external model lookup failed, using base email: {e}"
        )
        return base
    if not models:
        logger.info("Partner intro: no external models available, using base email")
        return base

    prompt = (
        "Personalize the following base email lightly for this professional, "
        "using only the known information. Do not invent anything.\n\n"
        f"Known information:\n{describe_known_info(pro)}\n\n"
        f"Base email:\n{base}\n"
    )
    for model in models[:3]:
        try:
            result = await asyncio.wait_for(
                model._infer_no_context(
                    system_prompts=[INTRO_SYSTEM_PROMPT],
                    prompt=prompt,
                    temperature=0.4,
                ),
                timeout=30.0,
            )
        except Exception as e:
            logger.debug(f"Partner intro draft failed with {model}: {e}")
            continue
        text = (result or "").strip()
        if _is_safe_intro_draft(text):
            return text
        logger.debug("Partner intro draft rejected by safety check; trying next model")
    return base


def generate_intro_email(pro: InterestedProfessional) -> str:
    """Synchronous wrapper around :func:`agenerate_intro_email` for sync views."""
    return async_to_sync(agenerate_intro_email)(pro)


def send_partner_intro_email(
    pro: InterestedProfessional,
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
) -> None:
    """Send the (edited) intro email to ``pro``, CC'ing the professional address.

    Raises on send failure so the caller can avoid marking the record attempted.
    """
    cc_list = cc if cc is not None else [get_professional_cc_email()]
    send_fallback_email(
        subject=subject,
        template_name="partner_intro",
        context={"body": body, "name": pro.name},
        to_email=pro.email,
        cc=cc_list,
    )


def get_next_interested_professional() -> Optional[InterestedProfessional]:
    """Oldest interested professional not yet attempted or skipped, or ``None``."""
    return (
        InterestedProfessional.objects.filter(
            partner_intro_attempted=False,
            partner_intro_skipped=False,
        )
        .order_by("signup_date", "id")
        .first()
    )


def remaining_interested_professionals_count() -> int:
    """Count of interested professionals still awaiting partner-intro processing."""
    return InterestedProfessional.objects.filter(
        partner_intro_attempted=False,
        partner_intro_skipped=False,
    ).count()

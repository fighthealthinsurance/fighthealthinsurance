"""
ML-driven generation of regulator-friendly cover letters.

Given a Denial and an EscalationRecipient (from
`fighthealthinsurance.escalation_addresses`), produces a one-shot cover
letter tailored to that recipient's role: state DOI, plan medical
director, or DOL EBSA. Each recipient type gets a different prompt so
the letter cites the right regulatory framing — investigation /
external-review for DOI, peer-to-peer clinical review for the medical
director, ERISA § 503 / fiduciary duty for DOL EBSA.

We reuse `model.generate_prior_auth_response` because, like prior auths,
these are short letters (rather than long appeal letters that benefit
from parallel multi-temperature exploration) -- but with this module's
own system prompt and point of view, since the letter is from the
patient unless the denial is being finished by a professional.
"""

import asyncio
import datetime
import time
from typing import Any, Optional

from loguru import logger

from fighthealthinsurance.escalation_addresses import (
    RECIPIENT_DOI,
    RECIPIENT_DOL_EBSA,
    RECIPIENT_MEDICAL_DIRECTOR,
    EscalationRecipient,
)
from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.ml.ml_models import RemoteModelLike, attempt_deadline
from fighthealthinsurance.ml.ml_router import ml_router

# How many distinct models one letter may ask: ours first, then (only when
# the user allows outside models) one outside model as a last resort.
MAX_INTERNAL_ATTEMPTS = 3
MAX_EXTERNAL_ATTEMPTS = 1
# One budget for the whole letter, shared out so each model still asked gets
# its turn: a slow first model used to hold the letter for its whole 300s
# timeout, and cutting the letter off at one deadline would instead give up
# before asking the others. Each call is bounded by its share (the attempt
# deadline clamps the model's own timeout); the grace is a backstop for a
# backend that does not honour it.
LETTER_BUDGET_SECONDS = 300.0
LETTER_GRACE_SECONDS = 10.0

# System prompt for this path. The prior-auth one it used to inherit
# ("helping a healthcare professional") pushed every letter toward the
# professional's voice even when the letter is from the patient.
REGULATOR_LETTER_SYSTEM_PROMPT = (
    "You write short, formal cover letters about health insurance denials to "
    "insurance regulators and health-plan executives. Write in the voice the "
    "prompt specifies (the patient, or the treating professional), keep a "
    "factual and restrained tone, cite only statutes and facts that appear in "
    "the prompt, and output only the letter text."
)

_DOI_FRAMING = (
    "Frame this letter as a complaint to the state Department of "
    "Insurance / insurance commissioner. Politely ask the regulator to "
    "(1) investigate whether the plan complied with state and federal "
    "claims-handling rules, (2) confirm whether external (independent) "
    "medical review is available for this denial, and (3) require the "
    "insurer to respond. Cite that internal appeals are being pursued "
    "in parallel. Use a respectful, factual tone — this is a regulator, "
    "not the insurer."
)

_MEDICAL_DIRECTOR_FRAMING = (
    "Frame this letter as a direct, peer-to-peer-style request to the "
    "plan's medical director asking for a clinical re-review. Briefly "
    "describe the patient's condition and the medical necessity of the "
    "service. Request a peer-to-peer review with the treating clinician "
    "and ask the medical director to personally re-examine the denial. "
    "Use a clinical, collegial tone."
)

_DOL_EBSA_FRAMING = (
    "Frame this letter as a request for enforcement assistance to the "
    "U.S. Department of Labor's Employee Benefits Security "
    "Administration (EBSA). The plan appears to be an ERISA-covered "
    "self-funded employer plan. You may reference the ERISA "
    "claims-and-appeals regulations at 29 C.F.R. § 2560.503-1 (ERISA "
    "§ 503); summarize the denial and the specific provisions that may "
    "have been violated (e.g. failure to provide the specific reason for "
    "denial, failure to identify the internal rule or guideline relied "
    "on, or failure to meet timing requirements). Request that EBSA "
    "review the plan's compliance and open an inquiry if appropriate. "
    "Tone: factual, formal, restrained."
)

_DEFAULT_FRAMING = (
    "Frame this letter as a respectful request for the recipient to "
    "review the denial."
)

_FRAMING_BY_RECIPIENT_TYPE = {
    RECIPIENT_DOI: _DOI_FRAMING,
    RECIPIENT_MEDICAL_DIRECTOR: _MEDICAL_DIRECTOR_FRAMING,
    RECIPIENT_DOL_EBSA: _DOL_EBSA_FRAMING,
}

# The one concrete ask each letter must close with, per recipient.
_ASK_BY_RECIPIENT_TYPE = {
    RECIPIENT_DOI: (
        "an investigation of the plan's handling of this claim and "
        "confirmation of whether external (independent) medical review is "
        "available"
    ),
    RECIPIENT_MEDICAL_DIRECTOR: (
        "a peer-to-peer review with the treating clinician and a personal "
        "re-examination of the denial"
    ),
    RECIPIENT_DOL_EBSA: (
        "an EBSA review of the plan's compliance and an inquiry if appropriate"
    ),
}
_DEFAULT_ASK = "a review of the denial"

# Human-readable roles; the internal codes ("doi", "dol_ebsa") mean nothing
# to the model.
_ROLE_LABEL_BY_RECIPIENT_TYPE = {
    RECIPIENT_DOI: "state insurance regulator (Department of Insurance)",
    RECIPIENT_MEDICAL_DIRECTOR: "the health plan's medical director",
    RECIPIENT_DOL_EBSA: (
        "U.S. Department of Labor, Employee Benefits Security Administration"
    ),
}

# How much of the denial letter to quote. Cut at a sentence/paragraph
# boundary so the excerpt reads as a coherent document, not a torn page.
DENIAL_EXCERPT_MAX_CHARS = 4000


def _letter_is_from_professional(denial: Any, professional: Optional[bool]) -> bool:
    if professional is not None:
        return professional
    return bool(getattr(denial, "professional_to_finish", False))


def make_regulator_letter_prompt(
    denial: Any,
    recipient: EscalationRecipient,
    professional: Optional[bool] = None,
) -> str:
    """Build the prompt for a single regulator/executive cover letter.

    ``professional`` picks the letter's voice; when None it follows the
    denial's ``professional_to_finish`` flag (the same signal the appeal
    itself uses), so the cover letter and the appeal it accompanies are
    written by the same person.
    """
    framing = _FRAMING_BY_RECIPIENT_TYPE.get(recipient.recipient_type, _DEFAULT_FRAMING)
    ask = _ASK_BY_RECIPIENT_TYPE.get(recipient.recipient_type, _DEFAULT_ASK)
    role_label = _ROLE_LABEL_BY_RECIPIENT_TYPE.get(
        recipient.recipient_type, recipient.recipient_type
    )
    from_professional = _letter_is_from_professional(denial, professional)

    if from_professional:
        author = (
            "the treating healthcare professional, writing about their patient "
            "in the third person"
        )
        placeholder_note = (
            "Use placeholders like {{Your Name}}, {{Your Practice}}, "
            "{{Your Phone Number}} for the professional's details and "
            "{{FIRST_NAME}} {{LAST_NAME}} and {{SCSID}} for the patient's; "
            "the user will fill those in."
        )
    else:
        author = "the patient, writing in the first person"
        placeholder_note = (
            "Use placeholders like {{FIRST_NAME}} {{LAST_NAME}}, "
            "{{Your Address}}, {{Your Phone Number}}, and {{SCSID}} where "
            "personal information is needed; the user will fill those in."
        )

    insurance_company = (
        denial.insurance_company
        if getattr(denial, "insurance_company", None)
        and denial.insurance_company != "UNKNOWN"
        else "the insurance company"
    )
    procedure = getattr(denial, "procedure", "") or "[procedure]"
    diagnosis = getattr(denial, "diagnosis", "") or "[diagnosis]"
    claim_id = getattr(denial, "claim_id", "") or "[claim id from your denial letter]"
    plan_id = getattr(denial, "plan_id", "") or ""
    state_name = recipient.extra.get("state_name", "")
    external_review_available = recipient.extra.get("external_review_available", False)

    extras = []
    if state_name:
        extras.append(f"- State: {state_name}")
    if recipient.recipient_type == RECIPIENT_DOI and external_review_available:
        extras.append(
            "- Note: external (independent) medical review IS available in this "
            "state — ask the regulator to point the patient to the right "
            "form/process."
        )
    if plan_id:
        extras.append(f"- Plan ID: {plan_id}")

    extras_block = "\n".join(extras)

    qa_context = getattr(denial, "qa_context", "") or ""
    denial_text = getattr(denial, "denial_text", "") or ""
    denial_excerpt = truncate_at_boundary(denial_text, DENIAL_EXCERPT_MAX_CHARS)

    prompt = f"""\
Write a one-page cover letter from {author} to the recipient below,
accompanying a parallel internal appeal that is already being pursued.
The letter should fit on a single page and be ready to print and mail
or fax.

Recipient: {recipient.name}
Recipient role: {role_label}
Recipient address: {recipient.address or "(see denial letter for address)"}

{framing}

Important rules:
- Do NOT fabricate citations, statutes, regulations, study names, or
  PMIDs. Only reference statutes or regulations that appear in this
  prompt.
- {placeholder_note}
- Where a value below is shown in [square brackets] it is unknown: keep
  a bracketed placeholder for the user to fill in rather than inventing
  one.
- Keep the tone professional and restrained. This is being read by a
  regulator or executive, not the insurer.
- End with one concrete ask: {ask}.
- Reference the denial below by its key facts: insurance company,
  procedure, diagnosis, and claim id.

Denial summary:
- Insurance company: {insurance_company}
- Procedure: {procedure}
- Diagnosis: {diagnosis}
- Claim id: {claim_id}
{extras_block}

Patient context (Q&A): {qa_context or "(none provided)"}

Denial letter excerpt:
{denial_excerpt}

Today's date is {datetime.date.today().isoformat()}.

Write the letter now. Output only the letter text — no preamble, no
explanation, no markdown headings.
"""
    return prompt


def _letter_backends(use_external: bool) -> list[RemoteModelLike]:
    """The models to ask for a letter, in order: up to MAX_INTERNAL_ATTEMPTS
    distinct models of ours, then, only with ``use_external``, up to
    MAX_EXTERNAL_ATTEMPTS outside ones.

    get_chat_backends is the chat fan-out's list (the lead twice, the outside
    models, then our others), so taking its first three asked the lead twice
    and one outside model and never reached our other models: a dead outside
    model (out of credit, retired) then failed the letter while a healthy
    model of ours sat further down the list.
    """
    outside_ids = {id(m) for m in ml_router.external_models_by_cost}
    outside_ids.update(id(m) for m in ml_router.chat_outside_models_by_name.values())
    seen: set[int] = set()
    internal: list[RemoteModelLike] = []
    external: list[RemoteModelLike] = []
    for model in ml_router.get_chat_backends(use_external=use_external):
        if id(model) in seen:
            continue
        seen.add(id(model))
        if id(model) in outside_ids or getattr(model, "external", False) is True:
            external.append(model)
        else:
            internal.append(model)
    chosen = internal[:MAX_INTERNAL_ATTEMPTS]
    if use_external:
        chosen += external[:MAX_EXTERNAL_ATTEMPTS]
    return chosen


async def generate_regulator_letter(
    denial: Any,
    recipient: EscalationRecipient,
    use_external: bool = False,
    professional: Optional[bool] = None,
) -> Optional[str]:
    """
    Generate a single regulator/executive cover letter for the denial.

    ``professional`` picks the letter's voice; when None it follows the
    denial's ``professional_to_finish`` flag, as in
    ``make_regulator_letter_prompt``.

    Returns the letter text, or None if no model is available or
    generation failed.
    """
    professional = _letter_is_from_professional(denial, professional)
    prompt = make_regulator_letter_prompt(denial, recipient, professional=professional)
    models = _letter_backends(use_external)
    if not models:
        logger.warning("No chat backends available for regulator letter generation")
        return None

    last_error: Optional[Exception] = None
    ends_at = time.monotonic() + LETTER_BUDGET_SECONDS
    for left_to_ask, model in zip(range(len(models), 0, -1), models):
        share = (ends_at - time.monotonic()) / left_to_ask
        if share <= 0:
            logger.warning(
                f"Regulator letter for recipient {recipient.recipient_type} "
                f"used its {LETTER_BUDGET_SECONDS:.0f}s budget; giving up"
            )
            return None
        try:
            with attempt_deadline(share):
                text: Optional[str] = await asyncio.wait_for(
                    model.generate_prior_auth_response(
                        prompt,
                        system_prompt=REGULATOR_LETTER_SYSTEM_PROMPT,
                        prof_pov=professional,
                    ),
                    timeout=share + LETTER_GRACE_SECONDS,
                )
            if text and len(text.strip()) > 50:
                return text
        except Exception as e:
            last_error = e
            logger.opt(exception=True).debug(
                f"Regulator letter generation failed on {model}: {e}"
            )
    if last_error is not None:
        logger.warning(
            f"All regulator-letter backends failed for recipient "
            f"{recipient.recipient_type}: {last_error}"
        )
    return None

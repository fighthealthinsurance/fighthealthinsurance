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
these are short professional letters (rather than long appeal letters
that benefit from parallel multi-temperature exploration).
"""

import datetime
from typing import Any, Optional

from loguru import logger

from fighthealthinsurance.escalation_addresses import (
    RECIPIENT_DOI,
    RECIPIENT_DOL_EBSA,
    RECIPIENT_MEDICAL_DIRECTOR,
    EscalationRecipient,
)
from fighthealthinsurance.ml.ml_router import ml_router

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
    "self-funded employer plan. Reference the ERISA claims-and-appeals "
    "regulations at 29 C.F.R. § 2560.503-1, summarize the denial and "
    "the specific provisions that may have been violated (e.g. failure "
    "to provide the specific reason for denial, failure to identify the "
    "internal rule or guideline relied on, or failure to meet timing "
    "requirements). Request that EBSA review the plan's compliance and "
    "open an inquiry if appropriate. Tone: factual, formal, restrained."
)

_FRAMING_BY_RECIPIENT_TYPE = {
    RECIPIENT_DOI: _DOI_FRAMING,
    RECIPIENT_MEDICAL_DIRECTOR: _MEDICAL_DIRECTOR_FRAMING,
    RECIPIENT_DOL_EBSA: _DOL_EBSA_FRAMING,
}


def make_regulator_letter_prompt(
    denial: Any,
    recipient: EscalationRecipient,
) -> str:
    """Build the prompt for a single regulator/executive cover letter."""
    framing = _FRAMING_BY_RECIPIENT_TYPE.get(
        recipient.recipient_type,
        "Frame this letter as a respectful request for the recipient to "
        "review the denial.",
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
        extras.append(f"State: {state_name}")
    if recipient.recipient_type == RECIPIENT_DOI and external_review_available:
        extras.append(
            "Note: external (independent) medical review IS available in this state — "
            "ask the regulator to point the patient to the right form/process."
        )
    if plan_id:
        extras.append(f"Plan ID: {plan_id}")

    extras_block = "\n".join(extras)

    qa_context = getattr(denial, "qa_context", "") or ""
    denial_text = getattr(denial, "denial_text", "") or ""

    prompt = f"""\
Write a one-page cover letter from a patient (or, if marked, the
treating professional) to the following recipient, accompanying a
parallel internal appeal that is already being pursued. The letter
should fit on a single page and be ready to print and mail or fax.

Recipient: {recipient.name}
Recipient role: {recipient.recipient_type}
Recipient address: {recipient.address or "(see denial letter for address)"}

{framing}

Important rules:
- Do NOT fabricate citations, statutes, regulations, study names, or
  PMIDs. Only reference statutes you are explicitly given (e.g. ERISA
  § 503 / 29 C.F.R. § 2560.503-1 above for the EBSA letter).
- Use placeholders like {{{{FIRST_NAME}}}} {{{{LAST_NAME}}}}, {{{{Your Address}}}},
  {{{{Your Phone Number}}}}, and {{{{SCSID}}}} where personal information is
  needed; the user will fill those in.
- Keep tone professional and restrained. This is being read by a
  regulator or executive, not the insurer.
- End with a concrete ask (investigation, peer-to-peer, EBSA inquiry).
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
{denial_text[:4000]}

Today's date is {datetime.date.today().isoformat()}.

Write the letter now. Output only the letter text — no preamble, no
explanation, no markdown headings.
"""
    return prompt


async def generate_regulator_letter(
    denial: Any,
    recipient: EscalationRecipient,
    use_external: bool = False,
) -> Optional[str]:
    """
    Generate a single regulator/executive cover letter for the denial.

    Returns the letter text, or None if no model is available or
    generation failed.
    """
    prompt = make_regulator_letter_prompt(denial, recipient)
    models = ml_router.get_chat_backends(use_external=use_external)
    if not models:
        logger.warning("No chat backends available for regulator letter generation")
        return None

    last_error: Optional[Exception] = None
    for model in models[:3]:
        try:
            text: Optional[str] = await model.generate_prior_auth_response(prompt)
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

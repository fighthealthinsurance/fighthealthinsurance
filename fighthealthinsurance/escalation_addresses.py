"""
Address book for the escalation packet feature.

Given a Denial, this module computes the list of regulator / executive
recipients the user could escalate to in parallel with their appeal:

- The state Department of Insurance / insurance commissioner — pulled
  from `state_help.py` using the denial's `your_state` abbreviation.
- The plan's medical director — a generic placeholder addressed to the
  insurer's medical director in care of the insurer (the user fills in
  the mailing address from the denial letter).
- DOL EBSA — for plans likely to be ERISA-covered (self-funded employer
  plans, including those administered by a TPA).

The model `Regulator` already exists in the database for matching denial
text; this module is the complementary "where do we send a letter"
address book that combines per-state DOI data and national resources.
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from fighthealthinsurance.models import RegulatorEscalation
from fighthealthinsurance.regulator_routing import letter_names_regulator
from fighthealthinsurance.state_help import (
    StateHelp,
    get_state_help_by_abbreviation,
)

RECIPIENT_DOI = RegulatorEscalation.RECIPIENT_DOI
RECIPIENT_MEDICAL_DIRECTOR = RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR
RECIPIENT_DOL_EBSA = RegulatorEscalation.RECIPIENT_DOL_EBSA

# National address for the DOL EBSA — the federal agency that enforces
# ERISA for self-funded employer health plans. Patients can request
# enforcement assistance from EBSA when a fiduciary appears to have
# violated ERISA's claims and appeals procedures (29 C.F.R. § 2560.503-1).
DOL_EBSA_NAME = "U.S. Department of Labor, Employee Benefits Security Administration"
DOL_EBSA_ADDRESS = (
    "Employee Benefits Security Administration\n"
    "U.S. Department of Labor\n"
    "200 Constitution Ave NW\n"
    "Washington, DC 20210"
)
DOL_EBSA_PHONE = "1-866-444-3272"
DOL_EBSA_URL = "https://www.dol.gov/agencies/ebsa"


def sanitize_http_url(value: Optional[str]) -> str:
    """Return *value* only if it is an http(s) URL, else an empty string.

    Recipient URLs end up in ``<a href>`` attributes (server templates and
    the escalation packet's client-side rendering), so unexpected schemes
    like ``javascript:`` must never propagate into a clickable link.
    """
    url = (value or "").strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""


@dataclass
class EscalationRecipient:
    """A single recipient on an escalation packet."""

    recipient_type: str  # one of RegulatorEscalation.RECIPIENT_*
    name: str
    address: str = ""
    phone: str = ""
    url: str = ""
    # Plain English description shown to the user describing why we suggest
    # this recipient — used in the UI checklist and as light prompt context.
    rationale: str = ""
    # Free-form metadata the prompt builder can use (e.g. the state name).
    extra: dict = field(default_factory=dict)


def _doi_recipient(state: StateHelp) -> Optional[EscalationRecipient]:
    """Build a DOI recipient from a StateHelp object, if it has one."""
    dept = state.insurance_department
    if not dept or not dept.name:
        return None
    rationale = (
        f"State insurance regulators investigate complaints against "
        f"insurers and can require the insurer to respond. "
        f"{dept.name} also oversees the external review process in "
        f"{state.name}."
    )
    return EscalationRecipient(
        recipient_type=RECIPIENT_DOI,
        name=dept.name,
        address=dept.complaint_url or dept.url or "",
        phone=dept.consumer_line or dept.phone or "",
        url=sanitize_http_url(dept.url),
        rationale=rationale,
        extra={
            "state_name": state.name,
            "state_abbreviation": state.abbreviation,
            "external_review_available": bool(
                state.external_review and state.external_review.available
            ),
            "external_review_url": (
                state.external_review.info_url if state.external_review else None
            ),
        },
    )


def _medical_director_recipient(
    insurance_company: Optional[str],
    insurance_company_obj: Any = None,
) -> EscalationRecipient:
    """Build a 'Medical Director' recipient for the plan.

    When the denial matched a known ``InsuranceCompany`` row we use its
    appeal-routing info (mailing address, member services / appeals phone,
    website) so the user gets a real number to call instead of a blank.
    """
    company = (
        insurance_company
        if insurance_company and insurance_company != "UNKNOWN"
        else "Your Health Plan"
    )
    # The plan's address isn't always known; the letter renders with an
    # obvious bracketed placeholder for the user to fill from the denial
    # letter when we don't have it on file.
    address = "[Insurance company mailing address from your denial letter]"
    phone = ""
    url = ""
    if insurance_company_obj is not None:
        known_address = getattr(insurance_company_obj, "appeal_address", "") or ""
        if known_address.strip():
            address = f"Medical Director\n{known_address.strip()}"
        phone = getattr(insurance_company_obj, "appeal_phone_number", "") or ""
        url = sanitize_http_url(
            getattr(insurance_company_obj, "member_services_url", "")
            or getattr(insurance_company_obj, "website", "")
        )
    return EscalationRecipient(
        recipient_type=RECIPIENT_MEDICAL_DIRECTOR,
        name=f"Medical Director, {company}",
        address=address,
        phone=phone,
        url=url,
        rationale=(
            "The plan's medical director is the physician responsible for "
            "coverage decisions. A direct, peer-to-peer-style letter to the "
            "medical director can prompt a faster clinical re-review."
        ),
        extra={"insurance_company": company},
    )


def _dol_ebsa_recipient() -> EscalationRecipient:
    """Build the DOL EBSA recipient for ERISA-covered plans."""
    return EscalationRecipient(
        recipient_type=RECIPIENT_DOL_EBSA,
        name=DOL_EBSA_NAME,
        address=DOL_EBSA_ADDRESS,
        phone=DOL_EBSA_PHONE,
        url=DOL_EBSA_URL,
        rationale=(
            "ERISA self-funded employer plans are regulated federally by "
            "DOL EBSA. EBSA investigates fiduciary breaches and violations "
            "of the ERISA claims-and-appeals procedures (29 C.F.R. "
            "§ 2560.503-1)."
        ),
    )


_ERISA_PLAN_SOURCE_KEYWORDS = ("erisa", "self-funded", "self funded")


def _is_erisa_likely(denial: Any) -> bool:
    """Heuristic: is this denial likely to be from an ERISA plan?

    True if any of: the denial is linked to the ERISA Regulator row, the
    structured insurance company is flagged as a TPA, or the plan source
    M2M contains an ERISA-tagged source.
    """
    regulator = getattr(denial, "regulator", None)
    if regulator is not None and (regulator.alt_name or "").upper() == "ERISA":
        return True

    ic = getattr(denial, "insurance_company_obj", None)
    if ic is not None and getattr(ic, "is_tpa", False):
        return True

    plan_source_manager = getattr(denial, "plan_source", None)
    if plan_source_manager is None or not hasattr(plan_source_manager, "all"):
        return False
    for source in plan_source_manager.all():
        label = (getattr(source, "name", "") or "").lower()
        if any(k in label for k in _ERISA_PLAN_SOURCE_KEYWORDS):
            return True
    return False


def get_recipients_for_denial(denial: Any) -> List[EscalationRecipient]:
    """
    Build the list of escalation recipients for a denial.

    Always returns at least the medical director recipient. Adds the DOI
    recipient if the denial has a usable state, and the DOL EBSA recipient
    if the plan looks ERISA-covered.
    """
    recipients: List[EscalationRecipient] = []

    state_abbr = (
        getattr(denial, "your_state", None) or getattr(denial, "state", None) or ""
    )
    if state_abbr:
        state = get_state_help_by_abbreviation(state_abbr)
        if state is not None:
            doi = _doi_recipient(state)
            if doi is not None:
                recipients.append(doi)

    insurance_company = getattr(denial, "insurance_company", None)
    ic_obj = getattr(denial, "insurance_company_obj", None)
    if not insurance_company and ic_obj is not None:
        insurance_company = getattr(ic_obj, "name", None)
    recipients.append(_medical_director_recipient(insurance_company, ic_obj))

    if _is_erisa_likely(denial):
        recipients.append(_dol_ebsa_recipient())

    return recipients


# Wording in a denial letter that says an appeal has already been decided:
# the insurer upheld itself, calls this its final word, or names a second
# level. Deliberately not "external review" or "independent review": a
# first-level denial carries those as a required notice of rights, so they
# say nothing about where this person is in the process.
_APPEAL_ALREADY_DECIDED_RE = re.compile(
    r"("
    r"\b(we|has|have|is|are|was|were|been)\s+(also\s+)?uph[eo]ld\b"
    r"|\buph[eo]ld\s+(our|the|its|this|that)\b"
    # "Our original decision" alone is in every first denial's rights notice
    # ("you have the right to appeal our initial determination"), so it
    # counts only with a verb saying it was kept.
    r"|\b(maintain|affirm|reaffirm|uph[eo]ld|stand\s+by)(s|ed|ing)?\s+"
    r"(our|the|its)\s+(original|initial|previous|prior|earlier)\s+"
    r"(decision|determination|denial)\b"
    r"|\bour (original|initial|previous|prior|earlier) "
    r"(decision|determination|denial)\b[^.;:!?\n]{0,60}?"
    r"\b(stands|remains|(is|was|has\s+been|have\s+been)\s+"
    r"(upheld|maintained|affirmed|reaffirmed|unchanged))\b"
    r"|\bfinal (adverse|internal)? ?(benefit )?(determination|decision)\b"
    r"|\bexhausted\b.{0,40}\bappeal"
    r"|\b(second|2nd|level[- ](2|two|ii))[- ]level\b"
    r"|\blevel[- ](2|two|ii)\b.{0,20}\bappeal"
    r")",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)
# The same words in a clause about what could happen, not what did: the
# rights notice on a first denial says "If our decision is upheld you may
# request an external review", and "once you have exhausted your internal
# appeals". Any of these in the clause before the match disqualifies it.
_CONDITIONAL_CLAUSE_RE = re.compile(
    r"\b(if|should|unless|in the event|may|might|would|will|could"
    r"|once you|when you|after you|until you)\b",
    re.IGNORECASE,
)
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n]")
# A negation just before the match: "this is not a final decision".
_NEGATED_RE = re.compile(r"\b(not|never|no)\s+(a|an|the|our|yet)?\s*$", re.IGNORECASE)
# How far back a clause is read for a conditional. Longer than any clause
# a letter is likely to hold, short enough that the sentence before cannot
# lend its "if" to this one when the break between them is unpunctuated.
_CLAUSE_LOOKBACK_CHARS = 200


def _appeal_already_decided(denial_text: str) -> bool:
    """Whether the letter says an appeal on this denial has been decided."""
    for match in _APPEAL_ALREADY_DECIDED_RE.finditer(denial_text):
        before = denial_text[
            max(0, match.start() - _CLAUSE_LOOKBACK_CHARS) : match.start()
        ]
        clause = _CLAUSE_BREAK_RE.split(before)[-1]
        if _CONDITIONAL_CLAUSE_RE.search(clause) or _NEGATED_RE.search(clause):
            continue
        return True
    return False


# Denial types where the denial itself is the kind of thing a regulator
# looks into, by name (fixture rows are matched by name, never by pk, as
# ml/denial_triage.py does). "Limit for filing expired" is left out: that
# is usually the provider's timely-filing problem, not the insurer's.
_REGULATOR_POLICED_DENIAL_TYPES: dict[str, str] = {
    "Pre-Existing Condition": (
        "Your denial cites a pre-existing condition. Health plans generally "
        "may not deny coverage on that basis, and regulators look into "
        "denials that do."
    ),
    "Preventive Care": (
        "Your denial is for preventive care, which most plans are required "
        "to cover. Regulators look into denials of required benefits."
    ),
    "In-Network Treated as Out of Network": (
        "An in-network provider was billed as out of network. Network "
        "adequacy and out-of-network billing are things regulators police."
    ),
    "Provider Bill (possible balance billing)": (
        "This looks like a balance bill. Surprise and balance billing is "
        "regulated, and regulators take complaints about it."
    ),
}


def regulator_escalation_reasons(denial: Any) -> List[str]:
    """Plain-English reasons a regulator should hear about this denial.

    Empty when nothing in the denial points that way; the appeal pages then
    show the regulator-letter callout at its normal size. Each reason is a
    sentence for the person, in the same restrained voice as the recipient
    rationales above. Rule-based and read from the row as it is: no model
    call, no extra lookups beyond the denial's own relations.
    """
    reasons: List[str] = []

    if _appeal_already_decided(getattr(denial, "denial_text", "") or ""):
        reasons.append(
            "Your letter reads like an appeal that has already been denied "
            "once. That is when a regulator complaint helps most."
        )

    regulator = getattr(denial, "regulator", None)
    regulator_name = (getattr(regulator, "name", "") or "").strip()
    # Only a regulator the letter names: one the model inferred (no regex
    # match) is a guess about the plan, not a sign from the letter.
    if regulator_name and letter_names_regulator(denial):
        reasons.append(
            f"Your denial letter names {regulator_name}, a regulator that "
            "oversees this kind of plan and takes consumer complaints."
        )

    if _is_erisa_likely(denial):
        reasons.append(
            "This looks like an employer self-funded (ERISA) plan, so the U.S. "
            "Department of Labor also oversees how the appeal is handled."
        )

    if getattr(denial, "urgent", False):
        reasons.append(
            "You marked this care as urgent. A regulator complaint can go out "
            "with your appeal instead of waiting on it."
        )

    denial_type_manager = getattr(denial, "denial_type", None)
    if denial_type_manager is not None and hasattr(denial_type_manager, "all"):
        for denial_type in denial_type_manager.all():
            reason = _REGULATOR_POLICED_DENIAL_TYPES.get(
                getattr(denial_type, "name", "") or ""
            )
            if reason is not None and reason not in reasons:
                reasons.append(reason)

    return reasons

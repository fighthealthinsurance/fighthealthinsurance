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

from dataclasses import dataclass, field
from typing import Any, List, Optional

from fighthealthinsurance.state_help import (
    StateHelp,
    get_state_help_by_abbreviation,
)

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
        f"State insurance regulators investigate complaints against insurers "
        f"and can require {dept.name} to respond. They also oversee the "
        f"external review process in {state.name}."
    )
    return EscalationRecipient(
        recipient_type="doi",
        name=dept.name,
        address=dept.complaint_url or dept.url or "",
        phone=dept.consumer_line or dept.phone or "",
        url=dept.url or "",
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
) -> EscalationRecipient:
    """Build a generic 'Medical Director' recipient for the plan."""
    company = (
        insurance_company
        if insurance_company and insurance_company != "UNKNOWN"
        else "Your Health Plan"
    )
    return EscalationRecipient(
        recipient_type="medical_director",
        name=f"Medical Director, {company}",
        # We don't know the plan's mailing address; the user fills that in
        # from the denial letter. We leave a placeholder block so the cover
        # letter renders with an obvious blank to fill.
        address="[Insurance company mailing address from your denial letter]",
        phone="",
        url="",
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
        recipient_type="dol_ebsa",
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


def _is_erisa_likely(denial: Any) -> bool:
    """Heuristic: is this denial likely to be from an ERISA plan?

    We use any of:
    - The denial is linked to the ERISA Regulator row.
    - The structured insurance company is flagged as a TPA.
    - The plan source ManyToMany contains an ERISA-tagged source.
    """
    try:
        if denial.regulator and (denial.regulator.alt_name or "").upper() == "ERISA":
            return True
    except Exception:
        pass
    try:
        ic = getattr(denial, "insurance_company_obj", None)
        if ic is not None and getattr(ic, "is_tpa", False):
            return True
    except Exception:
        pass
    try:
        for source in denial.plan_source.all():
            label = (getattr(source, "name", "") or "").lower()
            if "erisa" in label or "self-funded" in label or "self funded" in label:
                return True
    except Exception:
        pass
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
    if not insurance_company:
        ic_obj = getattr(denial, "insurance_company_obj", None)
        if ic_obj is not None:
            insurance_company = getattr(ic_obj, "name", None)
    recipients.append(_medical_director_recipient(insurance_company))

    if _is_erisa_likely(denial):
        recipients.append(_dol_ebsa_recipient())

    return recipients

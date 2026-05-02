import datetime
from dataclasses import asdict, dataclass
from typing import Any, Optional

from fighthealthinsurance.models import Denial, FollowUpSched

CONFIDENCE_LIKELY = "likely_eligible"
CONFIDENCE_POSSIBLE = "possibly_eligible"
CONFIDENCE_NOT = "likely_not_eligible"
CONFIDENCE_UNKNOWN = "unknown_need_human_review"

STATE_CONFIG: dict[str, dict[str, Any]] = {
    "CA": {
        "state": "CA",
        "regulator_name": "California Department of Managed Health Care",
        "external_review_url": "https://www.dmhc.ca.gov/FileaComplaint/IndependentMedicalReviewComplaintForms.aspx",
        "form_url": "https://www.dmhc.ca.gov/FileaComplaint.aspx",
        "phone": "1-888-466-2219",
        "deadline_days_or_months": "6 months",
        "expedited_available": True,
        "notes": "Department of Insurance may apply for some plans.",
        "last_verified_at": "2026-05-02",
    },
    "NY": {
        "state": "NY",
        "regulator_name": "New York Department of Financial Services",
        "external_review_url": "https://www.dfs.ny.gov/consumers/health_insurance/external_appeal",
        "form_url": "https://www.dfs.ny.gov/consumers/health_insurance/external_appeal",
        "phone": "1-800-400-8882",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Check plan notices for exact filing window.",
        "last_verified_at": "2026-05-02",
    },
    "TX": {
        "state": "TX",
        "regulator_name": "Texas Department of Insurance",
        "external_review_url": "https://www.tdi.texas.gov/consumer/external-review-program.html",
        "form_url": "https://www.tdi.texas.gov/consumer/cp/externalreviewrequestform.pdf",
        "phone": "1-800-252-3439",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Expedited review available for urgent situations.",
        "last_verified_at": "2026-05-02",
    },
    "FL": {
        "state": "FL",
        "regulator_name": "Florida Office of Insurance Regulation",
        "external_review_url": "https://www.floir.com/",
        "form_url": "https://www.myfloridacfo.com/division/consumers/needourhelp.htm",
        "phone": "1-877-693-5236",
        "deadline_days_or_months": "120 days",
        "expedited_available": True,
        "notes": "Program administration can vary by plan segment.",
        "last_verified_at": "2026-05-02",
    },
    "WA": {
        "state": "WA",
        "regulator_name": "Washington Office of the Insurance Commissioner",
        "external_review_url": "https://www.insurance.wa.gov/external-review",
        "form_url": "https://www.insurance.wa.gov/appeal-health-plan-decision",
        "phone": "1-800-562-6900",
        "deadline_days_or_months": "180 days",
        "expedited_available": True,
        "notes": "Some plans route through federal process.",
        "last_verified_at": "2026-05-02",
    },
    "IL": {
        "state": "IL",
        "regulator_name": "Illinois Department of Insurance",
        "external_review_url": "https://idoi.illinois.gov/consumers/file-a-complaint.html",
        "form_url": "https://idoi.illinois.gov/consumers/file-a-complaint.html",
        "phone": "1-866-445-5364",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "External review eligibility depends on product type.",
        "last_verified_at": "2026-05-02",
    },
    "AZ": {
        "state": "AZ",
        "regulator_name": "Arizona Department of Insurance and Financial Institutions",
        "external_review_url": "https://difi.az.gov/",
        "form_url": "https://difi.az.gov/consumer/health-insurance",
        "phone": "1-800-325-2548",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Check plan denial for exact filing deadline.",
        "last_verified_at": "2026-05-02",
    },
    "CO": {
        "state": "CO",
        "regulator_name": "Colorado Division of Insurance",
        "external_review_url": "https://doi.colorado.gov/for-consumers/file-a-complaint",
        "form_url": "https://doi.colorado.gov/for-consumers/file-a-complaint",
        "phone": "1-800-930-3745",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Independent review pathway can vary by policy.",
        "last_verified_at": "2026-05-02",
    },
    "GA": {
        "state": "GA",
        "regulator_name": "Georgia Office of Commissioner of Insurance",
        "external_review_url": "https://oci.georgia.gov/",
        "form_url": "https://oci.georgia.gov/consumer-services/file-complaint",
        "phone": "1-800-656-2298",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Use complaint + external review instructions from denial packet.",
        "last_verified_at": "2026-05-02",
    },
    "MA": {
        "state": "MA",
        "regulator_name": "Massachusetts Division of Insurance",
        "external_review_url": "https://www.mass.gov/orgs/division-of-insurance",
        "form_url": "https://www.mass.gov/how-to/file-a-health-insurance-complaint",
        "phone": "1-877-563-4467",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Some plans may route through OPP process.",
        "last_verified_at": "2026-05-02",
    },
    "MI": {
        "state": "MI",
        "regulator_name": "Michigan Department of Insurance and Financial Services",
        "external_review_url": "https://www.michigan.gov/difs/consumers/insurance/health-insurance",
        "form_url": "https://www.michigan.gov/difs/consumers/insurance/health-insurance/file-a-complaint",
        "phone": "1-877-999-6442",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Keep final internal appeal determination with filing.",
        "last_verified_at": "2026-05-02",
    },
    "MN": {
        "state": "MN",
        "regulator_name": "Minnesota Department of Commerce",
        "external_review_url": "https://mn.gov/commerce/consumers/your-insurance/health/",
        "form_url": "https://mn.gov/commerce/consumers/file-a-complaint/",
        "phone": "1-800-657-3602",
        "deadline_days_or_months": "6 months",
        "expedited_available": True,
        "notes": "Some HMOs may use separate state agency process.",
        "last_verified_at": "2026-05-02",
    },
    "NC": {
        "state": "NC",
        "regulator_name": "North Carolina Department of Insurance",
        "external_review_url": "https://www.ncdoi.gov/consumers/health-insurance",
        "form_url": "https://www.ncdoi.gov/consumers/assistance-or-file-complaint",
        "phone": "1-855-408-1212",
        "deadline_days_or_months": "120 days",
        "expedited_available": True,
        "notes": "Independent review rights depend on denial category.",
        "last_verified_at": "2026-05-02",
    },
    "NJ": {
        "state": "NJ",
        "regulator_name": "New Jersey Department of Banking and Insurance",
        "external_review_url": "https://www.nj.gov/dobi/division_consumers/insurance/managedcare.html",
        "form_url": "https://www.nj.gov/dobi/division_consumers/insurance/managedcare.html",
        "phone": "1-888-393-1062",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "External appeal route differs for some public coverage.",
        "last_verified_at": "2026-05-02",
    },
    "OH": {
        "state": "OH",
        "regulator_name": "Ohio Department of Insurance",
        "external_review_url": "https://insurance.ohio.gov/consumers/health",
        "form_url": "https://insurance.ohio.gov/consumers/file-a-complaint",
        "phone": "1-800-686-1526",
        "deadline_days_or_months": "180 days",
        "expedited_available": True,
        "notes": "Review denial notice for issuer-specific instructions.",
        "last_verified_at": "2026-05-02",
    },
    "VA": {
        "state": "VA",
        "regulator_name": "Virginia State Corporation Commission Bureau of Insurance",
        "external_review_url": "https://scc.virginia.gov/pages/Insurance",
        "form_url": "https://scc.virginia.gov/pages/File-an-Insurance-Complaint",
        "phone": "1-877-310-6560",
        "deadline_days_or_months": "120 days",
        "expedited_available": True,
        "notes": "Maintain documentation of urgent clinical need if applicable.",
        "last_verified_at": "2026-05-02",
    },
    "PA": {
        "state": "PA",
        "regulator_name": "Pennsylvania Insurance Department",
        "external_review_url": "https://www.insurance.pa.gov/Consumers/Pages/External-Review.aspx",
        "form_url": "https://www.insurance.pa.gov/Consumers/Pages/External-Review.aspx",
        "phone": "1-877-881-6388",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Keep denial and plan correspondence in packet.",
        "last_verified_at": "2026-05-02",
    },
}

FEDERAL_FALLBACK: dict[str, Any] = {
    "state": "FEDERAL",
    "regulator_name": "CMS / Federal External Review",
    "external_review_url": "https://www.cms.gov/CCIIO/Programs-and-Initiatives/Consumer-Support-and-Information/External-Appeals",
    "form_url": "https://www.cms.gov/CCIIO/Programs-and-Initiatives/Consumer-Support-and-Information/External-Appeals",
    "phone": "1-877-267-2323",
    "deadline_days_or_months": "4 months (typical)",
    "expedited_available": True,
    "notes": "Federal fallback when state route is unclear.",
    "last_verified_at": "2026-05-02",
}

EXTERNAL_REVIEW_WARNINGS = [
    "This is not legal advice.",
    "External review rules vary by plan type and state.",
    "If urgent, contact your regulator/plan immediately and consider expedited review.",
]


@dataclass
class ExternalReviewEligibility:
    confidence: str
    rationale: list[str]
    routing: str


def _parse_date(value: Any) -> Optional[datetime.date]:
    if isinstance(value, datetime.date):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def detect_external_review_eligibility(
    *,
    state: Optional[str],
    plan_type: Optional[str],
    denial_type: Optional[str],
    denial_date: Optional[datetime.date],
    appeal_denial_date: Optional[datetime.date],
    urgent: bool,
) -> ExternalReviewEligibility:
    del denial_date
    rationale: list[str] = []
    plan = (plan_type or "unknown").lower().strip()
    denial = (denial_type or "unknown").lower().strip()

    if appeal_denial_date is None:
        rationale.append("Internal appeal denial date missing; timeline uncertain.")

    if plan in {"medicare advantage", "medicare"}:
        rationale.append(
            "Medicare Advantage usually follows Medicare appeals, not ordinary commercial state IRO."
        )
        return ExternalReviewEligibility(CONFIDENCE_UNKNOWN, rationale, "medicare_path")

    if "medicaid" in plan:
        rationale.append(
            "Medicaid reviews are often state Medicaid fair-hearing driven."
        )
        return ExternalReviewEligibility(
            CONFIDENCE_POSSIBLE, rationale, "medicaid_path"
        )

    if "short-term" in plan or "limited" in plan or "health sharing" in plan:
        rationale.append(
            "Short-term/limited benefit/health sharing plans often have limited external review rights."
        )
        return ExternalReviewEligibility(
            CONFIDENCE_NOT, rationale, "limited_benefit_path"
        )

    if "erisa" in plan or "self-funded" in plan:
        rationale.append(
            "ERISA/self-funded plans may use federal process or plan-specified IRO."
        )
        confidence = CONFIDENCE_POSSIBLE if appeal_denial_date else CONFIDENCE_UNKNOWN
        return ExternalReviewEligibility(confidence, rationale, "erisa_federal_path")

    likely_denials = {
        "medical necessity",
        "experimental/investigational",
        "surprise billing",
        "out-of-network",
    }
    maybe_denials = {"administrative", "eligibility", "coding/billing"}
    if denial in likely_denials:
        rationale.append(
            "Denial type is commonly eligible for independent external review."
        )
        confidence = CONFIDENCE_LIKELY if appeal_denial_date else CONFIDENCE_POSSIBLE
    elif denial in maybe_denials:
        rationale.append(
            "Administrative/eligibility/coding denials may be excluded from medical IRO review."
        )
        confidence = CONFIDENCE_POSSIBLE
    else:
        rationale.append("Denial type unclear.")
        confidence = CONFIDENCE_UNKNOWN

    if urgent:
        rationale.append("Urgent medical risk may qualify for expedited handling.")
    if not state:
        rationale.append("State missing; cannot confidently select regulator.")

    return ExternalReviewEligibility(confidence, rationale, "state_or_federal_path")


def get_state_config(state: Optional[str]) -> dict[str, Any]:
    if not state:
        result = dict(FEDERAL_FALLBACK)
        result["notes"] = f'{result["notes"]} State not provided.'
        return result

    state_up = state.upper().strip()
    if state_up in STATE_CONFIG:
        return dict(STATE_CONFIG[state_up])

    result = dict(FEDERAL_FALLBACK)
    result.update(
        {
            "state": state_up,
            "not_configured": True,
            "notes": f"State {state_up} not yet configured. Use state DOI plus federal instructions.",
        }
    )
    return result


def generate_external_review_packet(
    denial: Denial, payload: dict[str, Any]
) -> dict[str, Any]:
    appeal_denial_date = _parse_date(payload.get("appeal_denial_date"))
    state = payload.get("state") or denial.state
    plan_type = payload.get("plan_type")

    eligibility = detect_external_review_eligibility(
        state=state,
        plan_type=plan_type,
        denial_type=payload.get("denial_type") or denial.denial_type_text,
        denial_date=denial.denial_date,
        appeal_denial_date=appeal_denial_date,
        urgent=bool(payload.get("urgent", False)),
    )

    checklist = [
        "Final internal appeal denial letter",
        "Plan booklet / EOC language",
        "Provider letter documenting medical necessity",
        "Relevant medical records and test results",
        "Completed external review form",
    ]

    return {
        "eligibility": asdict(eligibility),
        "regulator": get_state_config(state),
        "warnings": EXTERNAL_REVIEW_WARNINGS,
        "checklist": checklist,
        "cover_letter": (
            f"Request for external review for denial {denial.denial_id}. "
            f"Plan type: {plan_type or 'unknown'}."
        ),
    }


def schedule_external_review_followups(
    denial: Denial,
    email: str,
    deadline_date: datetime.date,
    today: Optional[datetime.date] = None,
) -> None:
    base_date = today or datetime.date.today()
    reminders = [
        deadline_date - datetime.timedelta(days=7),
        base_date + datetime.timedelta(days=7),
        base_date + datetime.timedelta(days=45),
    ]

    for reminder_date in reminders:
        FollowUpSched.objects.update_or_create(
            denial_id=denial,
            follow_up_type=None,
            follow_up_date=reminder_date,
            defaults={"email": email},
        )

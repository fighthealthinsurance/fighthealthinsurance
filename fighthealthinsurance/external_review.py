import datetime
from dataclasses import asdict, dataclass
from typing import Any, Optional

from fighthealthinsurance.email_utils import is_sendable_email
from fighthealthinsurance.models import Denial, FollowUpSched

CONFIDENCE_LIKELY = "likely_eligible"
CONFIDENCE_POSSIBLE = "possibly_eligible"
CONFIDENCE_NOT = "likely_not_eligible"
CONFIDENCE_UNKNOWN = "unknown_need_human_review"

STATE_CONFIG: dict[str, dict[str, Any]] = {
    "CA": {
        "state": "CA",
        "regulator_name": "California Department of Managed Health Care",
        "external_review_url": "https://www.dmhc.ca.gov/FileaComplaint.aspx#howToApply",
        "form_url": "https://www.dmhc.ca.gov/FileaComplaint/IndependentMedicalReviewComplaintForms.aspx#imrForms",
        "phone": "1-888-466-2219",
        "deadline_days_or_months": "6 months",
        "expedited_available": True,
        "notes": "Department of Insurance may apply for some plans.",
        "last_verified_at": "2026-05-02",
    },
    "NY": {
        "state": "NY",
        "regulator_name": "New York Department of Financial Services",
        "external_review_url": "https://www.dfs.ny.gov/complaints/file_external_appeal",
        "form_url": "https://myportal.dfs.ny.gov/web/externalappeals",
        "phone": "1-800-400-8882",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Check plan notices for exact filing window.",
        "last_verified_at": "2026-05-02",
    },
    "TX": {
        "state": "TX",
        "regulator_name": "Texas Department of Insurance",
        "external_review_url": "https://www.tdi.texas.gov/consumer/complaint-health.html",
        "form_url": "https://www.tdi.texas.gov/consumer/file-health-cmplnt.html",
        "phone": "1-800-252-3439",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Expedited review available for urgent situations.",
        "last_verified_at": "2026-05-02",
    },
    "WA": {
        "state": "WA",
        "regulator_name": "Washington Office of the Insurance Commissioner",
        "external_review_url": "https://www.insurance.wa.gov/sites/default/files/2024-10/appeals-guide.pdf",
        "form_url": "https://www.insurance.wa.gov/insurance-resources/health-insurance/appealing-health-insurance-denial/how-appeal-health-insurance-denial",
        "phone": "1-800-562-6900",
        "deadline_days_or_months": "...",
        "expedited_available": True,
        "notes": "Some plans route through federal process.",
        "last_verified_at": "2026-05-02",
    },
    "IL": {
        "state": "IL",
        "regulator_name": "Illinois Department of Insurance",
        "external_review_url": "https://idoi.illinois.gov/consumers/file-an-external-review.html",
        "form_url": "https://idoi.illinois.gov/consumers/file-an-external-review.html",
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
        "form_url": "https://difi.az.gov/consumers/health-insurance#appeals-forms",
        "phone": "1-800-325-2548",
        "deadline_days_or_months": "4 months",
        "expedited_available": True,
        "notes": "Check plan denial for exact filing deadline.",
        "last_verified_at": "2026-05-02",
    },
    "CO": {
        "state": "CO",
        "regulator_name": "Colorado Division of Insurance",
        "external_review_url": "https://doi.colorado.gov/sites/doi/files/documents/When%20Your%20Health%20Insurance%20Company%20Says%20_No_.pdf",
        "form_url": "https://doi.colorado.gov/sites/doi/files/documents/When%20Your%20Health%20Insurance%20Company%20Says%20_No_.pdf",
        "phone": "1-800-930-3745",
        "deadline_days_or_months": "",
        "expedited_available": True,
        "notes": "Independent review pathway can vary by policy.",
        "last_verified_at": "2026-05-02",
    },
    "GA": {
        "state": "GA",
        "regulator_name": "Georgia Office of Commissioner of Insurance",
        "external_review_url": "https://healthyfuturega.org/wp-content/uploads/2015/12/ET10_AppealsComplaints.pdf",
        "form_url": "https://oci.georgia.gov/consumer-services/file-complaint",
        "phone": "1-800-656-2298",
        "deadline_days_or_months": "6 months",
        "expedited_available": True,
        "notes": "Use complaint + external review instructions from denial packet.",
        "last_verified_at": "2026-05-02",
    },
}

FEDERAL_FALLBACK: dict[str, Any] = {
    "state": "FEDERAL",
    "regulator_name": "CMS / Federal External Review",
    "external_review_url": "https://www.cms.gov/CCIIO/Programs-and-Initiatives/Consumer-Support-and-Information/External-Appeals",
    "form_url": "https://www.cms.gov/CCIIO/Programs-and-Initiatives/Consumer-Support-and-Information/External-Appeals",
    "phone": "1-877-267-2323",
    "deadline_days_or_months": "varies",
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


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


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
        urgent=_parse_bool(payload.get("urgent", False)),
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
    if not is_sendable_email(email):
        return

    base_date = today or datetime.date.today()
    reminders = [
        deadline_date - datetime.timedelta(days=7),
        base_date + datetime.timedelta(days=7),
        base_date + datetime.timedelta(days=45),
    ]

    for reminder_date in reminders:
        if reminder_date < base_date:
            continue
        FollowUpSched.objects.update_or_create(
            denial_id=denial,
            follow_up_type=None,
            follow_up_date=reminder_date,
            defaults={"email": email},
        )

"""Structured, sourced rules for health-insurance appeal deadlines.

This module encodes the *general* regulatory timeframes that apply when a
patient wants to appeal a health-insurance denial. It is intentionally kept
small, well-typed, and heavily commented so the numbers stay maintainable and
auditable against their sources.

IMPORTANT LEGAL FRAMING
-----------------------
These are GENERAL federal baselines. Real deadlines depend on the specific plan
document, the state, and the exact type of claim. Where a figure is fixed by a
federal rule we cite it. Where it genuinely varies (most notably Medicaid, and
any employer plan that is more generous than the federal floor) we say so rather
than inventing a precise number.

Primary sources (see per-rule ``citations`` for the specific provisions):

* ERISA claims-procedure rule -- U.S. Department of Labor,
  29 CFR 2560.503-1. Governs employer-sponsored (self-funded and many insured)
  group health plans.
* ACA internal claims & appeals and external review -- 45 CFR 147.136
  (HHS) / 29 CFR 2590.715-2719 (DOL), implementing PHS Act sec. 2719. Governs
  non-grandfathered individual and group plans, including Marketplace (ACA)
  coverage.
* Medicare Advantage (Part C) organization determinations & reconsiderations --
  42 CFR Part 422, Subpart M. Part D coverage determinations & redeterminations
  -- 42 CFR Part 423, Subpart M. (CMS Parts C & D appeals guidance.)
* Medicaid fair hearings -- 42 CFR 431.220-431.246. Medicaid managed-care
  appeals -- 42 CFR 438.400-438.424.

"Last reviewed" is exposed as ``LAST_REVIEWED`` so the page can show a freshness
date and reviewers know when the citations were last checked.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

# Date the rules below were last reviewed against their cited sources.
LAST_REVIEWED: datetime.date = datetime.date(2026, 7, 10)


# ---------------------------------------------------------------------------
# Input vocabularies
# ---------------------------------------------------------------------------

# Coverage types the calculator understands. Keys are stable identifiers used in
# the form and the rules table; labels are what the patient sees.
COVERAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("commercial_aca", "Commercial / Marketplace (ACA) plan"),
    ("employer_erisa", "Employer / ERISA group health plan"),
    ("medicare_advantage_partd", "Medicare Advantage or Part D"),
    ("medicaid", "Medicaid"),
    ("other", "Other / not sure"),
)

# When the care happens relative to the decision.
TIMING_CHOICES: tuple[tuple[str, str], ...] = (
    ("pre_service", "Before the service (pre-service / prior authorization)"),
    ("post_service", "After the service (a bill or claim was denied)"),
    ("concurrent", "During ongoing care (concurrent)"),
)

_VALID_COVERAGE = {key for key, _ in COVERAGE_CHOICES}
_VALID_TIMING = {key for key, _ in TIMING_CHOICES}


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A single timeframe.

    ``days`` is the number of calendar days when the figure is a fixed federal
    floor and can be turned into a concrete date. When the number genuinely
    varies (plan-specific, state-specific, or an hours-based urgent window that
    is not meaningfully a "date"), ``days`` is ``None`` and ``label`` explains
    the rule in plain language.
    """

    label: str
    citation: str
    days: Optional[int] = None
    varies: bool = False

    def deadline_from(self, start: Optional[datetime.date]) -> Optional[datetime.date]:
        """Concrete deadline date, if this window is a fixed number of days.

        Returns ``None`` when there is no fixed day count or no start date.
        """
        if self.days is None or start is None:
            return None
        return start + datetime.timedelta(days=self.days)


@dataclass(frozen=True)
class CoverageRule:
    """All timeframes for one coverage type."""

    key: str
    name: str
    summary: str
    # Insurer's window to DECIDE an appeal, keyed by a normalized situation.
    # Keys used: "expedited", "pre_service", "post_service", "concurrent".
    decision_windows: dict[str, Window]
    # Patient's window to FILE the internal appeal with the plan.
    internal_appeal_filing: Window
    # Patient's window to request external / independent review (or, for public
    # programs, the next level such as an Independent Review Entity or state
    # fair hearing).
    external_review_filing: Window
    external_review_label: str
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The rules table
# ---------------------------------------------------------------------------
#
# Citations reference the controlling regulation. Hours-based urgent windows are
# expressed as labels (days=None) because "72 hours" is not usefully a calendar
# date for a patient planning around a denial letter.

_COMMERCIAL_ACA = CoverageRule(
    key="commercial_aca",
    name="Commercial / Marketplace (ACA) plan",
    summary=(
        "Non-grandfathered individual, Marketplace, and fully-insured plans "
        "follow the ACA internal-appeal and external-review rules."
    ),
    decision_windows={
        # 45 CFR 147.136(b)(2)(ii) adopts the ERISA timeframes for group plans
        # and sets parallel timeframes for individual plans.
        "expedited": Window(
            label="As soon as your health requires — no later than 72 hours",
            citation="45 CFR 147.136(b)(2)(ii)(A) / (b)(3)(ii)(A)",
        ),
        "pre_service": Window(
            label="Within 30 days for a pre-service appeal",
            citation="45 CFR 147.136(b)(2)(ii) (adopting 29 CFR 2560.503-1(i))",
            days=30,
        ),
        "post_service": Window(
            label="Within 60 days for a post-service appeal",
            citation="45 CFR 147.136(b)(2)(ii) (adopting 29 CFR 2560.503-1(i))",
            days=60,
        ),
        "concurrent": Window(
            label=(
                "Before care is reduced or stopped — urgent concurrent requests "
                "are handled within 72 hours"
            ),
            citation="45 CFR 147.136(b)(2)(ii); 29 CFR 2560.503-1(f)(2)(i)",
        ),
    },
    internal_appeal_filing=Window(
        label="You have at least 180 days from the denial to file an internal appeal",
        citation="45 CFR 147.136(b)(2)(ii)(B) / (b)(3)(ii)(B)",
        days=180,
    ),
    external_review_filing=Window(
        label="You generally have 4 months (about 120 days) after the final internal denial to request external review",
        citation="45 CFR 147.136(d)(2)(i) (standard federal external review)",
        days=120,
    ),
    external_review_label="Independent external review",
    notes=(
        "The 4-month external-review clock starts at the FINAL internal denial, "
        "not the first denial.",
        "Some states run their own external-review process with different "
        "timing — check the appeal-rights notice on your denial.",
    ),
)

_EMPLOYER_ERISA = CoverageRule(
    key="employer_erisa",
    name="Employer / ERISA group health plan",
    summary=(
        "Employer-sponsored group health plans follow the ERISA "
        "claims-procedure rule. Non-grandfathered plans also add the ACA "
        "external-review right."
    ),
    decision_windows={
        "expedited": Window(
            label="As soon as your health requires — no later than 72 hours for urgent care",
            citation="29 CFR 2560.503-1(i)(2)(i) / (f)(2)(i)",
        ),
        "pre_service": Window(
            label="Within 30 days for a pre-service appeal",
            citation="29 CFR 2560.503-1(i)(2)(iii)",
            days=30,
        ),
        "post_service": Window(
            label="Within 60 days for a post-service appeal",
            citation="29 CFR 2560.503-1(i)(2)(iii)(B)",
            days=60,
        ),
        "concurrent": Window(
            label=(
                "A reduction or termination of ongoing care must be decided "
                "before the change takes effect"
            ),
            citation="29 CFR 2560.503-1(f)(2)(ii)",
        ),
    },
    internal_appeal_filing=Window(
        label="You have at least 180 days from the denial to file an internal appeal",
        citation="29 CFR 2560.503-1(h)(3)(i) / (h)(4)",
        days=180,
    ),
    external_review_filing=Window(
        label="For non-grandfathered plans, you generally have 4 months (about 120 days) after the final internal denial to request external review",
        citation="29 CFR 2590.715-2719(d); 45 CFR 147.136(d)(2)(i)",
        days=120,
    ),
    external_review_label="Independent external review (non-grandfathered plans)",
    notes=(
        "Plans may require you to complete one or two levels of internal appeal "
        "before external review — your plan document says which.",
        "Grandfathered plans are not required to offer ACA external review; the "
        "internal-appeal deadlines above still apply.",
    ),
)

_MEDICARE_ADVANTAGE_PARTD = CoverageRule(
    key="medicare_advantage_partd",
    name="Medicare Advantage or Part D",
    summary=(
        "Medicare Advantage (Part C) uses organization determinations and "
        "plan-level reconsiderations; Part D uses coverage determinations and "
        "redeterminations. Deadlines are tighter than commercial plans."
    ),
    decision_windows={
        # Part C reconsideration: 42 CFR 422.590. Part D redetermination:
        # 42 CFR 423.590. Expedited: 42 CFR 422.590(d) / 423.590(d).
        "expedited": Window(
            label="Expedited decision within 72 hours",
            citation="42 CFR 422.590(d)(1) (Part C) / 42 CFR 423.590(d)(1) (Part D)",
        ),
        "pre_service": Window(
            label=(
                "Part C: standard reconsideration within 30 days. "
                "Part D: redetermination within 7 calendar days."
            ),
            citation="42 CFR 422.590(a) (Part C) / 42 CFR 423.590(a) (Part D)",
        ),
        "post_service": Window(
            label=(
                "Payment reconsideration within 60 days (Part C); Part D "
                "payment redetermination within 14 days"
            ),
            citation="42 CFR 422.590(b) (Part C) / 42 CFR 423.590(a)(2) (Part D)",
        ),
        "concurrent": Window(
            label=(
                "Ongoing-care disputes are handled on the expedited (72-hour) "
                "track when waiting could seriously harm your health"
            ),
            citation="42 CFR 422.590(d) / 42 CFR 423.590(d)",
        ),
    },
    internal_appeal_filing=Window(
        label="You have 60 days from the denial notice to ask the plan for an appeal (reconsideration or redetermination)",
        citation="42 CFR 422.582(b) (Part C) / 42 CFR 423.582(b) (Part D)",
        days=60,
    ),
    external_review_filing=Window(
        label=(
            "If the plan upholds the denial, Part C appeals are forwarded "
            "automatically to an Independent Review Entity; Part D gives you "
            "60 days to request Independent Review Entity reconsideration"
        ),
        citation="42 CFR 422.592 (Part C auto-forward) / 42 CFR 423.600(b) (Part D)",
        days=60,
        varies=True,
    ),
    external_review_label="Independent Review Entity (IRE) reconsideration",
    notes=(
        "The 60-day filing window can sometimes be extended for good cause — ask "
        "the plan.",
        "Part C denials that the plan upholds move to the IRE automatically; you "
        "do not have to re-file for that step.",
    ),
)

_MEDICAID = CoverageRule(
    key="medicaid",
    name="Medicaid",
    summary=(
        "Medicaid appeal timing is set by federal floors but the exact windows "
        "vary by state and by whether you are in managed care or fee-for-service."
    ),
    decision_windows={
        # Managed-care appeal resolution: 42 CFR 438.408.
        "expedited": Window(
            label="Expedited managed-care appeal resolved within 72 hours",
            citation="42 CFR 438.408(b)(3)",
        ),
        "pre_service": Window(
            label="Managed-care appeals resolved within 30 days (states may set shorter windows)",
            citation="42 CFR 438.408(b)(2)",
            varies=True,
        ),
        "post_service": Window(
            label="Managed-care appeals resolved within 30 days — check your state's rules",
            citation="42 CFR 438.408(b)(2)",
            varies=True,
        ),
        "concurrent": Window(
            label=(
                "If you appeal within the notice period you may be able to keep "
                "benefits during the appeal (aid paid pending)"
            ),
            citation="42 CFR 438.420 (continuation of benefits)",
        ),
    },
    internal_appeal_filing=Window(
        label=(
            "In managed care you generally have 60 days from the notice to file "
            "a plan appeal — your state may allow less time, so check your notice"
        ),
        citation="42 CFR 438.402(c)(2)(ii)",
        days=60,
        varies=True,
    ),
    external_review_filing=Window(
        label=(
            "You have up to 120 days after the plan's appeal decision to request "
            "a state fair hearing; states may allow as few as 90 days"
        ),
        citation="42 CFR 438.408(f)(2); 42 CFR 431.221(d)",
        days=120,
        varies=True,
    ),
    external_review_label="State fair hearing",
    notes=(
        "Medicaid deadlines vary meaningfully by state — always follow the "
        "instructions on your denial or notice of adverse benefit determination.",
        "To keep benefits during the appeal you usually must act within 10 days "
        "of the notice.",
    ),
)

_OTHER = CoverageRule(
    key="other",
    name="Other / not sure",
    summary=(
        "If you are not sure which rules apply, use these common baselines as a "
        "starting point and confirm against your denial letter and plan "
        "documents."
    ),
    decision_windows={
        "expedited": Window(
            label="Urgent appeals are commonly decided within 72 hours",
            citation="Common baseline; see 29 CFR 2560.503-1 and 45 CFR 147.136",
            varies=True,
        ),
        "pre_service": Window(
            label="Pre-service appeals are commonly decided within about 30 days",
            citation="Common baseline; see 29 CFR 2560.503-1(i)",
            varies=True,
        ),
        "post_service": Window(
            label="Post-service appeals are commonly decided within about 60 days",
            citation="Common baseline; see 29 CFR 2560.503-1(i)",
            varies=True,
        ),
        "concurrent": Window(
            label="Ongoing-care disputes are usually handled on an urgent track",
            citation="Common baseline; see 29 CFR 2560.503-1(f)(2)",
            varies=True,
        ),
    },
    internal_appeal_filing=Window(
        label=(
            "Many plans give you at least 180 days to appeal, but public programs "
            "can be as short as 60 days — check your denial letter"
        ),
        citation="Varies; commonly 29 CFR 2560.503-1(h) / 45 CFR 147.136(b)",
        days=180,
        varies=True,
    ),
    external_review_filing=Window(
        label=(
            "External or independent review windows vary — commonly about 4 "
            "months after the final internal denial"
        ),
        citation="Varies; commonly 45 CFR 147.136(d)(2)",
        days=120,
        varies=True,
    ),
    external_review_label="External / independent review",
    notes=(
        "This is a general starting point only. The appeal-rights section of "
        "your denial letter is the authoritative source for your deadlines.",
    ),
)

RULES: dict[str, CoverageRule] = {
    rule.key: rule
    for rule in (
        _COMMERCIAL_ACA,
        _EMPLOYER_ERISA,
        _MEDICARE_ADVANTAGE_PARTD,
        _MEDICAID,
        _OTHER,
    )
}


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedWindow:
    """A window plus, when computable, the concrete deadline date."""

    label: str
    citation: str
    varies: bool
    deadline: Optional[datetime.date]


@dataclass(frozen=True)
class DeadlineResult:
    """The full, plain-language answer for a set of inputs."""

    coverage_name: str
    coverage_summary: str
    situation_label: str
    expedited: bool
    denial_received: Optional[datetime.date]
    insurer_decision: ResolvedWindow
    internal_appeal_filing: ResolvedWindow
    external_review_filing: ResolvedWindow
    external_review_label: str
    notes: tuple[str, ...]
    last_reviewed: datetime.date


def _situation_key(timing: str, expedited: bool) -> str:
    """Normalize (timing, expedited) into a decision-window key."""
    if expedited:
        return "expedited"
    if timing == "concurrent":
        return "concurrent"
    if timing == "post_service":
        return "post_service"
    # Default and explicit pre-service both map here.
    return "pre_service"


def _resolve(window: Window, start: Optional[datetime.date]) -> ResolvedWindow:
    return ResolvedWindow(
        label=window.label,
        citation=window.citation,
        varies=window.varies,
        deadline=window.deadline_from(start),
    )


def _timing_label(timing: str) -> str:
    for key, label in TIMING_CHOICES:
        if key == timing:
            return label
    return timing


def compute_deadlines(
    coverage_type: str,
    service_timing: str,
    expedited: bool,
    denial_received: Optional[datetime.date],
) -> DeadlineResult:
    """Compute the general appeal timeframes for the given inputs.

    Raises ``ValueError`` for unknown coverage or timing values so callers
    (forms) surface a clean validation error rather than a silent wrong answer.
    """
    if coverage_type not in _VALID_COVERAGE:
        raise ValueError(f"Unknown coverage type: {coverage_type!r}")
    if service_timing not in _VALID_TIMING:
        raise ValueError(f"Unknown service timing: {service_timing!r}")

    rule = RULES[coverage_type]
    situation = _situation_key(service_timing, expedited)
    decision_window = rule.decision_windows[situation]

    situation_label = _timing_label(service_timing)
    if expedited:
        situation_label = f"{situation_label} — expedited / urgent"

    return DeadlineResult(
        coverage_name=rule.name,
        coverage_summary=rule.summary,
        situation_label=situation_label,
        expedited=expedited,
        denial_received=denial_received,
        insurer_decision=_resolve(decision_window, denial_received),
        internal_appeal_filing=_resolve(rule.internal_appeal_filing, denial_received),
        external_review_filing=_resolve(rule.external_review_filing, denial_received),
        external_review_label=rule.external_review_label,
        notes=rule.notes,
        last_reviewed=LAST_REVIEWED,
    )

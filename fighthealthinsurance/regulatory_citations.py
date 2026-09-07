"""Curated state/federal prior-authorization & utilization-review reforms.

These are surfaced into the appeal prompt (see
``AppealGenerator._collect_regulatory_context``) keyed off the patient's state,
with deliberately conservative framing so the model cites a law only where it
actually applies. Every entry is a real, sourced statute or rule; we cite by
name and effective date and never invent section numbers, quotations, or dates.

Scope is intentionally bounded: we only inject this block for states where we
have a verified hook, so the vast majority of appeals are unaffected. The
``source_url`` field is for our own documentation/tests and is *not* rendered
into the prompt (to avoid the model parroting URLs it cannot verify).
"""

import datetime
import re
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class RegulatoryHook:
    """A single law/rule a patient may be able to cite in an appeal."""

    name: str
    summary: str
    jurisdiction: str  # "US" for federal, or a 2-letter state code
    effective: str  # human-readable effective date / status
    source_url: str  # documentation only; not rendered into the prompt
    # Whether the hook reaches self-funded ERISA employer plans. State
    # insurance mandates do not; neither do payer-type-specific federal rules
    # (e.g. CMS-0057-F, which covers MA/Medicaid/CHIP/FFE issuers only).
    applies_to_self_insured: bool
    # Machine-readable effective date used to gate not-yet-in-force laws. When
    # set, the hook is only surfaced on/after this date so we never cite a law
    # before it applies (the ``effective`` string above is the human-readable
    # companion). ``None`` means "already in force / no gate".
    effective_date: Optional[datetime.date] = None


# Federal hooks apply broadly (subject to plan type); included alongside any
# matched state hook so the model has both layers to draw on.
FEDERAL_HOOKS: tuple[RegulatoryHook, ...] = (
    RegulatoryHook(
        name="CMS Interoperability and Prior Authorization Final Rule (CMS-0057-F)",
        summary=(
            "Impacted payers must send a specific reason for every "
            "prior-authorization denial and publicly report PA approval, "
            "denial, and appeal metrics. Use it to demand the specific denial "
            "rationale and the exact criteria applied."
        ),
        jurisdiction="US",
        effective="finalized 2024; compliance phasing in through 2026-2027",
        source_url=(
            "https://www.federalregister.gov/documents/2024/02/08/2024-00895/"
            "medicare-and-medicaid-programs-advancing-interoperability-and-"
            "improving-prior-authorization-processes"
        ),
        # CMS limits impacted payers to MA orgs, Medicaid/CHIP, and FFE QHP
        # issuers — not self-funded employer (ERISA) plans.
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name=(
            "CMS 2024 Final Rule on the use of algorithms and artificial "
            "intelligence in coverage determinations"
        ),
        summary=(
            "An algorithm or AI tool may not be the sole basis for an adverse "
            "coverage determination; an individualized assessment by a "
            "qualified human reviewer is required. Demand disclosure of any "
            "tool used and the human reviewer's clinical rationale."
        ),
        jurisdiction="US",
        effective="effective 2024",
        source_url=(
            "https://www.kff.org/patient-consumer-protections/"
            "regulation-of-ai-in-prior-authorization-and-claims-review-a-look-"
            "at-federal-and-state-consumer-protections/"
        ),
        # This is a Medicare Advantage rule; it does not bind self-funded
        # commercial employer plans.
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name="ACA internal appeal and external review rights (45 C.F.R. § 147.136)",
        summary=(
            "Non-grandfathered plans must provide a full and fair internal "
            "appeal and access to independent external review. Insist on both, "
            "and on the documents and clinical criteria relied upon."
        ),
        jurisdiction="US",
        effective="in force",
        source_url=(
            "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-B/"
            "part-147/section-147.136"
        ),
        # Internal claims/appeals and external review reach non-grandfathered
        # self-insured group health plans (enforced via 29 C.F.R. 2590.715-2719).
        applies_to_self_insured=True,
    ),
)


# AI-oversight states: laws requiring human clinical review and barring AI as
# the sole basis for a medical-necessity/utilization-review denial (per the
# KFF federal/state tracker, enacted as of April 2026). We name them
# descriptively and point to the tracker rather than inventing per-state
# statute numbers we have not individually verified.
_AI_OVERSIGHT_STATES: tuple[tuple[str, str], ...] = (
    ("CA", "California"),
    ("TX", "Texas"),
    ("IL", "Illinois"),
    ("AL", "Alabama"),
    ("UT", "Utah"),
    # Washington (SB 5395) and Maryland (HB 1563) are intentionally absent
    # here: each has an explicit, individually-verified hook below with a known
    # bill number and effective date, so listing them generically would
    # duplicate those entries.
)

_KFF_AI_TRACKER_URL = (
    "https://www.kff.org/patient-consumer-protections/"
    "regulation-of-ai-in-prior-authorization-and-claims-review-a-look-at-"
    "federal-and-state-consumer-protections/"
)


# Explicit, individually-sourced state reforms.
_EXPLICIT_STATE_HOOKS: tuple[RegulatoryHook, ...] = (
    RegulatoryHook(
        name="Massachusetts prior-authorization elimination regulation",
        summary=(
            "Massachusetts has eliminated prior authorization for a defined set "
            "of essential services (including cancer imaging, chronic-disease "
            "medications, maternity care, and primary care). For a "
            "fully-insured Massachusetts plan, demand that a denied service "
            "within the regulation be processed without prior authorization."
        ),
        jurisdiction="MA",
        effective="effective June 5, 2026",
        source_url=(
            "https://www.mass.gov/news/governor-healey-announces-final-regs-"
            "that-eliminate-prior-authorization-requirements-for-routine-and-"
            "essential-health-care"
        ),
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name="West Virginia continuity-of-care prior-authorization law",
        summary=(
            "A patient already approved for a treatment may switch to a "
            "medically equivalent alternative of equal or lesser cost without "
            "a new prior authorization. Cite it where a new PA is being "
            "demanded for an equivalent therapy."
        ),
        jurisdiction="WV",
        effective="effective June 10, 2026",
        source_url=(
            "https://kffhealthnews.org/news/article/prior-authorization-"
            "insurance-delays-coverage-denials-state-laws-west-virginia/"
        ),
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name="Washington prior-authorization AI-oversight and transparency law",
        summary=(
            "An AI or algorithm may not be the sole basis to deny, delay, or "
            "modify care; a licensed provider must make any medical-necessity "
            "adverse determination, the tool must account for the patient's "
            "individual clinical condition (not just group data), and the "
            "denial notice must disclose the credentials, board certifications, "
            "and specialty of the provider who had clinical oversight. Demand "
            "that human reviewer's clinical rationale and credentials, and "
            "confirmation that AI was not the sole basis for the denial."
        ),
        jurisdiction="WA",
        effective="effective June 11, 2026",
        # Washington SB 5395 (2026); effective date and provisions confirmed via
        # the bill sponsor's office and a state-law summary (Holland & Knight,
        # May 2026). Per this module's convention we render a descriptive name
        # rather than the bill number into the prompt.
        source_url=(
            "https://senatedemocrats.wa.gov/orwall/2026/03/25/orwall-bill-to-"
            "improve-prior-authorization-transparency-signed-into-law/"
        ),
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name=("Maryland utilization-review human-determination and AI-reporting law"),
        summary=(
            "Only a licensed health-care professional may render an adverse "
            "(medical-necessity) determination, and the carrier must report to "
            "the Insurance Commissioner each quarter whether AI was used in its "
            "adverse decisions. Demand confirmation that a licensed professional "
            "-- not an algorithm -- made this determination, and that the "
            "carrier is recording its AI use as the law requires."
        ),
        jurisdiction="MD",
        effective="effective June 1, 2026",
        # Maryland HB 1563 (2026); text/effective date per mgaleg.maryland.gov
        # and the Holland & Knight state-law roundup (May 2026). Descriptive
        # name per this module's convention (bill number kept out of the prompt).
        source_url=(
            "https://mgaleg.maryland.gov/mgawebsite/Legislation/Details/HB1563"
        ),
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name="Indiana law restricting AI as the sole basis for claim downcoding",
        summary=(
            "An AI or algorithm may not be the sole basis to downcode a claim "
            "(reduce a billed code to a lower-paying one) without review by a "
            "qualified health professional. Cite it where a claim was downcoded "
            "or a service reclassified to a lower level: demand confirmation "
            "that a qualified human reviewed the change and that AI was not the "
            "sole basis for it."
        ),
        jurisdiction="IN",
        effective="effective July 1, 2026",
        effective_date=datetime.date(2026, 7, 1),
        # Indiana HB 1271 (2026); effective July 1, 2026 per the Holland & Knight
        # state-law roundup (May 2026). Downcoding-specific -- the only hook here
        # aimed at code reduction rather than outright denial. Gated by
        # effective_date so it is not cited before it is in force.
        source_url="https://legiscan.com/IN/bill/HB1271/2026",
        applies_to_self_insured=False,
    ),
    RegulatoryHook(
        name="Georgia AI utilization-review oversight law",
        summary=(
            "An AI or algorithm may not be the sole basis for an adverse "
            "utilization-review determination; a qualified human must review. "
            "It may apply only to health plans or policies issued, delivered, "
            "or renewed on or after its effective date, so confirm the plan was "
            "issued or renewed under the new law before relying on it, and "
            "demand a qualified human reviewer where it applies."
        ),
        jurisdiction="GA",
        effective="effective January 1, 2027",
        effective_date=datetime.date(2027, 1, 1),
        # Georgia SB 544 (2026); effective Jan 1, 2027 per the Holland & Knight
        # state-law roundup (May 2026). Gated by effective_date so we do not
        # cite a not-yet-in-force law (consistent with this module's "cite only
        # where it actually applies" framing); the summary also flags that it
        # may bind only newly issued/renewed plans.
        source_url="https://legiscan.com/GA/bill/SB544/2026",
        applies_to_self_insured=False,
    ),
)

# Per-state AI-oversight hooks, generated from the KFF tracker list.
_AI_OVERSIGHT_HOOKS: tuple[RegulatoryHook, ...] = tuple(
    RegulatoryHook(
        name=(
            f"{state_name} law restricting AI as the sole basis for a "
            "medical-necessity or coverage denial"
        ),
        summary=(
            "State law requires that AI or an algorithm not be the sole basis "
            "for a medical-necessity or utilization-review denial and that a "
            "qualified human clinician make the determination. Demand "
            "confirmation that a human reviewer applied the patient's full "
            "clinical picture."
        ),
        jurisdiction=abbr,
        effective="enacted as of 2025-2026 (see KFF tracker)",
        source_url=_KFF_AI_TRACKER_URL,
        applies_to_self_insured=False,
    )
    for abbr, state_name in _AI_OVERSIGHT_STATES
)

STATE_HOOKS: tuple[RegulatoryHook, ...] = (
    *_EXPLICIT_STATE_HOOKS,
    *_AI_OVERSIGHT_HOOKS,
)


_STATE_NAME_TO_ABBR: dict[str, str] = {
    "massachusetts": "MA",
    "west virginia": "WV",
    "california": "CA",
    "texas": "TX",
    "illinois": "IL",
    "alabama": "AL",
    "utah": "UT",
    "washington": "WA",
    "maryland": "MD",
    "indiana": "IN",
    "georgia": "GA",
}


def _normalize_state(state: Optional[str]) -> Optional[str]:
    """Return a 2-letter state code from a 2-letter code or a full name."""
    if not state:
        return None
    s = state.strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _STATE_NAME_TO_ABBR.get(s.lower())


def _hook_in_effect(hook: RegulatoryHook, today: datetime.date) -> bool:
    """A hook is citable only once it is in force.

    Hooks with no ``effective_date`` are already in force and always pass;
    future-dated laws (e.g. IN HB 1271, GA SB 544) are withheld until their
    effective date so we never cite a law before it applies.
    """
    return hook.effective_date is None or today >= hook.effective_date


def get_regulatory_citation_context(
    state: Optional[str],
    denial_text: Optional[str] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    self_insured: Optional[bool] = None,
    as_of: Optional[datetime.date] = None,
) -> Optional[str]:
    """Return a conservatively-framed regulatory block for the denial's state.

    Returns ``None`` unless the state has at least one verified hook, so the
    overwhelming majority of appeals are unaffected. ``denial_text`` /
    ``procedure`` / ``diagnosis`` are accepted for future service-specific
    refinement and are intentionally unused today.

    When ``self_insured`` is ``True`` (a self-funded ERISA employer plan), hooks
    that do not reach such plans are dropped — state insurance mandates and
    payer-type-specific federal rules (e.g. CMS-0057-F, which covers Medicare
    Advantage, Medicaid/CHIP, and federally-facilitated Marketplace issuers, not
    self-funded employer plans) — so we never tell a self-insured appellant to
    rely on a protection that does not apply to them.
    """
    abbr = _normalize_state(state)
    if not abbr:
        return None
    today = as_of or datetime.date.today()
    state_hooks = [
        h for h in STATE_HOOKS if h.jurisdiction == abbr and _hook_in_effect(h, today)
    ]
    if not state_hooks:
        return None

    hooks = [h for h in FEDERAL_HOOKS if _hook_in_effect(h, today)] + state_hooks
    if self_insured is True:
        hooks = [h for h in hooks if h.applies_to_self_insured]

    bullet_lines = "\n".join(f"- {h.name} ({h.effective}): {h.summary}" for h in hooks)

    if self_insured is True:
        caveat = (
            "Note: this appears to be a self-insured (ERISA) employer plan. "
            "State insurance mandates and Medicare/Medicaid/Marketplace-specific "
            "federal prior-authorization rules generally do not bind it, so only "
            "the broadly applicable federal protections above are listed; rely on "
            "those and on the plan's own terms, and confirm the plan's specific "
            "obligations."
        )
    else:
        caveat = (
            "Note: the state laws listed above generally apply to fully-insured "
            "plans; self-insured (ERISA) employer plans are typically exempt, so "
            "confirm the plan type before relying on a state mandate."
        )

    header = (
        "REGULATORY CONTEXT: The following federal and state laws MAY support "
        "this appeal depending on the plan type and the specific service. Cite "
        "them BY NAME only where they actually apply to this denial. Do not "
        "assert applicability you cannot support, and do not invent statute "
        "section numbers, dates, or quotations beyond what is provided here."
    )
    return f"{header}\n{bullet_lines}\n{caveat}"


# ---------------------------------------------------------------------------
# Which appeal law governs the plan, from what intake collected.
#
# Intake asks "How do you get your insurance?" (PlanSource) and knows when the
# carrier is a TPA for self-funded employer plans, and the denial text can
# match the ERISA regulator. None of that used to reach the prompt, so a letter
# cited ERISA or the ACA only when a clinical template or a state hook happened
# to fire. This block names the governing law, says which law does NOT apply,
# and invites the model to cite the applicable one where it helps (Melanie,
# 2026-09-07: encourage, not order). Every citation here is a real statute or
# rule cited by name and section; nothing is quoted.
# ---------------------------------------------------------------------------

PLAN_LAW_HEADER = "APPLICABLE APPEAL LAW"

_ERISA = (
    "This looks like a private employer or union plan, so ERISA governs the "
    "appeal (ERISA section 503, 29 U.S.C. § 1133, and the claims-procedure "
    "rule at 29 C.F.R. § 2560.503-1). The plan must give the specific reasons "
    "for the denial and the plan provisions it relied on, must provide on "
    "request the internal rule or clinical criteria it applied and identify "
    "any medical expert it consulted, and must give a full and fair review by "
    "someone who did not make the original decision. A non-grandfathered plan "
    "also owes an independent external review (45 C.F.R. § 147.136, applied "
    "to group plans by 29 C.F.R. § 2590.715-2719). ERISA does not apply to "
    "government or church employer plans."
)
_ACA_MARKETPLACE = (
    "This is a marketplace (Affordable Care Act) plan, so ERISA does not "
    "apply. The plan owes an internal appeal and an independent external "
    "review under the ACA (45 C.F.R. § 147.136), and it must cover the ten "
    "essential health benefit categories (42 U.S.C. § 18022), so if the "
    "service falls in one of them, say so."
)
_GOVERNMENT_EMPLOYER = (
    "This is a state or local government employer plan, so ERISA does not "
    "apply (29 U.S.C. § 1003(b)(1)). A non-grandfathered plan still owes the "
    "ACA internal appeal and external review process (45 C.F.R. § 147.136), "
    "and state insurance law may apply if the plan is fully insured."
)
_FEDERAL_EMPLOYER = (
    "This is a federal employee (FEHB) plan under 5 U.S.C. chapter 89, so "
    "ERISA does not apply. A disputed claim goes first to the carrier for "
    "reconsideration and then to the Office of Personnel Management "
    "(5 C.F.R. § 890.105)."
)
_MEDICARE_ADVANTAGE = (
    "This is a Medicare Advantage plan. Neither ERISA nor the ACA appeal rules "
    "apply; the appeal follows the Medicare Advantage organization "
    "determination and reconsideration process (42 C.F.R. Part 422, Subpart "
    "M), with automatic review by the independent review entity if the plan "
    "upholds its denial."
)
_MEDICARE = (
    "This is Original Medicare. Neither ERISA nor the ACA appeal rules apply; "
    "the appeal follows the Medicare redetermination and reconsideration "
    "process (42 C.F.R. Part 405, Subpart I)."
)
_MEDICAID = (
    "This is a Medicaid plan. Neither ERISA nor the ACA appeal rules apply; a "
    "managed care plan owes an internal appeal (42 C.F.R. Part 438, Subpart "
    "F) and the patient has the right to a state fair hearing (42 C.F.R. "
    "Part 431, Subpart E)."
)
_VA = (
    "This is Veterans Affairs coverage. Neither ERISA nor the ACA appeal "
    "rules apply; VA has its own clinical appeal process, so argue the "
    "medical case and do not cite either law."
)
_UNKNOWN = (
    "We do not know how the patient gets this coverage. If the denial letter "
    "or the plan documents show it is a private employer or union plan, "
    "ERISA's claims-procedure rule (29 C.F.R. § 2560.503-1) most likely "
    "applies; if they show it is a marketplace or individual plan, the ACA "
    "internal appeal and external review rules (45 C.F.R. § 147.136) apply. "
    "Name one of them only when the letter itself supports it; otherwise ask "
    "for the plan's internal appeal and independent external review in plain "
    "words rather than naming a statute."
)
_TWO_SOURCES = (
    "More than one coverage source was given. Use the one the denial letter "
    "itself supports."
)
_INVITATION = (
    "If citing the applicable law strengthens this appeal (for example to "
    "demand the clinical criteria the plan relied on, or to insist on an "
    "independent external review), cite it by name and section exactly as "
    "given here; a reviewer takes a letter more seriously when it names the "
    "rule the plan must follow. Cite it only where it applies and where it "
    "helps the argument. Never cite a law that does not govern this plan, and "
    "do not invent section numbers, deadlines, quotations, or case names "
    "beyond what is listed here."
)

# (marker in the lower-cased plan source name, paragraph). First match wins,
# so the more specific markers come first ("medicare advantage" before
# "medicare", "federal government" before "government").
_PLAN_SOURCE_LAW: tuple[tuple[str, str], ...] = (
    ("medicare advantage", _MEDICARE_ADVANTAGE),
    ("medicare", _MEDICARE),
    ("medicaid", _MEDICAID),
    ("veterans", _VA),
    ("marketplace", _ACA_MARKETPLACE),
    ("affordable care", _ACA_MARKETPLACE),
    ("federal government", _FEDERAL_EMPLOYER),
    ("government", _GOVERNMENT_EMPLOYER),
    ("employer", _ERISA),
    ("union", _ERISA),
    ("other group", _ERISA),
)


def _law_for_plan_source(name: str) -> Optional[str]:
    label = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not label:
        return None
    for marker, paragraph in _PLAN_SOURCE_LAW:
        if marker in label:
            return paragraph
    return None  # "Other", "Don't know": nothing to say


def get_plan_law_context(
    plan_sources: Iterable[str],
    is_tpa: bool = False,
    regulator_alt_name: Optional[str] = None,
) -> str:
    """Return the APPLICABLE APPEAL LAW block for the appeal prompt.

    ``plan_sources`` are the PlanSource names the person picked at intake;
    ``is_tpa`` says the carrier administers self-funded employer plans; and
    ``regulator_alt_name`` is "ERISA" when the denial text matched the ERISA
    regulator. Always returns a block: an unknown plan gets the hedged
    paragraph, so the model is never left to guess in silence.
    """
    paragraphs: list[str] = []
    if is_tpa or (regulator_alt_name or "").strip().upper() == "ERISA":
        paragraphs.append(_ERISA)
    for name in plan_sources:
        paragraph = _law_for_plan_source(name)
        if paragraph is not None and paragraph not in paragraphs:
            paragraphs.append(paragraph)
    if not paragraphs:
        paragraphs.append(_UNKNOWN)
    elif len(paragraphs) > 1:
        paragraphs.append(_TWO_SOURCES)
    body = "\n".join(paragraphs)
    return f"{PLAN_LAW_HEADER}: {body}\n{_INVITATION}"

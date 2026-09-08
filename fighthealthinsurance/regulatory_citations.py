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

# Plan programs, as far as intake and the denial tell us: the plan source
# ("How do you get your insurance?"), the carrier's TPA flag, and whether the
# denial letter matched the ERISA regulator. One classification shared by
# both prompt blocks below, so they cannot contradict each other (review).
ERISA = "erisa"  # private employer or union plan
TPA = "tpa"  # carrier administers self-funded employer plans
ERISA_LETTER = "erisa_letter"  # the denial letter itself named ERISA rights
OTHER_GROUP = "other_group"
MARKETPLACE = "marketplace"
GOVERNMENT = "government"  # state or local government employer
FEHB = "fehb"  # federal employee
MEDICARE_ADVANTAGE = "medicare_advantage"
MEDICARE = "medicare"
MEDICAID = "medicaid"
VA = "va"

PUBLIC_PROGRAMS = frozenset({MEDICARE_ADVANTAGE, MEDICARE, MEDICAID, VA, FEHB})
# Coverage ERISA cannot govern. A denial letter that mentions ERISA rights
# does not override these; the block names the conflict instead.
EXCLUSIVE_OF_ERISA = PUBLIC_PROGRAMS | {GOVERNMENT}


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
    # The public programs (PUBLIC_PROGRAMS keys) this hook still reaches. Empty
    # means commercial and government-employer coverage only: state insurance
    # law and the ACA appeal rules do not bind Medicare, Medicaid, VA or FEHB
    # coverage, so for those only hooks written for the program are listed.
    public_programs: frozenset[str] = frozenset()


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
        public_programs=frozenset({MEDICARE_ADVANTAGE, MEDICAID}),
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
        public_programs=frozenset({MEDICARE_ADVANTAGE}),
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
    programs: Iterable[str] = (),
) -> Optional[str]:
    """Return a conservatively-framed regulatory block for the denial's state.

    ``programs`` is classify_plan()'s result. For a public program (Medicare
    Advantage, Original Medicare, Medicaid, VA, FEHB) only hooks that declare
    they reach it are kept, because state insurance law and the ACA appeal
    rules do not bind such coverage and the plan-law block says so; listing
    them here too would put two contradicting instructions in one prompt
    (review). With nothing left, no block.

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
    public = set(programs) & PUBLIC_PROGRAMS
    if public:
        hooks = [h for h in hooks if h.public_programs & public]
    if self_insured is True:
        hooks = [h for h in hooks if h.applies_to_self_insured]
    if not hooks:
        return None

    bullet_lines = "\n".join(f"- {h.name} ({h.effective}): {h.summary}" for h in hooks)

    if public:
        label = next(_PROGRAM_LABELS[k] for k in _PROGRAM_ORDER if k in public)
        caveat = (
            f"Note: this is {label} coverage. State insurance mandates and the "
            "ACA appeal rules do not bind it; only the federal rules written "
            "for the program are listed, so rely on those and on the program's "
            "own appeal process."
        )
    elif self_insured is True:
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
    "someone who did not make the original decision. If the plan is not "
    "grandfathered and the denial turns on medical judgment (medical "
    "necessity, appropriateness, level of care, or an experimental label) or "
    "is a rescission, the patient is also owed an independent external review "
    "(45 C.F.R. § 147.136, applied to group plans by 29 C.F.R. § "
    "2590.715-2719); a denial for ineligibility is not. ERISA does not apply "
    "to government or church employer plans (29 U.S.C. § 1003(b))."
)
_TPA_ONLY = (
    "The carrier administers self-funded employer plans as a third-party "
    "administrator, so this is most likely a self-funded plan. If the "
    "employer is a private company or a union, ERISA governs the appeal "
    "(ERISA section 503, 29 U.S.C. § 1133, and 29 C.F.R. § 2560.503-1: the "
    "specific reasons, the criteria relied on, and a full and fair review). "
    "If the employer is a government or a church, ERISA does not apply "
    "(29 U.S.C. § 1003(b)); the plan's own appeal terms apply, and for a "
    "non-grandfathered plan the ACA internal appeal and external review rules "
    "(45 C.F.R. § 147.136) as well. Use whichever the denial letter or the "
    "plan documents support."
)
_OTHER_GROUP = (
    "This is group coverage that is not clearly an employer or union plan. If "
    "a private employer or union sponsors it, ERISA governs (29 C.F.R. § "
    "2560.503-1); a government or church employer's plan is exempt "
    "(29 U.S.C. § 1003(b)); and if it is an association or membership plan, "
    "ERISA may not apply, and for non-grandfathered coverage the ACA internal "
    "appeal and external review rules (45 C.F.R. § 147.136) apply along with "
    "state insurance law. Name ERISA only if the denial letter or the plan "
    "documents show a private employer or union sponsor."
)
_ACA_MARKETPLACE = (
    "This is marketplace (Affordable Care Act) coverage. If it is an "
    "individual plan bought on the marketplace, ERISA does not apply; the "
    "plan owes an internal appeal and, for a denial that turns on medical "
    "judgment or is a rescission, an independent external review under the "
    "ACA (45 C.F.R. § 147.136), and it must cover the ten essential health "
    "benefit categories (42 U.S.C. § 18022), so if the service falls in one "
    "of them, say so. If it is small employer (SHOP) coverage, it is an "
    "employer plan: ERISA governs it when a private employer sponsors it "
    "(29 C.F.R. § 2560.503-1), and a government or church employer's plan "
    "is exempt (29 U.S.C. § 1003(b))."
)
_GOVERNMENT_EMPLOYER = (
    "This is a state or local government employer plan, so ERISA does not "
    "apply (29 U.S.C. § 1003(b)(1)). A non-grandfathered plan still owes the "
    "ACA internal appeal and, for a denial that turns on medical judgment, an "
    "independent external review (45 C.F.R. § 147.136), and state insurance "
    "law may apply if the plan is fully insured."
)
_FEDERAL_EMPLOYER = (
    "This is a federal employee (FEHB) plan under 5 U.S.C. chapter 89, so "
    "ERISA does not apply. A disputed claim goes first to the carrier for "
    "reconsideration and then to the Office of Personnel Management "
    "(5 C.F.R. § 890.105)."
)
_MEDICARE_ADVANTAGE = (
    "This is a Medicare Advantage plan. Neither ERISA nor the ACA appeal rules "
    "apply. A denial of a medical service or item follows the Medicare "
    "Advantage organization determination and reconsideration process "
    "(42 C.F.R. Part 422, Subpart M), and the plan must itself forward an "
    "upheld denial to the independent review entity. A denial of a "
    "prescription drug under the plan's Part D benefit follows the Part D "
    "process instead (42 C.F.R. Part 423, Subpart M), where after the plan's "
    "redetermination the patient must ask the independent review entity for "
    "reconsideration themselves."
)
_MEDICARE = (
    "This is Original Medicare. Neither ERISA nor the ACA appeal rules apply. "
    "A claim denial follows the Medicare redetermination and reconsideration "
    "process (42 C.F.R. Part 405, Subpart I); a prescription drug denial "
    "under a Part D plan follows the Part D process (42 C.F.R. Part 423, "
    "Subpart M)."
)
_MEDICAID = (
    "This is a Medicaid plan. Neither ERISA nor the ACA appeal rules apply; a "
    "managed care plan owes an internal appeal (42 C.F.R. Part 438, Subpart "
    "F) and the patient has the right to a state fair hearing (42 C.F.R. "
    "Part 431, Subpart E)."
)
_VA = (
    "This is Veterans Affairs coverage. Neither ERISA nor the ACA appeal "
    "rules apply. VA has its own processes: a clinical appeal for a decision "
    "about treatment, and a benefits decision review for a claim such as "
    "reimbursement of care outside VA. Argue the medical case and do not cite "
    "either law."
)
_UNKNOWN = (
    "We do not know how the patient gets this coverage. If the denial letter "
    "or the plan documents show it is a private employer or union plan, "
    "ERISA's claims-procedure rule (29 C.F.R. § 2560.503-1) most likely "
    "applies; if they show it is a marketplace plan or other non-grandfathered "
    "individual coverage, the ACA internal appeal and external review rules "
    "(45 C.F.R. § 147.136) apply to a denial that turns on medical judgment. "
    "Name one of them only when the letter itself supports it; otherwise ask "
    "for the plan's internal appeal and, where the denial is about medical "
    "judgment, an independent external review in plain words rather than "
    "naming a statute."
)
_TWO_SOURCES = (
    "More than one coverage source was given. Use the one the denial letter "
    "itself supports."
)
_INVITATION = (
    "If citing the applicable law strengthens this appeal (for example to "
    "demand the clinical criteria the plan relied on, or, only where the "
    "paragraph above says the plan owes one, to insist on an independent "
    "external review), cite it by name and section exactly as given here; a "
    "reviewer takes a "
    "letter more seriously when it names the rule the plan must follow. Cite "
    "it only where it applies and where it helps the argument. Never cite a "
    "law that does not govern this plan, and do not invent section numbers, "
    "deadlines, quotations, or case names beyond what is listed here."
)

_LETTER_CONFLICT = (
    "The denial letter mentions ERISA appeal rights, but the coverage source "
    "given is one that ERISA does not govern. Follow the letter only if the "
    "plan documents confirm a private employer or union plan; otherwise rely "
    "on the process described above and do not cite ERISA."
)

# (marker in the lower-cased plan source name, program). First match wins, so
# the more specific markers come first ("medicare advantage" before
# "medicare", "federal government" before "government").
_PLAN_SOURCE_PROGRAM: tuple[tuple[str, str], ...] = (
    ("medicare advantage", MEDICARE_ADVANTAGE),
    ("medicare", MEDICARE),
    ("medicaid", MEDICAID),
    ("veterans", VA),
    ("marketplace", MARKETPLACE),
    ("affordable care", MARKETPLACE),
    ("federal government", FEHB),
    ("government", GOVERNMENT),
    ("employer", ERISA),
    ("union", ERISA),
    ("other group", OTHER_GROUP),
)

_PARAGRAPHS: dict[str, str] = {
    ERISA: _ERISA,
    OTHER_GROUP: _OTHER_GROUP,
    MARKETPLACE: _ACA_MARKETPLACE,
    GOVERNMENT: _GOVERNMENT_EMPLOYER,
    FEHB: _FEDERAL_EMPLOYER,
    MEDICARE_ADVANTAGE: _MEDICARE_ADVANTAGE,
    MEDICARE: _MEDICARE,
    MEDICAID: _MEDICAID,
    VA: _VA,
}

_PROGRAM_ORDER: tuple[str, ...] = (MEDICARE_ADVANTAGE, MEDICARE, MEDICAID, VA, FEHB)
_PROGRAM_LABELS: dict[str, str] = {
    MEDICARE_ADVANTAGE: "Medicare Advantage",
    MEDICARE: "Original Medicare",
    MEDICAID: "Medicaid",
    VA: "Veterans Affairs",
    FEHB: "federal employee (FEHB)",
}


def _program_for_plan_source(name: str) -> Optional[str]:
    label = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not label:
        return None
    for marker, program in _PLAN_SOURCE_PROGRAM:
        if marker in label:
            return program
    return None  # "Other", "Don't know": nothing to say


def classify_plan(
    plan_sources: Iterable[str],
    is_tpa: bool = False,
    regulator_alt_name: Optional[str] = None,
) -> tuple[str, ...]:
    """Program keys for this denial, in the order the signals were given:
    the plan sources' programs first, then TPA, then ERISA_LETTER. Empty when
    nothing is known."""
    programs: list[str] = []
    for name in plan_sources:
        program = _program_for_plan_source(name)
        if program is not None and program not in programs:
            programs.append(program)
    if is_tpa:
        programs.append(TPA)
    if (regulator_alt_name or "").strip().upper() == "ERISA":
        programs.append(ERISA_LETTER)
    return tuple(programs)


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
    programs = classify_plan(
        plan_sources, is_tpa=is_tpa, regulator_alt_name=regulator_alt_name
    )
    sources = [k for k in programs if k in _PARAGRAPHS]
    paragraphs: list[str] = []
    conflict: Optional[str] = None
    # The denial letter naming ERISA rights is the strongest signal we have,
    # except against coverage ERISA cannot govern: there the source wins and
    # the conflict is named (review).
    if ERISA_LETTER in programs:
        if any(k in EXCLUSIVE_OF_ERISA for k in sources):
            conflict = _LETTER_CONFLICT
        elif ERISA not in sources:
            paragraphs.append(_ERISA)
    for k in sources:
        if _PARAGRAPHS[k] not in paragraphs:
            paragraphs.append(_PARAGRAPHS[k])
    # A TPA administers self-funded plans, and a self-funded plan can belong
    # to a city as easily as to a company, so the flag alone does not make it
    # ERISA (review). With a plan source, the source decides; without one,
    # the hedged self-funded paragraph.
    if TPA in programs and not paragraphs:
        paragraphs.append(_TPA_ONLY)
    if not paragraphs:
        paragraphs.append(_UNKNOWN)
    elif len(paragraphs) > 1:
        paragraphs.append(_TWO_SOURCES)
    if conflict is not None:
        paragraphs.append(conflict)
    body = "\n".join(paragraphs)
    return f"{PLAN_LAW_HEADER}: {body}\n{_INVITATION}"

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
# Sources that describe private or government-employer coverage, which state
# insurance law and the ACA appeal rules can reach. The TPA flag is not one:
# it says who administers the plan, not that a second plan exists (review).
PRIVATE_COVERAGE = frozenset({ERISA, OTHER_GROUP, MARKETPLACE, GOVERNMENT})


def self_insured_from(programs: Iterable[str]) -> Optional[bool]:
    """True when the TPA flag stands for a self-funded ERISA employer plan:
    the flag is set and no source says otherwise (a government plan is
    exempt from ERISA whoever administers it, and marketplace coverage is
    insured). None when we cannot tell, which selects neutral wording."""
    keys = set(programs)
    # "Other Group" is group coverage that may not be an employer plan at
    # all, so a TPA beside it is inconclusive too (review).
    if TPA in keys and not keys & (EXCLUSIVE_OF_ERISA | {MARKETPLACE, OTHER_GROUP}):
        return True
    return None


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
    # Whether the hook also reaches plans sold on the federal marketplace
    # (HealthCare.gov). Not a public program, so kept apart from
    # public_programs; a state's own marketplace is not covered.
    federal_marketplace: bool = False
    # The summary in plain words for the person themselves, as the public MCP
    # server shows it: what the law says and what an appeal can ask for,
    # never instructions for drafting a letter. ``summary`` stays the
    # prompt's wording.
    plain_summary: str = ""


# Federal hooks apply broadly (subject to plan type); included alongside any
# matched state hook so the model has both layers to draw on.
FEDERAL_HOOKS: tuple[RegulatoryHook, ...] = (
    RegulatoryHook(
        name="CMS Interoperability and Prior Authorization Final Rule (CMS-0057-F)",
        summary=(
            "Impacted payers must send a specific reason for every "
            "prior-authorization denial of an item or service (prescription "
            "drugs are outside this rule) and publicly report PA approval, "
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
        federal_marketplace=True,
        plain_summary=(
            "Medicare Advantage plans, Medicaid and CHIP plans, and plans "
            "sold on HealthCare.gov must give a specific reason for every "
            "prior authorization denial of an item or service (prescription "
            "drugs are outside this rule), and publicly report their prior "
            "authorization approval, denial and appeal numbers. An appeal can "
            "ask for that specific reason and the exact criteria the plan "
            "applied."
        ),
    ),
    RegulatoryHook(
        name=(
            "Medicare Advantage coverage criteria rule (CMS-4201-F, 42 C.F.R. "
            "§ 422.101(b)(6) and (c)(1)(i)) and CMS's February 6, 2024 FAQ memo"
        ),
        summary=(
            "Medicare Advantage plans only, for Part A and Part B benefits "
            "(a Part D drug denial follows 42 C.F.R. Part 423, Subpart M). A "
            "medical necessity decision must rest on the enrollee's own "
            "medical history, physician recommendations, and clinical notes, "
            "and the plan may use internal coverage criteria only where "
            "Medicare's are not fully established and only if they are public "
            "with their evidence. CMS's memo says an algorithm or AI tool may "
            "assist with, but cannot replace, that individual assessment. "
            "Demand disclosure of any tool used and the reviewer's clinical "
            "rationale."
        ),
        jurisdiction="US",
        effective="applies to coverage from January 1, 2024",
        source_url="https://www.ecfr.gov/current/title-42/section-422.101",
        # Binds Medicare Advantage plans only, and only for basic benefits.
        applies_to_self_insured=False,
        public_programs=frozenset({MEDICARE_ADVANTAGE}),
        plain_summary=(
            "For Medicare Advantage plans only, and only for Part A and Part B "
            "benefits, not Part D drugs: the plan has to base a medical "
            "necessity decision on the person's own medical history, their "
            "doctor's recommendations and their clinical notes. CMS has told "
            "these plans that an algorithm or AI tool can help with that "
            "review but cannot replace it. An appeal can ask whether any such "
            "tool was used and for the reviewer's clinical reasoning."
        ),
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
        plain_summary=(
            "Plans that are not grandfathered must offer a full and fair "
            "internal appeal and access to an independent external review. "
            "A person can ask for both, and for the documents and clinical "
            "criteria the plan relied on."
        ),
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
        plain_summary=(
            "Massachusetts has ended prior authorization for a defined set of "
            "essential services, including cancer imaging, chronic-disease "
            "medications, maternity care and primary care. For a fully insured "
            "Massachusetts plan, a denied service on that list should be "
            "handled without prior authorization."
        ),
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
        plain_summary=(
            "Someone already approved for a treatment may switch to a "
            "medically equivalent alternative of equal or lower cost without "
            "a new prior authorization. It matters when a plan asks for a new "
            "prior authorization for an equivalent therapy."
        ),
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
        plain_summary=(
            "An AI tool or algorithm may not be the only basis for denying, "
            "delaying or changing care. A licensed provider must make any "
            "medical-necessity denial, the tool must account for the person's "
            "own clinical condition (not just group data), and the denial "
            "notice must give the credentials, board certifications and "
            "specialty of the provider who oversaw the decision. An appeal can "
            "ask for that reviewer's clinical reasoning and credentials, and "
            "for confirmation that AI was not the only basis for the denial."
        ),
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
        plain_summary=(
            "Only a licensed health care professional may make a "
            "medical-necessity denial, and the insurer must report to the "
            "state Insurance Commissioner every quarter whether it used AI in "
            "its denials. An appeal can ask for confirmation that a licensed "
            "professional, not an algorithm, made the decision, and that the "
            "insurer is reporting its AI use as the law requires."
        ),
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
        plain_summary=(
            "An AI tool or algorithm may not be the only basis for downcoding "
            "a claim (changing a billed code to a lower-paying one) without "
            "review by a qualified health professional. It matters when a "
            "claim was downcoded or a service moved to a lower level, and an "
            "appeal can ask for confirmation that a qualified person reviewed "
            "the change."
        ),
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
        plain_summary=(
            "An AI tool or algorithm may not be the only basis for a "
            "utilization-review denial; a qualified person must review it. It "
            "may apply only to plans issued, delivered or renewed on or after "
            "January 1, 2027, so it is worth checking when the plan was issued "
            "or renewed."
        ),
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
        plain_summary=(
            f"{state_name} law says AI or an algorithm may not be the only "
            "basis for a medical-necessity or utilization-review denial, and a "
            "qualified human clinician must make the decision. An appeal can "
            "ask for confirmation that a human reviewer looked at the person's "
            "full clinical picture."
        ),
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


def applicable_hooks(
    state: Optional[str],
    programs: Iterable[str] = (),
    self_insured: Optional[bool] = None,
    as_of: Optional[datetime.date] = None,
) -> list[RegulatoryHook]:
    """The federal hooks in force, then the state's own hooks in force,
    narrowed to what reaches the plan.

    ``programs`` is classify_plan()'s result and ``self_insured`` is
    self_insured_from()'s. Shared by the appeal prompt's regulatory block
    and the public MCP server, so the two cannot disagree about which laws
    reach a plan. With no state (or one we cannot read), only the federal
    hooks are considered.
    """
    abbr = _normalize_state(state)
    today = as_of or datetime.date.today()
    hooks = [h for h in FEDERAL_HOOKS if _hook_in_effect(h, today)]
    if abbr:
        hooks += [
            h
            for h in STATE_HOOKS
            if h.jurisdiction == abbr and _hook_in_effect(h, today)
        ]
    keys = set(programs)
    public = keys & PUBLIC_PROGRAMS
    # Only public coverage: keep the rules written for the program. Public
    # AND private coverage (a Medicare Advantage member with an employer
    # plan too): we do not know which plan denied, so the private plan's
    # rules stay, with a sentence saying whom they reach, the same
    # tie-break the plan-law block uses (review).
    public_only = bool(public) and not keys & PRIVATE_COVERAGE
    if public_only:
        hooks = [h for h in hooks if h.public_programs & public]
    else:
        # Drop rules written only for a public program the patient doesn't
        # have (none known, or Original Medicare beside an employer plan).
        hooks = [
            h
            for h in hooks
            if not h.public_programs
            or h.federal_marketplace
            or h.public_programs & public
        ]
    if self_insured is True:
        hooks = [h for h in hooks if h.applies_to_self_insured]
    return hooks


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
    if not any(
        h.jurisdiction == abbr and _hook_in_effect(h, today) for h in STATE_HOOKS
    ):
        return None

    # A set first: programs may be a one-shot iterable, and it is read twice.
    keys = set(programs)
    hooks = applicable_hooks(
        abbr, programs=keys, self_insured=self_insured, as_of=today
    )
    if not hooks:
        return None
    public = keys & PUBLIC_PROGRAMS
    public_only = bool(public) and not keys & PRIVATE_COVERAGE

    bullet_lines = "\n".join(f"- {h.name} ({h.effective}): {h.summary}" for h in hooks)

    label = (
        next(PROGRAM_LABELS[k] for k in _PROGRAM_ORDER if k in public)
        if public
        else None
    )
    if public_only:
        caveat = (
            f"Note: this is {label} coverage. State insurance mandates and the "
            "ACA appeal rules do not bind it; only the federal rules written "
            "for the program are listed, so rely on those and on the program's "
            "own appeal process."
        )
    elif public:
        caveat = (
            "Note: the state laws listed above generally apply to fully-insured "
            "plans; self-insured (ERISA) employer plans are typically exempt, so "
            "confirm the plan type before relying on a state mandate. More than "
            f"one coverage source was given: the state and ACA items above reach "
            f"the private plan, not the {label} coverage, so apply them only if "
            "the denial concerns the private plan."
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

_ERISA_TEXT = (
    "This looks like a private employer or union plan, so ERISA governs the "
    "appeal (ERISA section 503, 29 U.S.C. § 1133, and the claims-procedure "
    "rule at 29 C.F.R. § 2560.503-1). The plan must give the specific reasons "
    "for the denial and the plan provisions it relied on, must provide on "
    "request the internal rule or clinical criteria it applied and identify "
    "any medical expert it consulted, and must give a full and fair review by "
    "someone who did not make the original decision. If the plan is not "
    "grandfathered and the denial turns on medical judgment (medical "
    "necessity, appropriateness, level of care, or an experimental label) or "
    "is a rescission, {who} is also owed an independent external review "
    "(45 C.F.R. § 147.136, applied to group plans by 29 C.F.R. § "
    "2590.715-2719); a denial for ineligibility is not. ERISA does not apply "
    "to government or church employer plans (29 U.S.C. § 1003(b))."
)
_TPA_ONLY_FACTS = (
    "The carrier administers self-funded employer plans as a third-party "
    "administrator, so this is most likely a self-funded plan. If the "
    "employer is a private company or a union, ERISA governs the appeal "
    "(ERISA section 503, 29 U.S.C. § 1133, and 29 C.F.R. § 2560.503-1: the "
    "specific reasons, the criteria relied on, and a full and fair review). "
    "If the employer is a government or a church, ERISA does not apply "
    "(29 U.S.C. § 1003(b)); the plan's own appeal terms apply, and for a "
    "non-grandfathered plan the ACA internal appeal and external review rules "
    "(45 C.F.R. § 147.136) as well."
)
_OTHER_GROUP_FACTS = (
    "This is group coverage that is not clearly an employer or union plan. If "
    "a private employer or union sponsors it, ERISA governs (29 C.F.R. § "
    "2560.503-1); a government or church employer's plan is exempt "
    "(29 U.S.C. § 1003(b)); and if it is an association or membership plan, "
    "ERISA may not apply, and for non-grandfathered coverage the ACA internal "
    "appeal and external review rules (45 C.F.R. § 147.136) apply along with "
    "state insurance law."
)
_ACA_MARKETPLACE_TEXT = (
    "This is marketplace (Affordable Care Act) coverage. If it is an "
    "individual plan bought on the marketplace, ERISA does not apply; the "
    "plan owes an internal appeal and, for a denial that turns on medical "
    "judgment or is a rescission, an independent external review under the "
    "ACA (45 C.F.R. § 147.136), and it must cover the ten essential health "
    "benefit categories (42 U.S.C. § 18022){say_so}. If it is small employer "
    "(SHOP) coverage, it is an "
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
_MEDICARE_ADVANTAGE_TEXT = (
    "This is a Medicare Advantage plan. Neither ERISA nor the ACA appeal rules "
    "apply. A denial of a medical service or item follows the Medicare "
    "Advantage organization determination and reconsideration process "
    "(42 C.F.R. Part 422, Subpart M), and the plan must itself forward an "
    "upheld denial to the independent review entity. A denial of a "
    "prescription drug under the plan's Part D benefit follows the Part D "
    "process instead (42 C.F.R. Part 423, Subpart M), where after the plan's "
    "redetermination {who} must ask the independent review entity for "
    "reconsideration themselves."
)
_MEDICARE = (
    "This is Original Medicare. Neither ERISA nor the ACA appeal rules apply. "
    "A claim denial follows the Medicare redetermination and reconsideration "
    "process (42 C.F.R. Part 405, Subpart I); a prescription drug denial "
    "under a Part D plan follows the Part D process (42 C.F.R. Part 423, "
    "Subpart M)."
)
_MEDICAID_TEXT = (
    "This is a Medicaid plan. Neither ERISA nor the ACA appeal rules apply; a "
    "managed care plan owes an internal appeal (42 C.F.R. Part 438, Subpart "
    "F) and {who} has the right to a state fair hearing (42 C.F.R. "
    "Part 431, Subpart E)."
)
_VA_FACTS = (
    "This is Veterans Affairs coverage. Neither ERISA nor the ACA appeal "
    "rules apply. VA has its own processes: a clinical appeal for a decision "
    "about treatment, and a benefits decision review for a claim such as "
    "reimbursement of care outside VA."
)
_UNKNOWN_TEXT = (
    "We do not know how {who} gets this coverage. If the denial letter "
    "or the plan documents show it is a private employer or union plan, "
    "ERISA's claims-procedure rule (29 C.F.R. § 2560.503-1) most likely "
    "applies; if they show it is a marketplace plan or other non-grandfathered "
    "individual coverage, the ACA internal appeal and external review rules "
    "(45 C.F.R. § 147.136) apply to a denial that turns on medical judgment."
)
_TWO_SOURCES_FACT = "More than one coverage source was given."
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

_LETTER_CONFLICT_FACT = (
    "The denial letter mentions ERISA appeal rights, but the coverage source "
    "given is one that ERISA does not govern."
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


@dataclass(frozen=True)
class _PlanLawWording:
    """One audience's words for the APPLICABLE APPEAL LAW paragraphs. The
    facts and citations are shared; only who is named and whether the
    letter-drafting instructions are added differ."""

    paragraphs: dict[str, str]
    tpa_only: str
    unknown: str
    two_sources: str
    letter_conflict: str


# The appeal prompt: a model drafting a letter for the patient.
_FOR_PROMPT = _PlanLawWording(
    paragraphs={
        ERISA: _ERISA_TEXT.format(who="the patient"),
        OTHER_GROUP: _OTHER_GROUP_FACTS
        + " Name ERISA only if the denial letter or the plan documents show a "
        "private employer or union sponsor.",
        MARKETPLACE: _ACA_MARKETPLACE_TEXT.format(
            say_so=", so if the service falls in one of them, say so"
        ),
        GOVERNMENT: _GOVERNMENT_EMPLOYER,
        FEHB: _FEDERAL_EMPLOYER,
        MEDICARE_ADVANTAGE: _MEDICARE_ADVANTAGE_TEXT.format(who="the patient"),
        MEDICARE: _MEDICARE,
        MEDICAID: _MEDICAID_TEXT.format(who="the patient"),
        VA: _VA_FACTS + " Argue the medical case and do not cite either law.",
    },
    tpa_only=_TPA_ONLY_FACTS
    + " Use whichever the denial letter or the plan documents support.",
    unknown=_UNKNOWN_TEXT.format(who="the patient")
    + " Name one of them only when the letter itself supports it; otherwise "
    "ask for the plan's internal appeal and, where the denial is about medical "
    "judgment, an independent external review in plain words rather than "
    "naming a statute.",
    two_sources=_TWO_SOURCES_FACT + " Use the one the denial letter itself supports.",
    letter_conflict=_LETTER_CONFLICT_FACT
    + " Follow the letter only if the plan documents confirm a private "
    "employer or union plan; otherwise rely on the process described above "
    "and do not cite ERISA.",
)

# The public MCP server: an assistant explaining a plan's appeal law to the
# person themselves, so no drafting instructions and no "the patient".
_FOR_PERSON = _PlanLawWording(
    paragraphs={
        ERISA: _ERISA_TEXT.format(who="the person"),
        OTHER_GROUP: _OTHER_GROUP_FACTS,
        MARKETPLACE: _ACA_MARKETPLACE_TEXT.format(say_so=""),
        GOVERNMENT: _GOVERNMENT_EMPLOYER,
        FEHB: _FEDERAL_EMPLOYER,
        MEDICARE_ADVANTAGE: _MEDICARE_ADVANTAGE_TEXT.format(who="the person"),
        MEDICARE: _MEDICARE,
        MEDICAID: _MEDICAID_TEXT.format(who="the person"),
        VA: _VA_FACTS,
    },
    tpa_only=_TPA_ONLY_FACTS
    + " The denial letter or the plan documents show which applies.",
    unknown=_UNKNOWN_TEXT.format(who="the person")
    + " The denial letter or the plan documents show which one applies.",
    two_sources=_TWO_SOURCES_FACT
    + " The denial letter shows which plan made the decision, and that plan's "
    "rules apply.",
    letter_conflict=_LETTER_CONFLICT_FACT
    + " ERISA applies only if the plan documents confirm a private employer "
    "or union plan; otherwise the process described above applies.",
)

_PROGRAM_ORDER: tuple[str, ...] = (MEDICARE_ADVANTAGE, MEDICARE, MEDICAID, VA, FEHB)
PROGRAM_LABELS: dict[str, str] = {
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
    body = "\n".join(
        plan_law_paragraphs(
            plan_sources, is_tpa=is_tpa, regulator_alt_name=regulator_alt_name
        )
    )
    return f"{PLAN_LAW_HEADER}: {body}\n{_INVITATION}"


def plan_law_paragraphs(
    plan_sources: Iterable[str],
    is_tpa: bool = False,
    regulator_alt_name: Optional[str] = None,
    for_person: bool = False,
) -> list[str]:
    """The paragraphs of the APPLICABLE APPEAL LAW block, without its header
    or the closing invitation to cite. Never empty. ``for_person`` gives the
    public MCP server's version: the same facts and citations, worded for the
    person rather than as instructions for drafting a letter."""
    wording = _FOR_PERSON if for_person else _FOR_PROMPT
    programs = classify_plan(
        plan_sources, is_tpa=is_tpa, regulator_alt_name=regulator_alt_name
    )
    sources = [k for k in programs if k in wording.paragraphs]
    paragraphs: list[str] = []
    conflict: Optional[str] = None
    # The denial letter naming ERISA rights is the strongest signal we have,
    # except against coverage ERISA cannot govern: there the source wins and
    # the conflict is named (review).
    if ERISA_LETTER in programs:
        if any(k in EXCLUSIVE_OF_ERISA for k in sources):
            conflict = wording.letter_conflict
        elif ERISA not in sources:
            paragraphs.append(wording.paragraphs[ERISA])
    for k in sources:
        if wording.paragraphs[k] not in paragraphs:
            paragraphs.append(wording.paragraphs[k])
    # A TPA administers self-funded plans, and a self-funded plan can belong
    # to a city as easily as to a company, so the flag alone does not make it
    # ERISA (review). With a plan source, the source decides; without one,
    # the hedged self-funded paragraph.
    if TPA in programs and not paragraphs:
        paragraphs.append(wording.tpa_only)
    if not paragraphs:
        paragraphs.append(wording.unknown)
    elif len(paragraphs) > 1:
        paragraphs.append(wording.two_sources)
    if conflict is not None:
        paragraphs.append(conflict)
    return paragraphs


# ---------------------------------------------------------------------------
# Emergency care and surprise billing, chosen by what the denial is about.
#
# The hooks above are keyed off the state. These are federal rules keyed off
# the facts of the denial instead: emergency care, an air ambulance, or an
# out-of-network clinician at an in-network facility. Like the plan-law
# block, the selection is by category (the rule applies to this kind of care
# under this kind of plan), never by matching words against document titles.
# Every entry cites the current section by number, checked against eCFR; the
# pre-2022 emergency rule in 45 C.F.R. 147.138(b) only governs plan years that
# began before January 1, 2022, so it is not used.
# ---------------------------------------------------------------------------

EMERGENCY_CARE = "emergency_care"
AIR_AMBULANCE = "air_ambulance"
OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY = "out_of_network_at_in_network_facility"

# HCPCS Level II air ambulance codes: rotary-wing and fixed-wing base rates
# and their mileage. CMS-owned codes, not CPT.
AIR_AMBULANCE_CODES = frozenset({"A0430", "A0431", "A0435", "A0436"})
# CPT emergency department visit codes.
EMERGENCY_DEPARTMENT_CODES = frozenset({"99281", "99282", "99283", "99284", "99285"})

_EMERGENCY_RE = re.compile(
    r"\bemergency (?:room|department|services?|care|treatment|visit|admission|"
    r"surgery|claim)\b|\b(?:ER|ED) (?:visit|services?|admission|claim|bill)\b",
    re.I,
)
_AIR_AMBULANCE_RE = re.compile(
    r"\bair ambulance\b|\bair medical (?:transport|services?)\b|"
    r"\b(?:helicopter|rotary[- ]wing|fixed[- ]wing) (?:ambulance|transport)\b|"
    r"\bmedevac\b|\blife ?flight\b",
    re.I,
)
_OUT_OF_NETWORK_RE = re.compile(
    r"\bout[- ]of[- ]network\b|\bnon[- ]?participating\b|\bnon[- ]?network\b|"
    r"\bnon[- ]?contracted\b",
    re.I,
)
# Never matches inside "non-participating" or "non-contracted"; "in-network"
# cannot occur inside "out-of-network".
_IN_NETWORK_FACILITY_RE = re.compile(
    r"\b(?:in[- ]network|(?<![-\w])(?<!non )participating|"
    r"(?<![-\w])(?<!non )contracted) "
    r"(?:hospital|facility|surgery center|surgical center|"
    r"ambulatory surgical center)\b",
    re.I,
)

# The term itself negated, and only then. Before it: "no", "not", "never" or
# "without" right in front, an article allowed ("no ER visit", "not an air
# ambulance"); "did not involve" or "did not receive" in front ("the claim
# did not involve an air ambulance"); or "non-" fixed to it ("non-emergency
# services"). After it: the care said not to have happened, directly after the
# term with no other words between ("an ER visit did not occur", "air
# ambulance was not used", "in-network hospital was not involved"). A
# negation that belongs to another phrase does not count, so "no prior
# authorization for emergency services" and "emergency services: prior
# authorization was not provided" still name emergency services, and a
# non-emergency air ambulance is still an air ambulance.
_NEGATED_BEFORE_RE = re.compile(
    r"(?:\b(?:no|not|never|without)\s+(?:(?:an?|the|any)\s+)?|"
    r"\b(?:did not|didn't|does not|doesn't|never) (?:involve|involved|receive|"
    r"received)\s+(?:(?:an?|the|any)\s+)?|\bnon[- ]?)$",
    re.I,
)
_NEGATED_AFTER_RE = re.compile(
    r"^\s+(?:was|were|is|are|did|does|has|had)\s+(?:not|never)\s+"
    r"(?:be(?:en)?\s+)?(?:occur|occurred|happen|happened|used|involved|provided|"
    r"furnished|performed|take place|taken place|given)\b|"
    r"^\s+(?:wasn't|weren't|didn't|never)\s+"
    r"(?:occur|occurred|happen|happened|used|involved|provided|furnished|"
    r"performed|take place|given)\b",
    re.I,
)
# Words that tie a mention to this person's claim when they appear within
# CLAIM_WINDOW_WORDS words of it in the same sentence.
_CLAIM_WORD_RE = re.compile(
    r"\b(?:denied|deny|denies|denial|claims?|billed|bill|bills|charged|charges?)\b",
    re.I,
)
CLAIM_WINDOW_WORDS = 12
# How close the out-of-network phrase and the participating facility phrase
# must be, in words between them, for the facility case.
FACILITY_PAIR_WINDOW_WORDS = 8
# A sentence ends at ".", "!" or "?" followed by a space and a capital letter,
# or at the end of the text, except after these abbreviations, so "Dr. Smith"
# or "the U.S. Code" never ends one.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[A-Z])")
_ABBREVIATIONS = frozenset(
    {"dr.", "u.s.", "st.", "inc.", "e.g.", "i.e.", "no.", "mr.", "ms.", "mrs."}
)


def _normalize(text: str) -> str:
    """Collapse runs of whitespace, newlines included, so a term broken
    across a line still matches."""
    return re.sub(r"\s+", " ", text).strip()


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Start and end offsets of each sentence in ``text``."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        words = text[start : m.end()].split()
        if words and words[-1].lower() in _ABBREVIATIONS:
            continue
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def _sentence_of(spans: list[tuple[int, int]], pos: int) -> tuple[int, int]:
    return next(((a, b) for a, b in spans if a <= pos < b), (0, 0))


def _negated(text: str, m: re.Match[str]) -> bool:
    before = text[max(0, m.start() - 40) : m.start()]
    after = text[m.end() : m.end() + 80]
    return bool(_NEGATED_BEFORE_RE.search(before) or _NEGATED_AFTER_RE.search(after))


def _mentions(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """The mentions of ``pattern`` in ``text`` whose term is not negated."""
    return [m for m in pattern.finditer(text) if not _negated(text, m)]


def _words_between(text: str, a: re.Match[str], b: re.Match[str]) -> int:
    first, second = (a, b) if a.start() <= b.start() else (b, a)
    return len(text[first.end() : second.start()].split())


def _claim_word_near(
    text: str, spans: list[tuple[int, int]], start: int, end: int
) -> bool:
    """Whether a claim word sits within CLAIM_WINDOW_WORDS words of the span
    [start, end), without crossing the sentence it is in."""
    s_start, s_end = _sentence_of(spans, start)
    left = text[s_start:start].split()[-CLAIM_WINDOW_WORDS:]
    right = text[end:s_end].split()[:CLAIM_WINDOW_WORDS]
    return bool(_CLAIM_WORD_RE.search(" ".join(left + [text[start:end]] + right)))


def _in_claim_context(pattern: re.Pattern[str], text: str) -> bool:
    """Whether the denial text mentions the care, not negated, within
    CLAIM_WINDOW_WORDS words of a claim word (denied, denial, claim, billed,
    charged) in the same sentence. A line with no claim word, such as "your
    plan covers air ambulance services", or a policy sentence after the
    sentence that names the claim, does not count; the model's "Cite only
    if" gate covers the cases this simple rule misjudges.
    """
    spans = _sentence_spans(text)
    return any(
        _claim_word_near(text, spans, m.start(), m.end())
        for m in _mentions(pattern, text)
    )


def _facility_case(text: str, need_claim_word: bool) -> bool:
    """An out-of-network phrase and a participating facility phrase within
    FACILITY_PAIR_WINDOW_WORDS words of each other, both affirmed, and, in
    the denial text, a claim word near them in the same sentence."""
    spans = _sentence_spans(text)
    for o in _mentions(_OUT_OF_NETWORK_RE, text):
        for f in _mentions(_IN_NETWORK_FACILITY_RE, text):
            if _words_between(text, o, f) > FACILITY_PAIR_WINDOW_WORDS:
                continue
            if _sentence_of(spans, o.start()) != _sentence_of(spans, f.start()):
                continue
            start, end = min(o.start(), f.start()), max(o.end(), f.end())
            if not need_claim_word or _claim_word_near(text, spans, start, end):
                return True
    return False


def surprise_billing_facts(
    denial_text: Optional[str] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    codes: Iterable[str] = (),
) -> frozenset[str]:
    """Which of the emergency and surprise-billing situations the denial is
    about: EMERGENCY_CARE, AIR_AMBULANCE, OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY.

    In order: a billing code, or the procedure (the service denied) naming
    the care; otherwise the denial text naming it near a claim word in the
    same sentence. The facility case needs an out-of-network phrase and a
    participating facility phrase within FACILITY_PAIR_WINDOW_WORDS words of
    each other, in the procedure, or in the denial text with a claim word
    near them in the same sentence. A mention whose term is negated
    never counts. This only decides whether the block is offered: it says
    the rules MAY apply, and each rule's "Cite only if" line tells the model
    when not to cite it. The diagnosis is not read, since it does not say
    where care was given.
    """
    del diagnosis  # accepted for a uniform call shape; see the docstring
    service = _normalize(procedure or "")
    letter = _normalize(denial_text or "")
    code_set = {c.strip().upper() for c in codes if c}
    facts: set[str] = set()
    if (
        code_set & AIR_AMBULANCE_CODES
        or _mentions(_AIR_AMBULANCE_RE, service)
        or _in_claim_context(_AIR_AMBULANCE_RE, letter)
    ):
        facts.add(AIR_AMBULANCE)
    if (
        code_set & EMERGENCY_DEPARTMENT_CODES
        or _mentions(_EMERGENCY_RE, service)
        or _in_claim_context(_EMERGENCY_RE, letter)
    ):
        facts.add(EMERGENCY_CARE)
    if _facility_case(service, need_claim_word=False) or _facility_case(
        letter, need_claim_word=True
    ):
        facts.add(OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY)
    return frozenset(facts)


@dataclass(frozen=True)
class SurpriseBillingRule:
    """One federal rule for a kind of care, with the plans it reaches.

    ``summary`` is what the rule requires, worded so a letter can use it: it
    never offers the plan a way out. ``cite_when`` is for the model only:
    when the rule fits and when it does not, so its limits are applied
    rather than written into the letter.
    """

    fact: str  # the surprise_billing_facts() value that triggers it
    name: str
    summary: str
    cite_when: str
    effective: str
    # Which coverage the rule reaches, in words, rendered with the rule so
    # the letter (and the person) can see whether it fits their plan.
    reaches: str
    source_url: str  # documentation only; not rendered into the prompt
    # Whether the rule reaches the plans the No Surprises Act covers: group
    # health plans (employer plans, self-funded ones included, and state and
    # local government plans, which the PHS Act counts as non-federal
    # governmental plans) and individual coverage. FEHB plans are reached
    # through 5 U.S.C. 8902(p), which applies PHS Act 2799A-1, 2799A-2 and
    # 2799A-7 and the provider rules in 2799B to FEHB enrollees; those are
    # listed in public_programs. It does not reach Medicare, Medicaid or VA
    # care, which have their own emergency rules below where they exist.
    nsa_plans: bool
    public_programs: frozenset[str] = frozenset()
    # The Labor regulation and the tax code section for the rule, which are
    # what an employer plan answers to under ERISA and the tax code. The tax
    # code is cited by statute: Treasury's version is a 2021 temporary
    # regulation whose current status is unsettled. Rendered for an employer,
    # TPA or unknown plan, since the model may not add sections of its own.
    employer_plan_sections: str = ""


_NSA_REACHES = (
    "employer plans (including self-funded ones), individual plans including "
    "marketplace plans, state and local government plans, and FEHB plans; not "
    "short-term limited-duration insurance, excepted benefits, or account-based "
    "plans such as HRAs, and not Original Medicare, Medicare Advantage, "
    "Medicaid or VA care"
)

_NO_OUT_OF_NETWORK_BENEFITS = (
    "A plan's lack of out-of-network benefits is not an exclusion of these " "services."
)

SURPRISE_BILLING_RULES: tuple[SurpriseBillingRule, ...] = (
    SurpriseBillingRule(
        fact=EMERGENCY_CARE,
        name=(
            "No Surprises Act emergency services rule (Public Health Service "
            "Act § 2799A-1(a); 45 C.F.R. § 149.110)"
        ),
        summary=(
            "Emergency services must be covered without prior authorization, "
            "whether or not the hospital or clinician is in network, with cost "
            "sharing no higher than in network and no administrative "
            "requirement stricter than for in-network emergency care. "
            f"{_NO_OUT_OF_NETWORK_BENEFITS} An emergency medical condition, "
            "including a mental health condition or substance use disorder, is "
            "one with acute symptoms severe enough (including severe pain) that "
            "a prudent layperson with an average knowledge of health and "
            "medicine could reasonably expect the absence of immediate medical "
            "attention to place the person's health (or a pregnant woman's or "
            "her unborn child's) in serious jeopardy, seriously impair bodily "
            "functions, or cause serious dysfunction of a bodily organ or part. "
            "The plan may not decide that solely from diagnosis codes."
        ),
        cite_when=(
            "the plan covers any emergency department services, as nearly all "
            "plans do. Emergency services is a defined term: the screening exam "
            "and the treatment needed to stabilize the person, and also covered "
            "care from out-of-network providers after stabilization during the "
            "same visit, unless the provider met the notice-and-consent "
            "conditions of 45 C.F.R. 149.410(b). The rule leaves "
            "only these other plan terms in place: an exclusion or coordination "
            "of benefits that is not inconsistent with emergency coverage, an "
            "affiliation or waiting period, and normal cost sharing. A missing "
            "prior authorization or a lack of out-of-network benefits is never a "
            "reason not to cite it."
        ),
        effective="plan years beginning on or after January 1, 2022",
        reaches=_NSA_REACHES,
        source_url="https://www.ecfr.gov/current/title-45/section-149.110",
        nsa_plans=True,
        public_programs=frozenset({FEHB}),
        employer_plan_sections="29 C.F.R. § 2590.716-4 and 26 U.S.C. § 9816(a)",
    ),
    SurpriseBillingRule(
        fact=EMERGENCY_CARE,
        name="Medicare Advantage emergency services rule (42 C.F.R. § 422.113(b))",
        summary=(
            "A Medicare Advantage plan is financially responsible for "
            "emergency services whether they were obtained inside or outside "
            "the plan's network and whether or not there was prior "
            "authorization, judged by the prudent layperson definition of an "
            "emergency medical condition regardless of the final diagnosis."
        ),
        cite_when="the denial is from a Medicare Advantage plan.",
        effective="in force",
        reaches="Medicare Advantage plans only",
        source_url="https://www.ecfr.gov/current/title-42/section-422.113",
        nsa_plans=False,
        public_programs=frozenset({MEDICARE_ADVANTAGE}),
    ),
    SurpriseBillingRule(
        fact=EMERGENCY_CARE,
        name="Medicaid managed care emergency services rule (42 C.F.R. § 438.114)",
        summary=(
            "A Medicaid managed care plan must cover and pay for emergency "
            "services whether or not the provider has a contract with the plan, "
            "may not deny payment when the enrollee had an emergency medical "
            "condition under the prudent layperson definition, and may not limit "
            "what counts as an emergency by lists of diagnoses or symptoms."
        ),
        cite_when="the denial is from a Medicaid managed care plan.",
        effective="in force",
        reaches="Medicaid managed care plans only",
        source_url="https://www.ecfr.gov/current/title-42/section-438.114",
        nsa_plans=False,
        public_programs=frozenset({MEDICAID}),
    ),
    SurpriseBillingRule(
        fact=AIR_AMBULANCE,
        name=(
            "No Surprises Act air ambulance rule (Public Health Service Act "
            "§ 2799A-2; 45 C.F.R. § 149.130)"
        ),
        summary=(
            "Cost sharing for an out-of-network air ambulance must be the same "
            "as for an in-network one, and counts toward the in-network "
            f"deductible and out-of-pocket maximum. {_NO_OUT_OF_NETWORK_BENEFITS}"
        ),
        cite_when=(
            "the plan covers air ambulance services at all, in network or out. "
            "It sets cost sharing for covered transport; it does not override "
            "the plan's own decision on whether the transport was medically "
            "necessary, so if that is the reason for the denial, argue necessity "
            "and use this rule only for the cost sharing. It does not apply to "
            "ground ambulances."
        ),
        effective="plan years beginning on or after January 1, 2022",
        reaches=_NSA_REACHES,
        source_url="https://www.ecfr.gov/current/title-45/section-149.130",
        nsa_plans=True,
        public_programs=frozenset({FEHB}),
        employer_plan_sections="29 C.F.R. § 2590.717-1 and 26 U.S.C. § 9817",
    ),
    SurpriseBillingRule(
        fact=OUT_OF_NETWORK_AT_IN_NETWORK_FACILITY,
        name=(
            "No Surprises Act rule for out-of-network clinicians at participating "
            "facilities (Public Health Service Act § 2799A-1(b); 45 C.F.R. "
            "§ 149.120)"
        ),
        summary=(
            "Non-emergency care from an out-of-network clinician at a "
            "participating health care facility (a hospital, hospital outpatient "
            "department, critical access hospital or ambulatory surgical center) "
            "must be covered with cost sharing no higher than in network, unless "
            "the clinician satisfied the federal notice-and-consent criteria in "
            "45 C.F.R. § 149.420(c) through (i). Those criteria can never apply "
            "to ancillary services (emergency medicine, anesthesiology, "
            "pathology, radiology and neonatology; assistant surgeons, "
            "hospitalists and intensivists; and diagnostic services including "
            "radiology and laboratory services), to care no participating "
            "provider at the facility could furnish, or to unforeseen, urgent "
            "needs that arise during the visit (45 C.F.R. § 149.420(b)). "
            f"{_NO_OUT_OF_NETWORK_BENEFITS}"
        ),
        cite_when=(
            "the plan covers the service itself. Section 149.420 binds the "
            "clinician, not the plan: use it only for what makes consent valid, "
            "never as a rule the plan broke."
        ),
        effective="plan years beginning on or after January 1, 2022",
        reaches=_NSA_REACHES,
        source_url="https://www.ecfr.gov/current/title-45/section-149.120",
        nsa_plans=True,
        public_programs=frozenset({FEHB}),
        employer_plan_sections="29 C.F.R. § 2590.716-5 and 26 U.S.C. § 9816(b)",
    ),
)

SURPRISE_BILLING_HEADER = "EMERGENCY AND SURPRISE BILLING RULES"

# Plan sources the No Surprises Act reaches: employer, other group,
# marketplace and individual plans, and state or local government plans
# (non-federal governmental plans). Not all of them are private, so the
# caveats below say "private or state or local government plan". A TPA carrier
# or an ERISA mention in the letter also points to an employer plan, but next
# to a known public program neither is enough to say the denial came from one.
_NSA_PLAN_SOURCES = PRIVATE_COVERAGE
_EMPLOYER_SIGNALS = frozenset({ERISA, TPA, ERISA_LETTER})

_UNKNOWN_PLAN_CAVEAT = (
    "Note: the plan type is not known. The No Surprises Act rules above reach "
    "most employer, individual, and state and local government plans and FEHB "
    "plans, not short-term limited-duration plans, Original Medicare, Medicare "
    "Advantage, Medicaid or VA care, so cite one only if the plan is one it "
    "reaches."
)
_MORE_THAN_ONE_SOURCE_CAVEAT = (
    "Note: coverage from more than one source was given. The No Surprises Act "
    "rules apply only if the plan that denied this is a private or state or "
    "local government plan, or an FEHB plan; the program's own rule applies to "
    "the other coverage."
)


def applicable_surprise_billing_rules(
    facts: Iterable[str], programs: Iterable[str] = ()
) -> list[SurpriseBillingRule]:
    """The rules for the denial's facts that reach the plan.

    ``programs`` is classify_plan()'s result. With known public coverage and
    no plan source the No Surprises Act reaches, only the rules written for
    that program (FEHB's include the act's), so Original Medicare and VA get
    none and a TPA flag beside Medicaid does not bring in the act's rules.
    With no plan information, the act's rules, which the context block
    renders with an unknown-plan caveat.
    """
    fact_set = set(facts)
    keys = set(programs)
    public = keys & PUBLIC_PROGRAMS
    public_only = bool(public) and not keys & _NSA_PLAN_SOURCES
    rules: list[SurpriseBillingRule] = []
    for rule in SURPRISE_BILLING_RULES:
        if rule.fact not in fact_set:
            continue
        if public_only:
            if rule.public_programs & public:
                rules.append(rule)
        elif rule.nsa_plans or rule.public_programs & public:
            rules.append(rule)
    return rules


def _render_rule(rule: SurpriseBillingRule, employer_note: Optional[str]) -> str:
    line = f"- {rule.name} ({rule.effective}; reaches {rule.reaches}): {rule.summary}"
    if employer_note and rule.employer_plan_sections:
        line += (
            f" {employer_note}, the same rule is also at "
            f"{rule.employer_plan_sections}."
        )
    return f"{line}\n  Cite only if {rule.cite_when}"


def get_surprise_billing_citation_context(
    denial_text: Optional[str] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    codes: Iterable[str] = (),
    programs: Iterable[str] = (),
) -> Optional[str]:
    """The emergency and surprise-billing block for the appeal prompt, or
    None when the denial is about none of those situations or no rule
    reaches the plan. Each rule names the coverage it reaches and when to
    cite it; with no plan information, or public and private coverage
    together, a caveat says so."""
    facts = surprise_billing_facts(denial_text, procedure, diagnosis, codes)
    if not facts:
        return None
    keys = set(programs)
    rules = applicable_surprise_billing_rules(facts, keys)
    if not rules:
        return None
    # FEHB is a public program the No Surprises Act does reach, so it counts
    # with the act's plans, not with the programs that have their own rules.
    public = keys & PUBLIC_PROGRAMS - {FEHB}
    nsa_sources = keys & (_NSA_PLAN_SOURCES | {FEHB})
    unknown = not keys & (_NSA_PLAN_SOURCES | _EMPLOYER_SIGNALS | PUBLIC_PROGRAMS)
    # The Labor regulation and the tax code section bind private employer
    # plans only: ERISA and the tax code leave out governmental plans and
    # FEHB. Beside a government or FEHB source the sections are offered only
    # conditionally.
    if unknown:
        employer_note: Optional[str] = "For an employer plan"
    elif keys & _EMPLOYER_SIGNALS and not keys & {GOVERNMENT, FEHB}:
        employer_note = "For an employer plan"
    elif keys & _EMPLOYER_SIGNALS:
        employer_note = "If your plan is a private employer plan"
    else:
        employer_note = None
    lines = "\n".join(_render_rule(r, employer_note) for r in rules)
    block = (
        f"{SURPRISE_BILLING_HEADER}: The denial appears to involve emergency "
        "care, an air ambulance, or an out-of-network clinician at a "
        "participating facility. These federal rules MAY apply. Each rule's "
        '"Cite only if" line is for you, not for the letter: cite a rule by '
        "name only when the facts of this denial and the plan fit it, and do "
        "not invent section numbers, dates, or quotations beyond what is "
        f"provided here.\n{lines}"
    )
    if unknown:
        block = f"{block}\n{_UNKNOWN_PLAN_CAVEAT}"
    elif public and nsa_sources and any(r.nsa_plans for r in rules):
        block = f"{block}\n{_MORE_THAN_ONE_SOURCE_CAVEAT}"
    return block

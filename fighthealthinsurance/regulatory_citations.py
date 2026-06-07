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

from dataclasses import dataclass
from typing import Optional


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
    ("WA", "Washington"),
    ("MD", "Maryland"),
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
}


def _normalize_state(state: Optional[str]) -> Optional[str]:
    """Return a 2-letter state code from a 2-letter code or a full name."""
    if not state:
        return None
    s = state.strip()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _STATE_NAME_TO_ABBR.get(s.lower())


def get_regulatory_citation_context(
    state: Optional[str],
    denial_text: Optional[str] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    self_insured: Optional[bool] = None,
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
    state_hooks = [h for h in STATE_HOOKS if h.jurisdiction == abbr]
    if not state_hooks:
        return None

    hooks = list(FEDERAL_HOOKS) + state_hooks
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

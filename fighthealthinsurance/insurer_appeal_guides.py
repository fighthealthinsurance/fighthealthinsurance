"""
Insurer appeal guide definitions for Fight Health Insurance.

This module provides original, programmatically rendered "how to appeal a
denial from <Insurer>" guides for the largest U.S. health insurers and
pharmacy benefit managers (PBMs).

The content is authored inline as typed dataclasses (mirroring the structure
and validation approach used by ``state_help.py``). Unlike state help data,
which is loaded from a static JSON file, the insurer guide copy is original
editorial content maintained directly in this module.

CONTENT SAFETY
--------------
These guides intentionally avoid insurer-specific phone numbers, mailing
addresses, portal URLs, exact deadlines, and success statistics. Those values
change frequently and vary by plan, so the copy keeps specifics general and
points readers to their own denial letter and plan documents. Everything here
is general information, not legal advice.
"""

from dataclasses import dataclass, field
from functools import lru_cache

# Date the guide content was last editorially reviewed (YYYY-MM-DD).
# Surfaced on every page so readers know how current the general guidance is.
LAST_REVIEWED = "2026-07-10"

# Controlled vocabulary for plan types. Keeping this constrained lets the
# validate function catch typos and keeps the rendered labels consistent.
VALID_PLAN_TYPES = {
    "Commercial",
    "Medicare Advantage",
    "Medicaid",
    "ACA Marketplace",
    "Pharmacy Benefit Manager",
}

# Bounds on the number of FAQ items per insurer. Enforced by validate so pages
# stay substantive without becoming padded.
MIN_FAQS = 4
MAX_FAQS = 6


class InsurerGuideValidationError(ValueError):
    """Raised when an insurer appeal guide configuration is invalid."""

    pass


@dataclass(frozen=True)
class FAQItem:
    """A single question/answer pair rendered into the FAQ section and FAQPage JSON-LD."""

    question: str
    answer: str


@dataclass(frozen=True)
class InsurerGuide:
    """A programmatic appeal guide for a single insurer or PBM.

    Attributes:
        name: Display name of the insurer (e.g. "UnitedHealthcare").
        slug: URL slug (e.g. "unitedhealthcare").
        aliases: Alternate names/abbreviations people search for. Used for
            search-friendly copy; must be unique across all guides.
        plan_types: Plan lines this insurer commonly administers. Values must
            come from ``VALID_PLAN_TYPES``.
        summary: One-line description used on the index grid and meta tags.
        why_denials: General, non-insurer-specific explanation of why claims
            from this insurer commonly get denied.
        overview: General overview of this insurer's appeal process.
        faqs: 4-6 original FAQ items.
        related_slugs: Slugs of other guides to cross-link to (the mesh). Every
            entry must resolve to an existing guide and must not be self.
    """

    name: str
    slug: str
    aliases: tuple[str, ...]
    plan_types: tuple[str, ...]
    summary: str
    why_denials: str
    overview: str
    faqs: tuple[FAQItem, ...]
    related_slugs: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Shared, insurer-agnostic copy helpers.
#
# The step-by-step internal-appeal and external-review process is essentially
# the same regardless of insurer, so the general steps live here and are
# reused by the template. Insurer-specific overview text lives in each guide.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppealStep:
    """A single step in the generic internal-appeal / external-review process."""

    title: str
    body: str


# Generic denial reasons shared across insurers. These are accurate at a
# general level and deliberately avoid insurer-specific claims.
GENERAL_DENIAL_REASONS: tuple[str, ...] = (
    "The insurer decided the service was not medically necessary based on its "
    "own clinical policy.",
    "The treatment was considered experimental, investigational, or not "
    "supported by the plan's coverage criteria.",
    "Prior authorization or a required referral was missing or was not on file "
    "before the service.",
    "The care was billed as out-of-network, or a network or tiering rule "
    "reduced or denied payment.",
    "A coding, documentation, or paperwork mismatch caused an automated denial.",
    "For pharmacy denials, the drug was non-formulary, required step therapy, "
    "or hit a quantity limit.",
)

# Generic step-by-step process: internal appeal followed by external review.
GENERAL_APPEAL_STEPS: tuple[AppealStep, ...] = (
    AppealStep(
        "Read your denial letter closely",
        "Your Explanation of Benefits or denial notice states the specific "
        "reason for the denial and the deadline to appeal. Those details drive "
        "everything else, so start there rather than with a generic template.",
    ),
    AppealStep(
        "Request your plan documents and records",
        "Ask for the clinical criteria used to deny the claim and gather your "
        "medical records, provider notes, and any test results that show the "
        "care was appropriate.",
    ),
    AppealStep(
        "File the internal appeal",
        "Submit a written internal appeal to the insurer that answers the exact "
        "denial reason and attaches supporting evidence. Keep a copy of "
        "everything you send and note how and when you sent it.",
    ),
    AppealStep(
        "Ask for an expedited review if care is urgent",
        "If waiting for a standard decision could seriously harm your health, "
        "you can usually request an expedited (fast) appeal that must be decided "
        "much sooner.",
    ),
    AppealStep(
        "Escalate to an external review",
        "If the internal appeal is denied, most plans give you the right to an "
        "independent external review by a reviewer not employed by the insurer. "
        "Your denial letter and your state insurance department explain how to "
        "request it.",
    ),
)


# ---------------------------------------------------------------------------
# The insurer guides themselves.
# ---------------------------------------------------------------------------

_INSURER_GUIDES: tuple[InsurerGuide, ...] = (
    InsurerGuide(
        name="UnitedHealthcare",
        slug="unitedhealthcare",
        aliases=("UHC", "United Healthcare", "United Health Care", "UnitedHealth"),
        plan_types=("Commercial", "Medicare Advantage", "Medicaid"),
        summary="Appeal a UnitedHealthcare denial with a clear, evidence-based internal appeal and, if needed, an independent external review.",
        why_denials=(
            "As one of the largest insurers in the country, UnitedHealthcare "
            "administers commercial, Medicare Advantage, and Medicaid plans, each "
            "with its own coverage rules. Denials frequently come down to a "
            "medical-necessity determination against an internal clinical policy, "
            "a missing prior authorization, or a network or coding issue rather "
            "than a decision that the care itself was wrong."
        ),
        overview=(
            "A UnitedHealthcare appeal starts with an internal appeal that "
            "responds directly to the reason on your denial letter. Because the "
            "same brand covers very different plan types, the exact address, "
            "deadline, and review levels depend on your specific plan — check "
            "your denial letter and plan documents. If the internal appeal does "
            "not succeed, you can generally escalate to an independent external "
            "review."
        ),
        faqs=(
            FAQItem(
                "How long do I have to appeal a UnitedHealthcare denial?",
                "The deadline is printed on your denial letter and varies by plan "
                "type — check your letter and plan documents rather than relying "
                "on a general number.",
            ),
            FAQItem(
                "What is the difference between an internal appeal and an external review?",
                "An internal appeal asks UnitedHealthcare to reconsider its own "
                "decision. An external review sends the dispute to an independent "
                "reviewer who does not work for the insurer, usually after the "
                "internal appeal is exhausted.",
            ),
            FAQItem(
                "Do I need my doctor's help to appeal?",
                "You can appeal on your own, but a letter of medical necessity "
                "from your treating provider that addresses the specific denial "
                "reason often strengthens the appeal considerably.",
            ),
            FAQItem(
                "Can I get a faster decision if my care is urgent?",
                "Yes. If waiting for a standard decision could seriously harm your "
                "health, you can usually request an expedited appeal, which must "
                "be decided on a much shorter timeline.",
            ),
            FAQItem(
                "What should my appeal letter focus on?",
                "Answer the exact reason for the denial and attach evidence — "
                "records, provider notes, and relevant clinical guidelines — that "
                "shows the service meets the plan's coverage criteria.",
            ),
        ),
        related_slugs=("aetna", "cigna", "optumrx", "humana"),
    ),
    InsurerGuide(
        name="Aetna",
        slug="aetna",
        aliases=("Aetna Health", "Aetna CVS Health", "Aetna Inc"),
        plan_types=("Commercial", "Medicare Advantage", "Medicaid"),
        summary="Appeal an Aetna denial by answering the stated denial reason with medical evidence and escalating to external review if needed.",
        why_denials=(
            "Aetna, part of CVS Health, offers commercial, Medicare Advantage, "
            "and Medicaid coverage. Like other large insurers, its denials often "
            "turn on medical-necessity criteria, prior-authorization requirements, "
            "or formulary and network rules rather than a judgment that the "
            "treatment had no value."
        ),
        overview=(
            "To appeal an Aetna denial, file an internal appeal that directly "
            "rebuts the denial reason and includes supporting documentation. The "
            "precise deadline and where to send the appeal depend on your plan, "
            "so confirm both on your denial letter. If the internal appeal is "
            "denied, an independent external review is generally available."
        ),
        faqs=(
            FAQItem(
                "Where do I send my Aetna appeal?",
                "The mailing address, fax, or portal for appeals is listed on your "
                "denial letter and plan documents, and it varies by plan — use the "
                "one on your paperwork.",
            ),
            FAQItem(
                "Does Aetna offer expedited appeals?",
                "Yes. When a delay could jeopardize your health, you can request "
                "an expedited review that is decided far faster than a standard "
                "appeal.",
            ),
            FAQItem(
                "What evidence helps an Aetna appeal?",
                "Medical records, a letter of medical necessity from your provider, "
                "and any clinical guidelines that support the treatment are the "
                "most persuasive attachments.",
            ),
            FAQItem(
                "Can I appeal a pharmacy or prescription denial?",
                "Yes. Prescription denials follow a similar appeal path, though "
                "they may involve the pharmacy benefit manager and a coverage "
                "exception request. Check your letter for the specific process.",
            ),
            FAQItem(
                "What if my internal appeal is denied?",
                "You can usually request an independent external review, where a "
                "reviewer unaffiliated with Aetna evaluates the denial.",
            ),
        ),
        related_slugs=("unitedhealthcare", "cigna", "cvs-caremark", "anthem-elevance"),
    ),
    InsurerGuide(
        name="Cigna",
        slug="cigna",
        aliases=("Cigna Healthcare", "The Cigna Group", "Cigna Health"),
        plan_types=("Commercial", "Medicare Advantage"),
        summary="Appeal a Cigna denial with a focused internal appeal and, if it fails, an independent external review.",
        why_denials=(
            "Cigna administers commercial and Medicare Advantage plans. Its "
            "denials commonly cite medical necessity under an internal coverage "
            "policy, a missing prior authorization, or network and coding issues "
            "rather than the underlying value of the care."
        ),
        overview=(
            "A Cigna appeal begins with an internal appeal that responds to the "
            "specific denial reason and includes supporting records. The deadline "
            "and submission method are on your denial letter and depend on your "
            "plan. If the internal appeal is unsuccessful, you can typically "
            "escalate to an independent external review."
        ),
        faqs=(
            FAQItem(
                "How do I start a Cigna appeal?",
                "Begin with a written internal appeal that addresses the exact "
                "reason on your denial letter and attaches supporting medical "
                "documentation.",
            ),
            FAQItem(
                "How long does a Cigna appeal take?",
                "Timelines depend on whether the appeal is standard or expedited "
                "and on your plan type; your denial letter states the applicable "
                "deadlines.",
            ),
            FAQItem(
                "Can my doctor appeal on my behalf?",
                "In most cases you can authorize your provider or another "
                "representative to file and pursue the appeal for you.",
            ),
            FAQItem(
                "What if Cigna upholds the denial?",
                "When the internal appeal is denied, you generally have the right "
                "to an external review by an independent organization.",
            ),
        ),
        related_slugs=(
            "unitedhealthcare",
            "aetna",
            "anthem-elevance",
            "express-scripts",
        ),
    ),
    InsurerGuide(
        name="Anthem (Elevance Health)",
        slug="anthem-elevance",
        aliases=("Anthem", "Elevance", "Elevance Health", "Anthem Blue Cross"),
        plan_types=("Commercial", "Medicare Advantage", "Medicaid"),
        summary="Appeal an Anthem / Elevance Health denial by rebutting the stated reason and, if needed, requesting external review.",
        why_denials=(
            "Anthem, now operating under the parent company Elevance Health and "
            "affiliated with many Blue Cross Blue Shield plans, covers commercial, "
            "Medicare Advantage, and Medicaid members. Denials typically stem from "
            "medical-necessity policies, prior-authorization gaps, or network and "
            "tiering rules."
        ),
        overview=(
            "To appeal an Anthem or Elevance denial, submit an internal appeal "
            "that answers the denial reason with evidence. Because Anthem operates "
            "across many states and plan lines, the deadline and address vary — "
            "rely on your denial letter. An independent external review is "
            "generally available after the internal appeal."
        ),
        faqs=(
            FAQItem(
                "Is Anthem the same as Blue Cross Blue Shield?",
                "Anthem (Elevance Health) is a large operator of Blue Cross Blue "
                "Shield-branded plans in many states, but Blue Cross Blue Shield "
                "is a broader association of independent companies. Your specific "
                "plan documents control your appeal.",
            ),
            FAQItem(
                "How do I appeal an Anthem denial?",
                "File a written internal appeal that directly addresses the denial "
                "reason and includes medical records and a provider statement of "
                "medical necessity.",
            ),
            FAQItem(
                "Can I request an expedited Anthem appeal?",
                "Yes, when a delay could seriously harm your health an expedited "
                "review is generally available and is decided quickly.",
            ),
            FAQItem(
                "What happens after an internal denial?",
                "You can usually escalate to an independent external review; your "
                "denial letter and state insurance department explain how.",
            ),
        ),
        related_slugs=("blue-cross-blue-shield", "unitedhealthcare", "aetna", "cigna"),
    ),
    InsurerGuide(
        name="Blue Cross Blue Shield",
        slug="blue-cross-blue-shield",
        aliases=("BCBS", "Blue Cross", "Blue Shield", "The Blues"),
        plan_types=("Commercial", "Medicare Advantage", "Medicaid"),
        summary="Appeal a Blue Cross Blue Shield denial using your specific plan's internal appeal and external review process.",
        why_denials=(
            "Blue Cross Blue Shield is an association of independent, locally "
            "operated companies, so coverage rules differ from state to state. "
            "Denials generally involve medical-necessity criteria, prior "
            "authorization, or network and benefit-design rules specific to your "
            "local plan."
        ),
        overview=(
            "Because each Blue Cross Blue Shield company operates independently, "
            "your appeal follows the process in your own plan's documents. Start "
            "with an internal appeal that addresses the denial reason, then "
            "escalate to an independent external review if it is denied. Confirm "
            "the deadline and address on your denial letter."
        ),
        faqs=(
            FAQItem(
                "Why do Blue Cross Blue Shield rules differ by state?",
                "Blue Cross Blue Shield is a federation of independent companies, "
                "each licensed to operate in its own area, so benefits and appeal "
                "procedures vary. Always use your local plan's documents.",
            ),
            FAQItem(
                "How do I find my plan's appeal process?",
                "Your denial letter, member handbook, and plan documents describe "
                "the exact steps, deadlines, and where to submit your appeal.",
            ),
            FAQItem(
                "What should I include in the appeal?",
                "Answer the specific denial reason and attach medical records and "
                "a provider letter explaining why the care is necessary and covered.",
            ),
            FAQItem(
                "Can I get an external review of a Blue Cross Blue Shield denial?",
                "Yes. After the internal appeal, most members can request an "
                "independent external review through their plan or state insurance "
                "department.",
            ),
        ),
        related_slugs=(
            "anthem-elevance",
            "unitedhealthcare",
            "aetna",
            "kaiser-permanente",
        ),
    ),
    InsurerGuide(
        name="Kaiser Permanente",
        slug="kaiser-permanente",
        aliases=("Kaiser", "KP", "Kaiser Foundation Health Plan"),
        plan_types=("Commercial", "Medicare Advantage", "Medicaid"),
        summary="Appeal a Kaiser Permanente denial through its integrated grievance and appeal process and, if needed, external review.",
        why_denials=(
            "Kaiser Permanente is an integrated system where the health plan and "
            "care delivery are closely linked. Denials often involve "
            "medical-necessity determinations, referral and authorization "
            "requirements, or coverage limits under your specific plan."
        ),
        overview=(
            "A Kaiser Permanente appeal runs through its grievance and appeals "
            "process. File an internal appeal that addresses the denial reason "
            "with supporting records; if it is denied, you generally have a right "
            "to an independent external review. Check your denial letter and "
            "member materials for deadlines and submission details."
        ),
        faqs=(
            FAQItem(
                "How do I file a Kaiser Permanente appeal?",
                "Submit an internal appeal (sometimes called a grievance or "
                "appeal) that responds to the denial reason and includes your "
                "medical records and provider support.",
            ),
            FAQItem(
                "Does Kaiser Permanente offer expedited appeals?",
                "Yes. If your health could be seriously harmed by waiting, you can "
                "request an expedited review with a much shorter decision window.",
            ),
            FAQItem(
                "Can I appeal a referral or authorization denial?",
                "Yes. Denials of referrals, authorizations, or specific services "
                "can be appealed the same way, focusing on why the care is "
                "medically necessary.",
            ),
            FAQItem(
                "What if my Kaiser Permanente appeal is denied?",
                "You can usually request an independent external review; your "
                "denial letter and state regulator explain the process.",
            ),
        ),
        related_slugs=("blue-cross-blue-shield", "unitedhealthcare", "humana", "aetna"),
    ),
    InsurerGuide(
        name="Humana",
        slug="humana",
        aliases=("Humana Inc", "Humana Health"),
        plan_types=("Medicare Advantage", "Commercial", "Medicaid"),
        summary="Appeal a Humana denial, with special attention to Medicare Advantage appeal levels, and escalate as far as the process allows.",
        why_denials=(
            "Humana is heavily focused on Medicare Advantage along with some "
            "commercial and Medicaid coverage. Denials commonly involve "
            "medical-necessity criteria, prior authorization, or formulary rules, "
            "and Medicare Advantage plans follow a structured, multi-level appeal "
            "process."
        ),
        overview=(
            "For a Humana Medicare Advantage denial, the appeal follows the "
            "Medicare appeal levels, beginning with a reconsideration by the plan "
            "and, if denied, review by an independent entity. Commercial and "
            "Medicaid plans follow their own internal appeal and external review "
            "steps. Your denial letter states which process and deadlines apply."
        ),
        faqs=(
            FAQItem(
                "How are Medicare Advantage appeals different?",
                "Medicare Advantage appeals follow defined levels — starting with "
                "a plan reconsideration and moving to an independent review entity "
                "and beyond — rather than a single internal appeal.",
            ),
            FAQItem(
                "How do I start a Humana appeal?",
                "Begin with the reconsideration or internal appeal described on "
                "your denial letter, addressing the specific reason and attaching "
                "supporting documentation.",
            ),
            FAQItem(
                "Can I request an expedited Humana appeal?",
                "Yes. If waiting could seriously harm your health, an expedited "
                "appeal is available and is decided on a shortened timeline.",
            ),
            FAQItem(
                "What if my drug is denied under a Humana plan?",
                "Prescription denials can be appealed through a coverage "
                "determination and, if needed, a redetermination, often involving "
                "the plan's pharmacy benefit process.",
            ),
        ),
        related_slugs=("unitedhealthcare", "aetna", "kaiser-permanente", "cigna"),
    ),
    InsurerGuide(
        name="Centene",
        slug="centene",
        aliases=("Centene Corporation", "Ambetter", "WellCare"),
        plan_types=("Medicaid", "ACA Marketplace", "Medicare Advantage"),
        summary="Appeal a Centene denial across its Medicaid, Marketplace, and Medicare lines using the process in your plan documents.",
        why_denials=(
            "Centene focuses on government-sponsored coverage, including Medicaid "
            "managed care, ACA Marketplace plans (often branded Ambetter), and "
            "Medicare Advantage (including WellCare). Denials often involve "
            "medical-necessity criteria, prior authorization, or eligibility and "
            "network rules that differ by program."
        ),
        overview=(
            "A Centene appeal depends on which program you are in. Medicaid "
            "managed-care members can typically pursue a plan appeal and then a "
            "state fair hearing, while Marketplace and Medicare members follow "
            "internal appeal and external or independent review steps. Your denial "
            "letter identifies the correct path and deadline."
        ),
        faqs=(
            FAQItem(
                "What is a Medicaid fair hearing?",
                "For Medicaid managed-care denials, after the plan's internal "
                "appeal you can usually request a state fair hearing, where a "
                "state official independently reviews the decision.",
            ),
            FAQItem(
                "Is Ambetter the same as Centene?",
                "Ambetter is Centene's ACA Marketplace brand. Your specific plan "
                "documents govern the appeal process for an Ambetter denial.",
            ),
            FAQItem(
                "How do I appeal a Centene denial?",
                "File the internal appeal listed on your denial letter, addressing "
                "the specific reason with medical records and provider support.",
            ),
            FAQItem(
                "Are there deadlines I need to watch?",
                "Yes, and they vary by program and plan. Medicaid appeals in "
                "particular can have short windows, so check your denial letter "
                "promptly.",
            ),
        ),
        related_slugs=("molina", "unitedhealthcare", "humana", "anthem-elevance"),
    ),
    InsurerGuide(
        name="Molina Healthcare",
        slug="molina",
        aliases=("Molina", "Molina Health"),
        plan_types=("Medicaid", "Medicare Advantage", "ACA Marketplace"),
        summary="Appeal a Molina Healthcare denial through its plan appeal and, for Medicaid, a state fair hearing if needed.",
        why_denials=(
            "Molina Healthcare specializes in Medicaid managed care along with "
            "Medicare and Marketplace plans. Denials frequently involve "
            "medical-necessity criteria, prior authorization, or program "
            "eligibility and network requirements."
        ),
        overview=(
            "To appeal a Molina denial, start with the plan's internal appeal, "
            "responding to the denial reason with supporting records. For Medicaid "
            "members, a state fair hearing is generally available after the plan "
            "appeal. Your denial letter states the deadline and how to file."
        ),
        faqs=(
            FAQItem(
                "How do I appeal a Molina Medicaid denial?",
                "File the plan's internal appeal first; if it is denied, you can "
                "usually request a state fair hearing for an independent review.",
            ),
            FAQItem(
                "Can I keep my benefits during a Molina appeal?",
                "In some Medicaid situations you can ask to continue benefits "
                "during the appeal if you act within a short deadline noted on "
                "your denial letter.",
            ),
            FAQItem(
                "What should I send with a Molina appeal?",
                "Include your medical records and a provider statement that "
                "explains why the denied service is medically necessary and "
                "covered.",
            ),
            FAQItem(
                "Does Molina offer expedited appeals?",
                "Yes. When a delay could seriously harm your health, an expedited "
                "appeal with a faster decision is generally available.",
            ),
        ),
        related_slugs=("centene", "unitedhealthcare", "anthem-elevance", "humana"),
    ),
    InsurerGuide(
        name="CVS Caremark",
        slug="cvs-caremark",
        aliases=("Caremark", "CVS Caremark PBM"),
        plan_types=("Pharmacy Benefit Manager",),
        summary="Appeal a CVS Caremark prescription denial with a coverage exception request and formulary or step-therapy appeal.",
        why_denials=(
            "CVS Caremark is a pharmacy benefit manager (PBM) that administers "
            "drug benefits for many health plans. Its denials usually involve "
            "non-formulary drugs, prior authorization, step therapy, or quantity "
            "limits rather than a decision about your overall medical care."
        ),
        overview=(
            "Appealing a CVS Caremark denial generally means asking for a coverage "
            "exception or prior authorization, supported by your prescriber's "
            "explanation of why the specific drug is needed. If the request is "
            "denied, your underlying health plan's appeal and external review "
            "rights typically apply. Check your denial notice for the exact steps."
        ),
        faqs=(
            FAQItem(
                "What is a formulary exception?",
                "A formulary exception asks the plan to cover a drug that is not "
                "on its preferred list, usually because covered alternatives are "
                "not appropriate for you. Your prescriber's support is important.",
            ),
            FAQItem(
                "How do I appeal a step-therapy requirement?",
                "You can request an exception explaining why you should not have "
                "to try the preferred drug first — for example, if you already "
                "tried it or it is medically inappropriate.",
            ),
            FAQItem(
                "Who decides my CVS Caremark appeal?",
                "The pharmacy benefit process handles the initial coverage "
                "decision, but your health plan's appeal rights, including "
                "external review, generally govern further steps.",
            ),
            FAQItem(
                "Can I get an urgent decision on a drug denial?",
                "Yes. If your health could be seriously harmed by a delay, you can "
                "usually request an expedited coverage decision.",
            ),
        ),
        related_slugs=("express-scripts", "optumrx", "aetna", "unitedhealthcare"),
    ),
    InsurerGuide(
        name="Express Scripts",
        slug="express-scripts",
        aliases=("ESI", "Express Scripts PBM", "Evernorth"),
        plan_types=("Pharmacy Benefit Manager",),
        summary="Appeal an Express Scripts prescription denial through a coverage exception and your plan's drug appeal process.",
        why_denials=(
            "Express Scripts is a pharmacy benefit manager (PBM) that manages drug "
            "coverage for many plans. Denials commonly involve non-formulary "
            "medications, prior authorization, step therapy, or quantity limits."
        ),
        overview=(
            "To appeal an Express Scripts denial, request a coverage exception or "
            "prior authorization backed by your prescriber's clinical rationale. "
            "If denied, your health plan's internal appeal and external review "
            "rights generally apply. Your denial notice explains the specific "
            "process and deadline."
        ),
        faqs=(
            FAQItem(
                "How do I appeal an Express Scripts drug denial?",
                "Ask for a coverage exception or prior authorization with your "
                "prescriber's support explaining why the specific medication is "
                "necessary.",
            ),
            FAQItem(
                "What if a lower-cost drug was required first?",
                "You can request a step-therapy exception if the preferred drug is "
                "inappropriate for you or you have already tried it without "
                "success.",
            ),
            FAQItem(
                "Can my prescriber submit the appeal?",
                "Yes. Prescribers commonly submit coverage exception and prior "
                "authorization requests on the patient's behalf.",
            ),
            FAQItem(
                "What happens if the exception is denied?",
                "Your underlying health plan's appeal process, including an "
                "independent external review, generally applies to a denied drug "
                "coverage request.",
            ),
        ),
        related_slugs=("cvs-caremark", "optumrx", "cigna", "unitedhealthcare"),
    ),
    InsurerGuide(
        name="OptumRx",
        slug="optumrx",
        aliases=("Optum Rx", "OptumRx Pharmacy", "Optum Pharmacy"),
        plan_types=("Pharmacy Benefit Manager",),
        summary="Appeal an OptumRx prescription denial with a coverage exception and your plan's pharmacy appeal rights.",
        why_denials=(
            "OptumRx is a pharmacy benefit manager (PBM) that administers drug "
            "benefits for many plans. Denials typically involve non-formulary "
            "drugs, prior authorization, step therapy, or quantity limits rather "
            "than your broader medical coverage."
        ),
        overview=(
            "Appealing an OptumRx denial generally starts with a coverage "
            "exception or prior authorization supported by your prescriber. If the "
            "request is denied, your health plan's appeal and external review "
            "rights usually apply. Confirm the steps and deadline on your denial "
            "notice."
        ),
        faqs=(
            FAQItem(
                "What is a coverage exception request?",
                "It asks the plan to cover a drug that is otherwise restricted or "
                "not on the formulary, based on your prescriber's explanation of "
                "medical need.",
            ),
            FAQItem(
                "How do I appeal an OptumRx prior-authorization denial?",
                "Submit the coverage determination or appeal listed on your notice, "
                "with your prescriber documenting why the medication is necessary.",
            ),
            FAQItem(
                "Can I request a fast decision for an urgent medication?",
                "Yes. If a delay could seriously harm your health, an expedited "
                "coverage decision is generally available.",
            ),
            FAQItem(
                "Who handles the next level of appeal?",
                "After the pharmacy benefit decision, your health plan's internal "
                "appeal and external review process generally governs further "
                "steps.",
            ),
        ),
        related_slugs=("cvs-caremark", "express-scripts", "unitedhealthcare", "humana"),
    ),
)


def validate_insurer_guides(
    guides: tuple[InsurerGuide, ...] = _INSURER_GUIDES,
) -> None:
    """Validate the insurer guide data set for integrity.

    Checks:
        - Required text fields are non-empty.
        - Slugs are unique.
        - Aliases are unique across all guides (case-insensitive).
        - Plan types come from ``VALID_PLAN_TYPES`` and are non-empty.
        - Each guide has between ``MIN_FAQS`` and ``MAX_FAQS`` FAQ items with
          non-empty questions and answers.
        - Every ``related_slugs`` entry resolves to an existing guide and is not
          the guide itself.

    Raises:
        InsurerGuideValidationError: If any check fails.
    """
    slugs: set[str] = set()
    aliases_seen: dict[str, str] = {}

    for guide in guides:
        if not guide.name.strip():
            raise InsurerGuideValidationError("Insurer guide has an empty name")
        if not guide.slug.strip():
            raise InsurerGuideValidationError(
                f"Insurer guide '{guide.name}' has an empty slug"
            )
        for field_name in ("summary", "why_denials", "overview"):
            if not getattr(guide, field_name).strip():
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' has an empty {field_name}"
                )

        if guide.slug in slugs:
            raise InsurerGuideValidationError(f"Duplicate insurer slug: {guide.slug}")
        slugs.add(guide.slug)

        if not guide.plan_types:
            raise InsurerGuideValidationError(
                f"Insurer guide '{guide.slug}' has no plan types"
            )
        for plan_type in guide.plan_types:
            if plan_type not in VALID_PLAN_TYPES:
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' has invalid plan type: {plan_type}"
                )

        for alias in guide.aliases:
            key = alias.strip().lower()
            if not key:
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' has an empty alias"
                )
            if key in aliases_seen:
                raise InsurerGuideValidationError(
                    f"Duplicate alias '{alias}' on '{guide.slug}' "
                    f"(already used by '{aliases_seen[key]}')"
                )
            aliases_seen[key] = guide.slug

        if not (MIN_FAQS <= len(guide.faqs) <= MAX_FAQS):
            raise InsurerGuideValidationError(
                f"Insurer guide '{guide.slug}' must have between {MIN_FAQS} and "
                f"{MAX_FAQS} FAQ items, found {len(guide.faqs)}"
            )
        for faq in guide.faqs:
            if not faq.question.strip() or not faq.answer.strip():
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' has an FAQ with empty text"
                )

    # Validate cross-links after all slugs are known.
    for guide in guides:
        for related in guide.related_slugs:
            if related == guide.slug:
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' links to itself"
                )
            if related not in slugs:
                raise InsurerGuideValidationError(
                    f"Insurer guide '{guide.slug}' links to unknown slug: {related}"
                )


@lru_cache(maxsize=1)
def _guides_by_slug() -> dict[str, InsurerGuide]:
    """Return a validated slug -> InsurerGuide mapping, cached after first call."""
    validate_insurer_guides()
    return {guide.slug: guide for guide in _INSURER_GUIDES}


def get_all_insurer_guides() -> dict[str, InsurerGuide]:
    """Return all insurer guides as a slug -> InsurerGuide mapping."""
    return dict(_guides_by_slug())


def get_insurer_guide(slug: str) -> InsurerGuide | None:
    """Return the insurer guide for a slug, or None if not found."""
    return _guides_by_slug().get(slug)


def get_insurers_sorted_by_name() -> list[InsurerGuide]:
    """Return insurer guides sorted alphabetically by display name."""
    return sorted(_guides_by_slug().values(), key=lambda g: g.name)


def get_insurer_slugs() -> list[str]:
    """Return all insurer guide slugs."""
    return list(_guides_by_slug().keys())


def get_related_guides(slug: str) -> list[InsurerGuide]:
    """Return the cross-linked (related) guides for a given insurer slug.

    Falls back to an empty list if the slug is unknown. Only related slugs that
    resolve to an existing guide are returned.
    """
    guide = get_insurer_guide(slug)
    if guide is None:
        return []
    by_slug = _guides_by_slug()
    return [by_slug[s] for s in guide.related_slugs if s in by_slug]

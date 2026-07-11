"""
Health-insurance and appeals glossary for Fight Health Insurance.

This module defines a curated set of plain-language glossary terms used to
power the public ``/glossary`` index and per-term ``/glossary/<slug>`` pages.

Content is authored directly in Python (rather than a static JSON file or a
database table) because the definitions are original prose maintained
alongside the code and there is no need for runtime editing. The structure
mirrors the lightweight loader/helper API used by ``state_help.py`` so the
views and sitemap can consume it the same way.

Every entry provides a URL slug, a display term, a short one-line summary
(used for meta descriptions and index cards), a longer plain-language
definition, and a list of ``related`` slugs that link to other glossary
entries. ``validate_glossary`` guarantees the internal linking mesh is
consistent (no duplicate slugs and no dangling related-term references).
"""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional


class GlossaryValidationError(ValueError):
    """Raised when the glossary data fails an internal consistency check."""

    pass


@dataclass(frozen=True)
class GlossaryTerm:
    """A single glossary entry.

    Attributes:
        slug: URL-safe identifier used in ``/glossary/<slug>``.
        term: Human-readable display name for the term.
        short: One-sentence summary used for index cards and meta descriptions.
        definition: Original plain-language definition (2-4 sentences).
        related: Slugs of other glossary terms to cross-link to.
        aliases: Alternate names people search for (used to enrich SEO markup).
    """

    slug: str
    term: str
    short: str
    definition: str
    related: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def letter(self) -> str:
        """First letter of the term, upper-cased, for A-Z grouping."""
        return self.term[0].upper() if self.term else "#"

    def __repr__(self) -> str:
        return f"<GlossaryTerm: {self.term} ({self.slug})>"


# The glossary itself. Definitions are original plain-language explanations.
# Keep this list alphabetized-by-term-ish for readability, but the display
# ordering is handled by the helper functions below.
GLOSSARY_TERMS: tuple[GlossaryTerm, ...] = (
    GlossaryTerm(
        slug="adverse-benefit-determination",
        term="Adverse Benefit Determination",
        short="An insurer's formal decision to deny, reduce, or end coverage for a service.",
        definition=(
            "An adverse benefit determination is the official term for a decision "
            "by a health plan that goes against you: denying a service, paying less "
            "than expected, ending a treatment the plan was covering, or refusing to "
            "pay a claim. Federal rules require the plan to explain the reason in "
            "writing and to tell you how to appeal. Recognizing a letter as an "
            "adverse benefit determination matters because it starts the clock on "
            "your appeal deadlines."
        ),
        related=("denial", "internal-appeal", "external-review", "grievance"),
        aliases=("adverse determination",),
    ),
    GlossaryTerm(
        slug="affordable-care-act",
        term="Affordable Care Act (ACA)",
        short="The 2010 federal law that created broad protections for health-plan members.",
        definition=(
            "The Affordable Care Act is the federal health-reform law that, among "
            "many other things, guarantees your right to appeal a denial internally "
            "and to an outside reviewer. It also bans coverage refusals based on "
            "pre-existing conditions and requires most plans to cover a core set of "
            "essential health benefits. Many of the appeal rights people rely on "
            "when fighting a denial come from this law."
        ),
        related=(
            "external-review",
            "internal-appeal",
            "essential-health-benefits",
            "pre-existing-condition",
        ),
        aliases=("ACA", "Obamacare"),
    ),
    GlossaryTerm(
        slug="allowed-amount",
        term="Allowed Amount",
        short="The maximum a plan will count toward a covered service.",
        definition=(
            "The allowed amount is the price your health plan treats as reasonable "
            "for a given service, sometimes called the eligible or negotiated "
            "amount. Your share of the cost, such as coinsurance, is usually "
            "calculated from this figure rather than from the provider's full list "
            "price. When an out-of-network provider charges more than the allowed "
            "amount, you can be billed for the difference."
        ),
        related=(
            "coinsurance",
            "balance-billing",
            "out-of-network",
            "usual-customary-and-reasonable",
        ),
    ),
    GlossaryTerm(
        slug="appeal",
        term="Appeal",
        short="A formal request asking a plan to reconsider a denial.",
        definition=(
            "An appeal is your formal request for a health plan to look again at a "
            "decision you disagree with, such as a denied service or an unpaid "
            "claim. Appeals usually start inside the plan itself and, if that fails, "
            "can move to an independent outside reviewer. A well-documented appeal "
            "explains why the care is medically necessary and points to the plan's "
            "own coverage rules."
        ),
        related=(
            "internal-appeal",
            "external-review",
            "expedited-appeal",
            "adverse-benefit-determination",
            "medical-necessity",
        ),
    ),
    GlossaryTerm(
        slug="balance-billing",
        term="Balance Billing",
        short="When a provider bills you for the gap between their charge and the allowed amount.",
        definition=(
            "Balance billing happens when a provider charges you the difference "
            "between their full fee and the lower amount your plan considers "
            "allowable. It most often arises with out-of-network care. Federal and "
            "state surprise-billing protections now block balance billing in many "
            "emergency and unexpected situations, but not all."
        ),
        related=(
            "allowed-amount",
            "out-of-network",
            "in-network",
            "coordination-of-benefits",
        ),
        aliases=("surprise billing",),
    ),
    GlossaryTerm(
        slug="claim",
        term="Health Insurance Claim",
        short="A request for payment submitted to your plan for covered care.",
        definition=(
            "A claim is the bill submitted to your health plan asking it to pay for "
            "a service you received. Providers usually file claims for you, and the "
            "plan responds by paying, reducing, or denying them. The plan's decision "
            "is documented on an explanation of benefits, which is where you first "
            "see whether a claim was denied."
        ),
        related=("explanation-of-benefits", "denial", "allowed-amount"),
    ),
    GlossaryTerm(
        slug="clinical-guidelines",
        term="Clinical Guidelines",
        short="Evidence-based standards insurers use to judge whether care is appropriate.",
        definition=(
            "Clinical guidelines are the published standards, often drawn from "
            "medical societies or commercial criteria sets, that a plan uses to "
            "decide whether a treatment is appropriate for a given condition. "
            "Insurers frequently cite these guidelines when they deny a service as "
            "not medically necessary. Citing the guideline your own care actually "
            "meets, or showing why it does not fit your situation, is a powerful "
            "appeal argument."
        ),
        related=(
            "medical-necessity",
            "medical-policy",
            "utilization-management",
            "off-label",
        ),
    ),
    GlossaryTerm(
        slug="coinsurance",
        term="Coinsurance",
        short="Your percentage share of the cost of a covered service.",
        definition=(
            "Coinsurance is the portion of a covered service you pay as a "
            "percentage, for example twenty percent, after you have met your "
            "deductible. It is calculated from the plan's allowed amount, not the "
            "provider's list price. Coinsurance continues until your spending "
            "reaches the out-of-pocket maximum for the year."
        ),
        related=("deductible", "copayment", "allowed-amount", "out-of-pocket-maximum"),
    ),
    GlossaryTerm(
        slug="concurrent-review",
        term="Concurrent Review",
        short="A plan's ongoing review of care while you are still receiving it.",
        definition=(
            "Concurrent review is the process a plan uses to keep approving care "
            "that is already underway, such as a hospital stay or an ongoing course "
            "of therapy. If the reviewer decides continued treatment is no longer "
            "necessary, the plan can cut off coverage even mid-course. Because these "
            "decisions are time-sensitive, they often qualify for an expedited "
            "appeal."
        ),
        related=(
            "utilization-management",
            "retrospective-review",
            "expedited-appeal",
            "medical-necessity",
        ),
    ),
    GlossaryTerm(
        slug="continuity-of-care",
        term="Continuity of Care",
        short="Protections that let you keep seeing a provider during a coverage transition.",
        definition=(
            "Continuity of care refers to rules that let you keep receiving "
            "treatment from a current provider for a limited time when your "
            "coverage changes, for instance when your doctor leaves the network or "
            "you switch plans mid-treatment. These protections are especially "
            "important during pregnancy, active cancer treatment, or other ongoing "
            "care. You usually have to request continuity of care within a short "
            "window after the change."
        ),
        related=("in-network", "out-of-network", "non-medical-switching"),
    ),
    GlossaryTerm(
        slug="coordination-of-benefits",
        term="Coordination of Benefits",
        short="Rules that decide which plan pays first when you have more than one.",
        definition=(
            "Coordination of benefits is the set of rules used when a person is "
            "covered by two or more health plans to determine which one pays first "
            "and which pays second. The goal is to make sure combined payments do "
            "not exceed the total cost of care. Confusion over which plan is primary "
            "is a common and fixable cause of claim denials."
        ),
        related=("claim", "denial", "allowed-amount"),
    ),
    GlossaryTerm(
        slug="copayment",
        term="Copayment",
        short="A fixed dollar amount you pay for a covered service.",
        definition=(
            "A copayment, or copay, is a set fee you pay at the time of care, such "
            "as thirty dollars for an office visit or a fixed amount for a "
            "prescription. Unlike coinsurance, it does not change with the total "
            "price of the service. Copays for a drug often depend on which formulary "
            "tier it falls into."
        ),
        related=(
            "coinsurance",
            "deductible",
            "formulary-tier",
            "out-of-pocket-maximum",
        ),
    ),
    GlossaryTerm(
        slug="deductible",
        term="Deductible",
        short="The amount you pay yourself before the plan starts sharing costs.",
        definition=(
            "A deductible is the amount you must pay out of pocket for covered "
            "services each year before your plan begins paying its share. Some "
            "services, such as preventive care, may be covered before you meet it. "
            "Once the deductible is satisfied, you typically move on to paying "
            "coinsurance or copayments."
        ),
        related=("coinsurance", "copayment", "out-of-pocket-maximum", "premium"),
    ),
    GlossaryTerm(
        slug="denial",
        term="Claim Denial",
        short="A plan's refusal to pay for or authorize a service.",
        definition=(
            "A denial is a decision by your health plan not to pay for or not to "
            "approve a service. Denials can happen before care through a rejected "
            "prior authorization, or after care when a claim is not paid. Every "
            "denial should come with a reason code and instructions for appealing, "
            "and most denials can be challenged."
        ),
        related=(
            "adverse-benefit-determination",
            "prior-authorization",
            "internal-appeal",
            "explanation-of-benefits",
            "medical-necessity",
        ),
        aliases=("claim denial", "coverage denial"),
    ),
    GlossaryTerm(
        slug="erisa",
        term="ERISA",
        short="The federal law governing most employer-sponsored health plans.",
        definition=(
            "ERISA, the Employee Retirement Income Security Act, is the federal law "
            "that regulates most health plans offered through private employers. It "
            "sets out what information a plan must give you, the deadlines for "
            "deciding appeals, and your right to see the documents used to deny your "
            "claim. If your coverage comes from a private-sector job, ERISA rules "
            "usually shape how your appeal must be handled."
        ),
        related=(
            "internal-appeal",
            "adverse-benefit-determination",
            "evidence-of-coverage",
        ),
    ),
    GlossaryTerm(
        slug="essential-health-benefits",
        term="Essential Health Benefits",
        short="A core set of ten service categories most plans must cover.",
        definition=(
            "Essential health benefits are the ten categories of care, such as "
            "hospitalization, prescription drugs, maternity care, and mental health "
            "services, that most individual and small-group plans are required to "
            "cover. A denial that leaves an essential benefit entirely uncovered may "
            "be worth challenging on those grounds. The exact services within each "
            "category can vary by state benchmark plan."
        ),
        related=("affordable-care-act", "evidence-of-coverage", "formulary"),
    ),
    GlossaryTerm(
        slug="evidence-of-coverage",
        term="Evidence of Coverage (EOC)",
        short="The detailed contract describing exactly what your plan covers.",
        definition=(
            "The evidence of coverage is the full document that spells out your "
            "plan's benefits, exclusions, cost sharing, and the rules for getting "
            "care approved. It is essentially the contract between you and your "
            "insurer. When you appeal, quoting the specific language in your "
            "evidence of coverage that supports your case is often decisive."
        ),
        related=(
            "summary-of-benefits-and-coverage",
            "medical-policy",
            "internal-appeal",
            "erisa",
        ),
        aliases=("EOC", "certificate of coverage"),
    ),
    GlossaryTerm(
        slug="expedited-appeal",
        term="Expedited Appeal",
        short="A faster appeal for situations where waiting could harm your health.",
        definition=(
            "An expedited appeal is a speeded-up review used when the normal "
            "timeline could seriously jeopardize your health or your ability to "
            "regain function. Plans generally must decide these within seventy-two "
            "hours, and sometimes sooner. You can often pursue an expedited internal "
            "appeal and an expedited external review at the same time."
        ),
        related=("internal-appeal", "external-review", "concurrent-review", "appeal"),
        aliases=("urgent appeal",),
    ),
    GlossaryTerm(
        slug="experimental-or-investigational",
        term="Experimental or Investigational",
        short="A common denial reason claiming a treatment is unproven.",
        definition=(
            "Insurers label a service experimental or investigational when they "
            "argue it has not been proven safe and effective for your condition, "
            "and they use that label to deny coverage. These denials can often be "
            "overturned with peer-reviewed studies, treatment guidelines, or "
            "evidence that the therapy is now standard of care. What counts as "
            "experimental is defined in the plan's own medical policy."
        ),
        related=(
            "medical-necessity",
            "off-label",
            "medical-policy",
            "clinical-guidelines",
        ),
        aliases=(
            "experimental treatment",
            "investigational",
        ),
    ),
    GlossaryTerm(
        slug="explanation-of-benefits",
        term="Explanation of Benefits (EOB)",
        short="A statement showing how a claim was processed and what you owe.",
        definition=(
            "An explanation of benefits is the statement your plan sends after "
            "processing a claim, showing the amount billed, the allowed amount, what "
            "the plan paid, and what you may owe. It is not a bill, but it tells you "
            "whether a claim was paid or denied and why. Reading the EOB carefully "
            "is usually the first step in spotting an error worth appealing."
        ),
        related=("claim", "denial", "allowed-amount", "coinsurance"),
        aliases=("EOB",),
    ),
    GlossaryTerm(
        slug="external-review",
        term="External Review",
        short="An independent review of a denial by reviewers outside your plan.",
        definition=(
            "External review is your right to have a denial examined by "
            "independent medical experts who do not work for your insurer, usually "
            "after you have exhausted the plan's internal appeals. The reviewers' "
            "decision is binding on the plan. External review is a powerful step "
            "because it removes the insurer from the final call."
        ),
        related=(
            "internal-appeal",
            "independent-review-organization",
            "adverse-benefit-determination",
            "affordable-care-act",
        ),
        aliases=("independent external review",),
    ),
    GlossaryTerm(
        slug="formulary",
        term="Formulary",
        short="The list of prescription drugs a plan agrees to cover.",
        definition=(
            "A formulary is the list of medications your plan covers, usually "
            "organized into tiers that determine your cost. Drugs left off the "
            "formulary may require a special exception request to be covered. "
            "Formularies change over time, so a medication covered last year may "
            "move tiers or drop off entirely."
        ),
        related=(
            "formulary-tier",
            "non-formulary-exception",
            "tiering-exception",
            "step-therapy",
            "quantity-limit",
        ),
    ),
    GlossaryTerm(
        slug="formulary-tier",
        term="Formulary Tier",
        short="The pricing level a covered drug is placed in.",
        definition=(
            "A formulary tier is the cost level a plan assigns to a covered drug, "
            "with lower tiers usually holding inexpensive generics and higher tiers "
            "holding brand-name or specialty medications. Your copay or coinsurance "
            "depends heavily on which tier your drug lands in. If a needed drug sits "
            "on an expensive tier, a tiering exception can sometimes lower your "
            "cost."
        ),
        related=(
            "formulary",
            "tiering-exception",
            "copayment",
            "non-formulary-exception",
        ),
        aliases=("specialty tier", "drug tier"),
    ),
    GlossaryTerm(
        slug="gold-carding",
        term="Gold Carding",
        short="Programs that exempt reliable providers from prior authorization.",
        definition=(
            "Gold carding is a policy under which a plan waives prior authorization "
            "requirements for providers who have shown a consistent track record of "
            "appropriate requests. The idea is to cut paperwork and delays for "
            "clinicians the insurer already trusts. Several states have passed laws "
            "encouraging or requiring gold-carding programs."
        ),
        related=("prior-authorization", "utilization-management"),
    ),
    GlossaryTerm(
        slug="grievance",
        term="Grievance",
        short="A formal complaint about service quality rather than a coverage decision.",
        definition=(
            "A grievance is a formal complaint you file about how your plan or a "
            "provider treated you, such as poor service, long waits, or "
            "communication problems, rather than about a specific denied benefit. "
            "It follows a different track than an appeal, which challenges a "
            "coverage decision. Filing a grievance can still be worthwhile when the "
            "problem is about process rather than payment."
        ),
        related=("appeal", "adverse-benefit-determination", "internal-appeal"),
        aliases=("complaint",),
    ),
    GlossaryTerm(
        slug="in-network",
        term="In-Network",
        short="Providers who have a contract with your plan for lower rates.",
        definition=(
            "In-network providers are the doctors, hospitals, and pharmacies that "
            "have agreed to contracted rates with your plan, which usually means "
            "lower costs for you. Staying in network also protects you from balance "
            "billing in most cases. Plans often require you to use in-network "
            "providers except in emergencies."
        ),
        related=(
            "out-of-network",
            "balance-billing",
            "allowed-amount",
            "continuity-of-care",
        ),
    ),
    GlossaryTerm(
        slug="independent-review-organization",
        term="Independent Review Organization (IRO)",
        short="The outside body that conducts external reviews of denials.",
        definition=(
            "An independent review organization is an accredited, third-party "
            "entity that carries out external reviews of insurance denials. Its "
            "medical reviewers have no financial stake in the outcome, and their "
            "decision generally binds the plan. Understanding that a real, "
            "independent panel will read your file is a good reason to make your "
            "external-review submission thorough."
        ),
        related=("external-review", "internal-appeal", "adverse-benefit-determination"),
        aliases=("IRO",),
    ),
    GlossaryTerm(
        slug="internal-appeal",
        term="Internal Appeal",
        short="The first level of appeal, handled inside your plan.",
        definition=(
            "An internal appeal is your request for the plan itself to reconsider a "
            "denial, and it is usually the required first step before an outside "
            "review. You generally have at least 180 days from the denial to file. "
            "Completing the internal appeal, even when you expect it to fail, is "
            "often necessary to unlock your right to external review."
        ),
        related=(
            "external-review",
            "expedited-appeal",
            "adverse-benefit-determination",
            "appeal",
            "letter-of-medical-necessity",
        ),
    ),
    GlossaryTerm(
        slug="letter-of-medical-necessity",
        term="Letter of Medical Necessity",
        short="A provider's written case explaining why care is needed.",
        definition=(
            "A letter of medical necessity is a statement, usually written by your "
            "treating provider, that explains why a specific service or medication "
            "is appropriate and necessary for your condition. It ties your diagnosis "
            "and history to the requested treatment and often cites supporting "
            "evidence. A strong letter is frequently the deciding factor in "
            "overturning a medical-necessity denial."
        ),
        related=(
            "medical-necessity",
            "internal-appeal",
            "prior-authorization",
            "clinical-guidelines",
        ),
        aliases=("LMN", "medical necessity letter"),
    ),
    GlossaryTerm(
        slug="local-coverage-determination",
        term="Local Coverage Determination (LCD)",
        short="A regional Medicare rule on when a service is covered.",
        definition=(
            "A local coverage determination is a decision by a regional Medicare "
            "contractor about whether and when a particular service is covered in "
            "its area. LCDs fill in gaps where there is no national rule and can "
            "vary from region to region. If a Medicare denial cites an LCD, checking "
            "whether your situation actually meets its criteria is a good appeal "
            "starting point."
        ),
        related=(
            "national-coverage-determination",
            "medicare-advantage",
            "medical-necessity",
        ),
        aliases=("LCD",),
    ),
    GlossaryTerm(
        slug="medical-necessity",
        term="Medical Necessity",
        short="The standard of care being appropriate and needed for your condition.",
        definition=(
            "Medical necessity is the standard insurers use to decide whether a "
            "service is appropriate, effective, and needed to diagnose or treat your "
            "condition. A denial for lack of medical necessity means the plan is not "
            "convinced the care meets its criteria, not necessarily that the care is "
            "wrong. These are among the most appealable denials because they turn on "
            "clinical evidence you and your provider can supply."
        ),
        related=(
            "letter-of-medical-necessity",
            "clinical-guidelines",
            "medical-policy",
            "experimental-or-investigational",
            "denial",
        ),
    ),
    GlossaryTerm(
        slug="medical-policy",
        term="Medical Policy",
        short="An insurer's internal rules for covering a specific treatment.",
        definition=(
            "A medical policy, sometimes called a clinical or coverage policy, is a "
            "plan's own written rulebook describing the conditions under which it "
            "will cover a particular procedure, drug, or device. These documents are "
            "usually public and list the exact criteria a request must meet. "
            "Reading the relevant medical policy tells you precisely what evidence "
            "your appeal needs to provide."
        ),
        related=(
            "clinical-guidelines",
            "medical-necessity",
            "utilization-management",
            "experimental-or-investigational",
        ),
        aliases=("coverage policy", "clinical policy"),
    ),
    GlossaryTerm(
        slug="medicare-advantage",
        term="Medicare Advantage",
        short="Private plans that deliver Medicare benefits, often with prior auth.",
        definition=(
            "Medicare Advantage plans are private health plans that provide your "
            "Medicare benefits in place of Original Medicare, frequently adding "
            "networks and prior-authorization rules. Denials in these plans follow a "
            "structured appeal process with specific deadlines and an automatic "
            "outside review at higher levels. Knowing you are in a Medicare "
            "Advantage plan changes which appeal rules apply to you."
        ),
        related=(
            "prior-authorization",
            "external-review",
            "national-coverage-determination",
            "local-coverage-determination",
        ),
        aliases=("Medicare Part C",),
    ),
    GlossaryTerm(
        slug="national-coverage-determination",
        term="National Coverage Determination (NCD)",
        short="A nationwide Medicare rule on whether a service is covered.",
        definition=(
            "A national coverage determination is a Medicare-wide decision about "
            "whether a specific item or service is covered across the entire "
            "country. It sets a baseline that applies everywhere, unlike a regional "
            "local coverage determination. When a Medicare denial conflicts with an "
            "NCD that supports your care, that mismatch can anchor a strong appeal."
        ),
        related=(
            "local-coverage-determination",
            "medicare-advantage",
            "medical-necessity",
        ),
        aliases=("NCD",),
    ),
    GlossaryTerm(
        slug="non-formulary-exception",
        term="Non-Formulary Exception",
        short="A request to cover a drug that is not on the plan's list.",
        definition=(
            "A non-formulary exception is a formal request asking your plan to cover "
            "a medication that is not on its formulary, usually because covered "
            "alternatives are ineffective or unsafe for you. Your prescriber "
            "typically has to explain why the non-formulary drug is medically "
            "necessary. If the exception is denied, the decision can be appealed "
            "like any other denial."
        ),
        related=(
            "formulary",
            "formulary-tier",
            "tiering-exception",
            "step-therapy",
            "letter-of-medical-necessity",
        ),
        aliases=("formulary exception",),
    ),
    GlossaryTerm(
        slug="non-medical-switching",
        term="Non-Medical Switching (Forced Switch)",
        short="Being pushed off a working treatment for cost, not medical, reasons.",
        definition=(
            "Non-medical switching happens when a plan pressures you to change from "
            "a treatment that is working to a different one for reasons of cost "
            "rather than health, often by dropping a drug or raising its tier "
            "mid-year. Patients who are stable on a therapy can face setbacks when "
            "forced to switch. A continuity-of-care request or a formulary exception "
            "is sometimes the path to keep your current treatment."
        ),
        related=(
            "formulary",
            "non-formulary-exception",
            "tiering-exception",
            "continuity-of-care",
            "step-therapy",
        ),
        aliases=("forced switch", "forced switching"),
    ),
    GlossaryTerm(
        slug="off-label",
        term="Off-Label Use",
        short="Using an approved drug for a purpose beyond its FDA labeling.",
        definition=(
            "Off-label use means prescribing an FDA-approved medication for a "
            "condition or in a way not listed on its official label, which is legal "
            "and often supported by evidence. Insurers sometimes deny off-label "
            "prescriptions as experimental, even when medical literature backs them. "
            "Citing compendia or peer-reviewed studies is a common way to appeal "
            "these denials."
        ),
        related=(
            "experimental-or-investigational",
            "medical-necessity",
            "clinical-guidelines",
        ),
    ),
    GlossaryTerm(
        slug="out-of-network",
        term="Out-of-Network",
        short="Providers without a contract with your plan, often costing more.",
        definition=(
            "Out-of-network providers have no contracted rate with your plan, so "
            "care from them typically costs more and may not count the same toward "
            "your deductible or out-of-pocket maximum. In some cases you can request "
            "in-network coverage when no suitable in-network provider is available. "
            "Out-of-network care is also where balance billing most often occurs."
        ),
        related=(
            "in-network",
            "balance-billing",
            "allowed-amount",
            "continuity-of-care",
        ),
    ),
    GlossaryTerm(
        slug="out-of-pocket-maximum",
        term="Out-of-Pocket Maximum",
        short="The yearly cap on what you pay before the plan covers 100%.",
        definition=(
            "The out-of-pocket maximum is the most you have to pay for covered, "
            "in-network care in a plan year through deductibles, copays, and "
            "coinsurance combined. Once you reach it, the plan pays the full cost of "
            "covered services for the rest of the year. Premiums and non-covered "
            "services do not count toward this limit."
        ),
        related=("deductible", "coinsurance", "copayment", "premium"),
        aliases=("maximum out-of-pocket", "MOOP"),
    ),
    GlossaryTerm(
        slug="peer-to-peer-review",
        term="Peer-to-Peer Review",
        short="A direct call between your doctor and the plan's reviewing physician.",
        definition=(
            "A peer-to-peer review is a phone conversation in which your treating "
            "provider speaks directly with a physician working for the insurer to "
            "argue that a denied service should be approved. It is often available "
            "quickly and can resolve a denial before a formal appeal is even filed. "
            "A focused peer-to-peer that addresses the exact reason for denial can "
            "reverse it on the spot."
        ),
        related=(
            "prior-authorization",
            "internal-appeal",
            "medical-necessity",
            "utilization-management",
        ),
        aliases=("peer to peer", "P2P"),
    ),
    GlossaryTerm(
        slug="pre-existing-condition",
        term="Pre-Existing Condition",
        short="A health condition you had before coverage began.",
        definition=(
            "A pre-existing condition is a health problem that existed before your "
            "current coverage started. Under the Affordable Care Act, most plans can "
            "no longer deny coverage or charge you more because of one. If a plan "
            "tries to deny care by pointing to a pre-existing condition, that denial "
            "may conflict with current law."
        ),
        related=("affordable-care-act", "rescission", "denial"),
    ),
    GlossaryTerm(
        slug="premium",
        term="Premium",
        short="The recurring amount you pay to keep coverage in force.",
        definition=(
            "A premium is the fixed amount, usually paid monthly, that you or your "
            "employer pay to keep your health coverage active, regardless of whether "
            "you use any care. It is separate from the costs you pay when you "
            "actually receive services. Premiums do not count toward your deductible "
            "or out-of-pocket maximum."
        ),
        related=("deductible", "out-of-pocket-maximum", "coinsurance"),
    ),
    GlossaryTerm(
        slug="prior-authorization",
        term="Prior Authorization",
        short="Advance approval a plan requires before it will cover a service.",
        definition=(
            "Prior authorization is a requirement that your provider get the plan's "
            "approval before a service, drug, or procedure will be covered. The plan "
            "reviews the request against its medical policy and either approves or "
            "denies it, sometimes causing delays in care. A prior-authorization "
            "denial can be challenged through a peer-to-peer review or a formal "
            "appeal."
        ),
        related=(
            "step-therapy",
            "peer-to-peer-review",
            "medical-necessity",
            "denial",
            "gold-carding",
            "quantity-limit",
        ),
        aliases=(
            "preauthorization",
            "pre-authorization",
            "prior auth",
            "precertification",
        ),
    ),
    GlossaryTerm(
        slug="quantity-limit",
        term="Quantity Limit",
        short="A cap on how much of a drug the plan will cover in a period.",
        definition=(
            "A quantity limit restricts how much of a medication your plan will "
            "cover over a given time, such as a maximum number of pills per month. "
            "The limits are usually based on standard dosing, but they can conflict "
            "with what your prescriber has determined you need. When they do, your "
            "provider can request an exception explaining the higher quantity."
        ),
        related=(
            "formulary",
            "prior-authorization",
            "non-formulary-exception",
            "step-therapy",
        ),
    ),
    GlossaryTerm(
        slug="rescission",
        term="Rescission",
        short="A retroactive cancellation of coverage as if it never existed.",
        definition=(
            "Rescission is when an insurer cancels your coverage retroactively, "
            "treating it as though it never existed, usually alleging a material "
            "misstatement on your application. The Affordable Care Act sharply "
            "limits rescission, permitting it generally only for fraud or "
            "intentional misrepresentation. A rescission must come with advance "
            "notice and can be appealed."
        ),
        related=(
            "affordable-care-act",
            "pre-existing-condition",
            "adverse-benefit-determination",
        ),
    ),
    GlossaryTerm(
        slug="retrospective-review",
        term="Retrospective Review",
        short="A plan's review of care after it has already been provided.",
        definition=(
            "Retrospective review is when a plan evaluates whether care was "
            "appropriate after it has already been delivered, which can lead to a "
            "claim being denied after the fact. It differs from prior authorization, "
            "which happens before care, and concurrent review, which happens during "
            "it. A retrospective denial is appealable, often with the clinical "
            "records that justified the treatment at the time."
        ),
        related=(
            "concurrent-review",
            "utilization-management",
            "prior-authorization",
            "claim",
        ),
    ),
    GlossaryTerm(
        slug="site-of-service",
        term="Site-of-Service Review",
        short="A plan steering care to a lower-cost location.",
        definition=(
            "Site-of-service review is a plan's practice of deciding that a covered "
            "procedure must be done at a specific, usually lower-cost location, such "
            "as an outpatient center instead of a hospital. If care is delivered "
            "somewhere the plan considers unnecessary, it may deny or reduce "
            "payment. When your clinical situation requires a particular setting, "
            "that justification is the core of a site-of-service appeal."
        ),
        related=("utilization-management", "medical-necessity", "prior-authorization"),
    ),
    GlossaryTerm(
        slug="step-therapy",
        term="Step Therapy (Fail-First)",
        short="A rule requiring you to try cheaper treatments before a preferred one.",
        definition=(
            "Step therapy, also called fail-first, requires you to try one or more "
            "lower-cost treatments and have them fail before the plan will cover the "
            "medication your provider originally wanted. It is meant to control "
            "costs but can delay effective care. Most plans allow an exception when "
            "you have already tried the required steps or they are inappropriate for "
            "you."
        ),
        related=(
            "prior-authorization",
            "formulary",
            "non-formulary-exception",
            "quantity-limit",
            "medical-necessity",
        ),
        aliases=("fail first", "fail-first"),
    ),
    GlossaryTerm(
        slug="summary-of-benefits-and-coverage",
        term="Summary of Benefits and Coverage (SBC)",
        short="A short standardized snapshot of what a plan covers and costs.",
        definition=(
            "The summary of benefits and coverage is a brief, standardized document "
            "that lets you see a plan's key benefits and cost sharing in a common "
            "format. It is designed to make plans easier to compare and to give a "
            "quick reference for what is covered. For the full and controlling "
            "details, you still need the evidence of coverage."
        ),
        related=("evidence-of-coverage", "essential-health-benefits", "deductible"),
        aliases=("SBC",),
    ),
    GlossaryTerm(
        slug="tiering-exception",
        term="Tiering Exception",
        short="A request to pay a lower tier's cost for a higher-tier drug.",
        definition=(
            "A tiering exception is a request asking your plan to charge you the "
            "cost-sharing of a lower formulary tier for a drug that normally sits on "
            "a more expensive tier. It is typically granted when lower-tier "
            "alternatives would not be as safe or effective for you. Your prescriber "
            "usually needs to support the request with a medical explanation."
        ),
        related=("formulary-tier", "formulary", "non-formulary-exception", "copayment"),
    ),
    GlossaryTerm(
        slug="usual-customary-and-reasonable",
        term="Usual, Customary, and Reasonable (UCR)",
        short="A benchmark insurers use to cap payment for out-of-network care.",
        definition=(
            "Usual, customary, and reasonable is a benchmark plans use to decide how "
            "much to pay for a service, especially out-of-network, based on typical "
            "charges in the area. If a provider bills above the UCR amount, the plan "
            "may pay only up to that figure and leave you responsible for the rest. "
            "Disputes over how a UCR rate was calculated can be a basis for appeal."
        ),
        related=("allowed-amount", "out-of-network", "balance-billing"),
        aliases=("UCR",),
    ),
    GlossaryTerm(
        slug="utilization-management",
        term="Utilization Management",
        short="The umbrella process insurers use to control which care is approved.",
        definition=(
            "Utilization management is the broad set of techniques, including prior "
            "authorization, step therapy, and concurrent and retrospective review, "
            "that plans use to decide whether care they consider appropriate will be "
            "covered. It is where most denials originate. Understanding which "
            "utilization-management tool produced your denial helps you target the "
            "right appeal argument."
        ),
        related=(
            "prior-authorization",
            "step-therapy",
            "concurrent-review",
            "retrospective-review",
            "medical-policy",
        ),
        aliases=("utilization review", "UM"),
    ),
    GlossaryTerm(
        slug="hmo",
        term="Health Maintenance Organization (HMO)",
        short="A plan type built around a set network that usually requires referrals.",
        definition=(
            "A health maintenance organization is a plan that keeps costs down by "
            "limiting you to a defined network of providers and, in most cases, "
            "asking you to pick a primary care doctor who coordinates your care. "
            "Seeing a specialist often requires a referral, and care outside the "
            "network is generally not covered except in emergencies. Denials in an "
            "HMO frequently trace back to a missing referral or an out-of-network "
            "visit."
        ),
        related=(
            "ppo",
            "epo",
            "pos",
            "in-network",
            "out-of-network",
            "prior-authorization",
        ),
        aliases=("HMO",),
    ),
    GlossaryTerm(
        slug="ppo",
        term="Preferred Provider Organization (PPO)",
        short="A flexible plan type that covers out-of-network care at a higher cost.",
        definition=(
            "A preferred provider organization is a plan that lets you see providers "
            "both inside and outside its network, usually without a referral, though "
            "you pay more for out-of-network care. The added flexibility typically "
            "comes with higher premiums than more restrictive plans. Because "
            "out-of-network care is only partly covered, PPO members can still face "
            "balance billing and larger cost-sharing."
        ),
        related=(
            "hmo",
            "epo",
            "pos",
            "in-network",
            "out-of-network",
            "balance-billing",
        ),
        aliases=("PPO",),
    ),
    GlossaryTerm(
        slug="epo",
        term="Exclusive Provider Organization (EPO)",
        short="A network-only plan that usually skips referrals but not the network.",
        definition=(
            "An exclusive provider organization is a plan that covers care only from "
            "providers in its network, much like an HMO, but often does not require "
            "referrals to see specialists. Step outside the network and you generally "
            "pay the full cost yourself, except in emergencies. Knowing your plan is "
            "an EPO helps you anticipate that out-of-network denials will be hard to "
            "overturn on network grounds alone."
        ),
        related=("hmo", "ppo", "pos", "in-network", "out-of-network"),
        aliases=("EPO",),
    ),
    GlossaryTerm(
        slug="pos",
        term="Point-of-Service Plan (POS)",
        short="A hybrid plan blending HMO referral rules with some out-of-network coverage.",
        definition=(
            "A point-of-service plan mixes features of HMO and PPO coverage: you "
            "usually choose a primary care doctor and need referrals like an HMO, but "
            "you can still get partial coverage for out-of-network care like a PPO. "
            "Your costs depend heavily on whether you follow the referral rules and "
            "stay in network. Missing a required referral is a common, and often "
            "appealable, reason a POS claim is denied."
        ),
        related=(
            "hmo",
            "ppo",
            "epo",
            "in-network",
            "out-of-network",
            "prior-authorization",
        ),
        aliases=("POS",),
    ),
    GlossaryTerm(
        slug="cobra",
        term="COBRA Continuation Coverage",
        short="A right to keep employer coverage for a time after it would otherwise end.",
        definition=(
            "COBRA is a federal protection that lets you keep your employer-based "
            "health plan for a limited period after an event like a job loss or a "
            "reduction in hours. The coverage is the same, but you usually pay the "
            "full premium yourself plus a small administrative fee, so it can be "
            "costly. COBRA can bridge a gap in coverage and preserve continuity of "
            "care while you find a new plan."
        ),
        related=("premium", "continuity-of-care", "hipaa", "pre-existing-condition"),
        aliases=("COBRA",),
    ),
    GlossaryTerm(
        slug="hipaa",
        term="HIPAA",
        short="The federal law protecting the privacy of your health information.",
        definition=(
            "HIPAA, the Health Insurance Portability and Accountability Act, is the "
            "federal law best known for protecting the privacy and security of your "
            "health information and giving you the right to see and get copies of "
            "your records. Those access rights matter when you are gathering medical "
            "records to support an appeal. HIPAA also set early rules about carrying "
            "coverage between jobs."
        ),
        related=("cobra", "pre-existing-condition", "evidence-of-coverage"),
        aliases=("HIPAA",),
    ),
    GlossaryTerm(
        slug="waiting-period",
        term="Waiting Period",
        short="The time you must wait before coverage or a specific benefit begins.",
        definition=(
            "A waiting period is the stretch of time you have to wait after enrolling "
            "before your coverage, or a particular benefit within it, actually takes "
            "effect. Employer plans, for example, may impose a waiting period before "
            "a new hire's coverage starts. If a claim is denied as falling inside a "
            "waiting period, confirming the exact dates against your plan documents "
            "is the first step in checking whether the denial is correct."
        ),
        related=("premium", "pre-existing-condition", "continuity-of-care"),
    ),
)


class _GlossaryData:
    """Immutable, validated view of the glossary data.

    Built once and cached so views and the sitemap share the same parsed,
    consistency-checked data.
    """

    def __init__(self, terms: tuple[GlossaryTerm, ...]):
        validate_glossary(terms)
        self.terms: tuple[GlossaryTerm, ...] = terms
        self.by_slug: dict[str, GlossaryTerm] = {t.slug: t for t in terms}


def validate_glossary(terms: tuple[GlossaryTerm, ...] = GLOSSARY_TERMS) -> None:
    """Validate the internal consistency of the glossary.

    Ensures there are no duplicate slugs, that no term links to itself, and
    that every ``related`` slug resolves to a real glossary entry.

    Raises:
        GlossaryValidationError: If any consistency check fails.
    """
    slugs = [t.slug for t in terms]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        raise GlossaryValidationError(f"Duplicate glossary slugs: {sorted(duplicates)}")

    valid_slugs = set(slugs)
    for term in terms:
        for related_slug in term.related:
            if related_slug == term.slug:
                raise GlossaryValidationError(
                    f"Term '{term.slug}' lists itself as a related term"
                )
            if related_slug not in valid_slugs:
                raise GlossaryValidationError(
                    f"Term '{term.slug}' links to unknown related term '{related_slug}'"
                )


@lru_cache(maxsize=1)
def _glossary_data() -> _GlossaryData:
    """Return the validated, cached glossary data."""
    return _GlossaryData(GLOSSARY_TERMS)


def get_all_terms() -> tuple[GlossaryTerm, ...]:
    """Return all glossary terms in their declared order."""
    return _glossary_data().terms


def get_terms_sorted() -> list[GlossaryTerm]:
    """Return all glossary terms sorted alphabetically by display name."""
    return sorted(_glossary_data().terms, key=lambda t: t.term.lower())


def get_term(slug: str) -> Optional[GlossaryTerm]:
    """Return the glossary term for a slug, or ``None`` if it does not exist."""
    return _glossary_data().by_slug.get(slug)


def get_related_terms(term: GlossaryTerm) -> list[GlossaryTerm]:
    """Return the resolved ``GlossaryTerm`` objects for a term's related slugs."""
    data = _glossary_data()
    return [data.by_slug[s] for s in term.related if s in data.by_slug]


def get_glossary_slugs() -> list[str]:
    """Return every glossary slug."""
    return [t.slug for t in _glossary_data().terms]


def get_terms_grouped_by_letter() -> list[tuple[str, list[GlossaryTerm]]]:
    """Group terms alphabetically by first letter for the A-Z index.

    Returns:
        A list of ``(letter, terms)`` pairs ordered A-Z, each ``terms`` list
        sorted by display name.
    """
    groups: dict[str, list[GlossaryTerm]] = {}
    for term in get_terms_sorted():
        groups.setdefault(term.letter, []).append(term)
    return [(letter, groups[letter]) for letter in sorted(groups)]

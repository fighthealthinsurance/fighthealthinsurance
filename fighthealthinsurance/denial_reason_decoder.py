"""
Denial Reason Decoder – structured data for the public educational tool.

Each denial reason is described by a typed DenialReason instance so that
content lives in one place and is easy to extend. The module provides a
load function with in-memory caching (matching the state_help.py pattern).

SEO helpers (JSON-LD, meta, breadcrumb) are exposed as methods on each
DenialReason instance so that templates stay clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import ClassVar, Dict, List, Optional


@dataclass(frozen=True)
class AppealAngle:
    """A single appeal angle with a headline and supporting evidence tips."""

    headline: str
    description: str
    evidence_tips: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DenialReason:
    """One curated denial reason with all the content for the public tool."""

    slug: str
    title: str
    subtitle: str
    meta_description: str

    # Section 1 – what it means in plain English
    what_it_means: str

    # Section 2 – why insurers commonly use it
    why_insurers_use_it: str

    # Section 3 – strongest general appeal angles + evidence tips
    appeal_angles: List[AppealAngle] = field(default_factory=list)

    # Section 4 – call-to-action text (formatted as HTML-safe snippet)
    cta_text: str = ""

    # Keywords for the reason (for meta and internal linking)
    keywords: List[str] = field(default_factory=list)

    # ——— JSON-LD helpers ———————————————————————————————————————————————

    FAQ_JSONLD_TYPE: ClassVar[str] = "FAQPage"
    ARTICLE_JSONLD_TYPE: ClassVar[str] = "Article"

    def faq_jsonld(
        self, site_url: str = "https://www.fighthealthinsurance.com"
    ) -> dict:
        """Return an FAQPage JSON-LD blob for this reason."""
        detail_url = f"{site_url}/tools/denial-reason-decoder/{self.slug}/"
        return {
            "@context": "https://schema.org",
            "@type": self.FAQ_JSONLD_TYPE,
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": f"What does '{self.title}' mean in a health insurance denial?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": self.what_it_means,
                    },
                },
                {
                    "@type": "Question",
                    "name": f"Why do insurers deny claims as '{self.title}'?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": self.why_insurers_use_it,
                    },
                },
                {
                    "@type": "Question",
                    "name": f"How can I appeal a '{self.title}' denial?",
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": " ".join(
                            f"{angle.headline}: {angle.description}"
                            for angle in self.appeal_angles
                        ),
                    },
                },
            ],
            "url": detail_url,
        }

    def breadcrumb_jsonld(
        self, site_url: str = "https://www.fighthealthinsurance.com"
    ) -> dict:
        """Return a BreadcrumbList JSON-LD blob for this reason."""
        index_url = f"{site_url}/tools/denial-reason-decoder/"
        detail_url = f"{site_url}/tools/denial-reason-decoder/{self.slug}/"
        return {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": "Denial Reason Decoder",
                    "item": index_url,
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": self.title,
                    "item": detail_url,
                },
            ],
        }

    def article_jsonld(
        self, site_url: str = "https://www.fighthealthinsurance.com"
    ) -> dict:
        """Return an Article JSON-LD blob for this reason."""
        detail_url = f"{site_url}/tools/denial-reason-decoder/{self.slug}/"
        return {
            "@context": "https://schema.org",
            "@type": self.ARTICLE_JSONLD_TYPE,
            "headline": f"Understanding '{self.title}' Health Insurance Denials",
            "description": self.meta_description,
            "url": detail_url,
            "author": {
                "@type": "Organization",
                "name": "Fight Health Insurance",
            },
        }

    # ——— Internal linking helpers ——————————————————————————————————————

    def related_reasons(
        self, all_reasons: Dict[str, DenialReason]
    ) -> List[DenialReason]:
        """Return up to 3 other reasons for cross-linking, sorted by slug."""
        others = [r for slug, r in all_reasons.items() if slug != self.slug]
        return others[:3]


# ── curated data ──────────────────────────────────────────────────────────


def _build_reasons() -> List[DenialReason]:
    """Return the curated list of denial reasons in display order."""

    return [
        DenialReason(
            slug="step-therapy-fail-first",
            title="Step Therapy / Fail First",
            subtitle="When your insurer requires you to try a cheaper treatment before covering the one your doctor prescribed",
            meta_description="Learn what 'step therapy' means in a health insurance denial, why insurers require it, and the strongest appeal strategies and evidence to fight back.",
            what_it_means=(
                "Step therapy, sometimes called 'fail first,' is a policy where your insurance company requires "
                "you to try one or more lower-cost medications or treatments before they will cover the one your "
                "healthcare provider originally prescribed. If the cheaper option does not work (or causes intolerable "
                "side effects), the insurer may then approve the originally prescribed treatment. In practice, this "
                "denial means the insurer believes a less expensive alternative could be effective for your condition."
            ),
            why_insurers_use_it=(
                "Insurers use step therapy to control prescription drug spending. By requiring members to start "
                "with generic or preferred-brand drugs, they reduce their overall pharmacy costs. This approach "
                "is especially common for high-cost specialty drugs used to treat conditions like rheumatoid "
                "arthritis, psoriasis, multiple sclerosis, and hepatitis C."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Medical necessity of the prescribed treatment",
                    description=(
                        "Provide documentation from your doctor explaining why the prescribed treatment is "
                        "medically necessary for you specifically — not just why it is a preferred option, but "
                        "why the step-therapy alternatives would be inappropriate or harmful."
                    ),
                    evidence_tips=[
                        "A detailed letter of medical necessity from your prescribing physician",
                        "Published clinical guidelines supporting the prescribed treatment for your condition",
                        "Peer-reviewed studies showing superior efficacy or safety of the prescribed treatment",
                    ],
                ),
                AppealAngle(
                    headline="Step-therapy alternatives already tried or contraindicated",
                    description=(
                        "If you have already tried the required step-therapy drug and experienced adverse effects "
                        "or lack of efficacy, document that history. If you have a medical reason you cannot safely "
                        "take the step-therapy drug, your doctor should state that explicitly."
                    ),
                    evidence_tips=[
                        "Medical records documenting prior treatment failures or adverse reactions",
                        "A statement from your doctor that the step drug is contraindicated for you",
                        "Pharmacy records showing prior fills of the required medication",
                    ],
                ),
                AppealAngle(
                    headline="Risk of irreversible disease progression",
                    description=(
                        "For progressive conditions, delaying effective treatment can lead to permanent damage. "
                        "Your doctor can explain that waiting for a step-therapy trial to fail may cause "
                        "irreversible harm — an argument many external reviewers find compelling."
                    ),
                    evidence_tips=[
                        "Medical literature documenting the risk of disease progression without timely treatment",
                        "Imaging or lab results showing current disease activity",
                        "A prognosis statement from your specialist",
                    ],
                ),
            ],
            cta_text=(
                "If you received a step therapy denial, our free tool can help you generate an appeal letter "
                "that addresses this specific reason. Start your appeal below."
            ),
            keywords=[
                "step therapy appeal",
                "fail first denial",
                "step therapy exception",
                "prior authorization step therapy",
            ],
        ),
        DenialReason(
            slug="not-medically-necessary",
            title="Not Medically Necessary",
            subtitle="The most common denial reason — your insurer says the treatment isn't needed",
            meta_description="Understand what 'not medically necessary' really means in insurance denials, why it's so frequently used, and the evidence-based appeal strategies that work.",
            what_it_means=(
                "When an insurer labels a service as 'not medically necessary,' they are claiming that the "
                "treatment, test, or procedure does not meet their internal criteria for appropriate care. "
                "This does not mean your doctor is wrong or that the treatment is unnecessary — it means "
                "the insurer's definition of 'necessary' differs from your doctor's clinical judgment. "
                "This is the single most common reason given for claim denials."
            ),
            why_insurers_use_it=(
                "Insurers rely on 'not medically necessary' because it is broad, subjective, and shifts the "
                "burden of proof onto the patient and provider. Each insurer maintains its own clinical "
                "coverage guidelines, which may be more restrictive than national medical society standards. "
                "By categorizing a service this way, the insurer avoids paying while appearing to base its "
                "decision on clinical reasoning."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Cite authoritative clinical guidelines",
                    description=(
                        "Gather published guidelines from the relevant medical specialty society (e.g., "
                        "American College of Cardiology, American Society of Clinical Oncology) that support "
                        "the treatment as standard of care for your condition."
                    ),
                    evidence_tips=[
                        "National medical society clinical practice guidelines",
                        "Peer-reviewed journal articles establishing standard of care",
                        "Your doctor's written statement explaining why the treatment meets these standards",
                    ],
                ),
                AppealAngle(
                    headline="Document the consequences of not receiving care",
                    description=(
                        "A letter from your doctor explaining the likely clinical consequences if you do not "
                        "receive the treatment — disease progression, permanent impairment, increased pain, "
                        "or emergency hospitalization — can be powerful."
                    ),
                    evidence_tips=[
                        "A statement of medical necessity detailing risks of non-treatment",
                        "Medical records documenting your current condition and prognosis",
                        "Cost-effectiveness data (preventing more expensive care later)",
                    ],
                ),
                AppealAngle(
                    headline="Challenge the insurer's internal criteria",
                    description=(
                        "Request a copy of the insurer's medical policy or clinical coverage guideline used "
                        "to deny your claim. Compare it with broader, evidence-based standards. Point out "
                        "where the insurer's criteria are more restrictive than accepted medical practice."
                    ),
                    evidence_tips=[
                        "The insurer's own written clinical policy (request it in writing)",
                        "Comparison with Medicare coverage determinations for the same service",
                        "Independent medical review organization standards",
                    ],
                ),
            ],
            cta_text=(
                "A 'not medically necessary' denial can be overturned with the right documentation. "
                "Use our free appeal generator to build a letter tailored to this denial reason."
            ),
            keywords=[
                "not medically necessary appeal",
                "medical necessity denial",
                "medical necessity letter",
                "appeal medical necessity",
            ],
        ),
        DenialReason(
            slug="experimental-or-investigational",
            title="Experimental or Investigational",
            subtitle="Your insurer claims the treatment is unproven or still in testing",
            meta_description="Learn what 'experimental or investigational' means in an insurance denial, why insurers use this label, and the most effective appeal strategies and types of evidence to gather.",
            what_it_means=(
                "An 'experimental or investigational' denial means the insurer has determined that the "
                "proposed treatment, drug, or procedure is not yet proven to be safe and effective for your "
                "specific condition. This label is frequently applied to newer treatments, off-label uses "
                "of FDA-approved drugs, genetic testing, and emerging surgical techniques. It does not mean "
                "the treatment is actually experimental — only that the insurer has not yet added it to "
                "its list of covered services."
            ),
            why_insurers_use_it=(
                "Insurers use this denial to avoid covering high-cost cutting-edge treatments before "
                "they have accumulated a large body of evidence. New treatments are often expensive, and "
                "insurers prefer to wait until sufficient data exists before adding them to coverage "
                "policies. This creates a frustrating gap between what medical science offers and what "
                "insurance will pay for."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Show the treatment is standard of care, not experimental",
                    description=(
                        "Collect published clinical trials, systematic reviews, and professional society "
                        "guidelines that demonstrate the treatment is accepted as effective. Even if it is "
                        "newer, evidence of efficacy and safety from respected sources can shift the "
                        "classification from 'experimental' to 'medically necessary.'"
                    ),
                    evidence_tips=[
                        "Phase III clinical trial results published in peer-reviewed journals",
                        "Professional society guidelines endorsing the treatment",
                        "FDA approval or clearance documentation for the device or drug",
                    ],
                ),
                AppealAngle(
                    headline="Demonstrate that standard alternatives have failed",
                    description=(
                        "Document your history with standard treatments that have been tried and failed. "
                        "When conventional options are exhausted, the justification for trying an "
                        "evidence-supported newer approach becomes stronger."
                    ),
                    evidence_tips=[
                        "Medical records showing treatment history and failures",
                        "A letter from your doctor explaining why standard options are exhausted",
                        "Documentation of adverse effects from prior treatments",
                    ],
                ),
                AppealAngle(
                    headline="Use the insurer's own definition against them",
                    description=(
                        "Request the insurer's written definition of 'experimental' and 'investigational.' "
                        "Many policies define these terms in ways that exclude treatments with published "
                        "peer-reviewed evidence. If you can show the treatment meets the insurer's own "
                        "standard for being proven, you have a strong case."
                    ),
                    evidence_tips=[
                        "The insurer's written medical policy definition",
                        "Published evidence that satisfies each element of their definition",
                        "Coverage decisions by other major insurers for the same treatment",
                    ],
                ),
            ],
            cta_text=(
                "An 'experimental' label does not have to be the final word. "
                "Generate a strong, evidence-backed appeal letter with our free tool."
            ),
            keywords=[
                "experimental treatment appeal",
                "investigational denial appeal",
                "off-label treatment coverage",
                "experimental drug insurance appeal",
            ],
        ),
        DenialReason(
            slug="non-formulary-formulary-exclusion",
            title="Non-Formulary / Formulary Exclusion",
            subtitle="Your medication isn't on your plan's approved drug list",
            meta_description="Understand what a formulary exclusion means, why insurers use drug formularies to deny coverage, and the appeal strategies that can help you get your medication covered.",
            what_it_means=(
                "A formulary is a list of prescription drugs that your health plan agrees to cover. "
                "A non-formulary or formulary-exclusion denial means the medication your doctor prescribed "
                "is not on that list. This can happen because the insurer considers the drug too expensive, "
                "believes a formulary alternative is equally effective, or has not yet reviewed the "
                "medication for inclusion. It is essentially the pharmacy-benefit equivalent of a coverage "
                "exclusion."
            ),
            why_insurers_use_it=(
                "Pharmacy benefit managers (PBMs) and insurers negotiate rebates and discounts with drug "
                "manufacturers. Drugs that make it onto the formulary are often those for which the insurer "
                "receives the best financial arrangement — not necessarily the most clinically appropriate "
                "option. Excluding a drug from the formulary is a cost-control mechanism."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Formulary exception request",
                    description=(
                        "Most plans have a formal process to request a formulary exception. Your doctor "
                        "submits documentation showing that the formulary alternatives are not appropriate "
                        "for you — because of contraindications, past failures, or unique clinical needs."
                    ),
                    evidence_tips=[
                        "A formulary exception request form (available from your insurer)",
                        "A letter of medical necessity from your prescribing physician",
                        "Documentation of why each formulary alternative is unsuitable",
                    ],
                ),
                AppealAngle(
                    headline="Medical necessity of the prescribed drug",
                    description=(
                        "Argue that the prescribed medication is medically necessary for your condition "
                        "and that no formulary alternative provides equivalent safety and efficacy. "
                        "This is often successful when the drug is the standard of care for your diagnosis."
                    ),
                    evidence_tips=[
                        "Clinical guidelines naming the prescribed drug as first-line therapy",
                        "Comparative effectiveness studies favoring the prescribed drug",
                        "Medical records documenting your specific clinical need",
                    ],
                ),
                AppealAngle(
                    headline="Continuity of care / stable on current medication",
                    description=(
                        "If you are already taking the medication and are stable, argue that switching "
                        "could destabilize your condition. Many plans have continuity-of-care provisions "
                        "that protect patients who are stable on existing therapies."
                    ),
                    evidence_tips=[
                        "Records showing you are stable on the current medication",
                        "A doctor's statement that switching poses clinical risk",
                        "Documentation of past adverse reactions to formulary alternatives",
                    ],
                ),
            ],
            cta_text=(
                "A formulary exclusion is not the end of the road. Use our appeal generator to create "
                "a persuasive letter requesting coverage for your prescribed medication."
            ),
            keywords=[
                "formulary exception appeal",
                "non-formulary drug coverage",
                "prescription drug denial appeal",
                "formulary exclusion appeal letter",
            ],
        ),
        DenialReason(
            slug="prior-authorization-required-not-met",
            title="Prior Authorization Required or Not Met",
            subtitle="Your insurer says it did not pre-approve the service, or the approval was insufficient",
            meta_description="Learn what prior authorization denials mean, why insurers require pre-approval, and the most effective strategies to appeal when prior auth is denied or deemed insufficient.",
            what_it_means=(
                "A prior authorization (PA) denial means the insurer requires pre-approval before a "
                "service or medication is covered, and that approval was either not obtained or was "
                "obtained but deemed insufficient for the specific service rendered. Sometimes the "
                "provider did submit a PA request that was denied, and other times the service was "
                "performed without a required PA. In either case, the insurer is refusing to pay because "
                "the administrative step was not satisfied."
            ),
            why_insurers_use_it=(
                "Prior authorization is a utilization management tool. By requiring pre-approval, insurers "
                "can review whether a service meets their coverage criteria before it is performed. This "
                "lets them deny services prospectively and steer patients toward lower-cost alternatives. "
                "The administrative burden also creates a de facto barrier — some providers and patients "
                "simply give up rather than navigate the PA process."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Retrospective authorization / retroactive review",
                    description=(
                        "Many insurers allow retrospective (after-the-fact) authorization when the service "
                        "was medically urgent and waiting for approval would have jeopardized your health. "
                        "Your doctor can submit documentation explaining why immediate treatment was necessary."
                    ),
                    evidence_tips=[
                        "Medical records documenting the urgency of the service",
                        "A physician statement explaining why waiting for PA would cause harm",
                        "Emergency department or hospital records, if applicable",
                    ],
                ),
                AppealAngle(
                    headline="Correct and resubmit the PA request",
                    description=(
                        "If the original PA request was denied, review the denial reason carefully. "
                        "Sometimes the request lacked sufficient clinical information. A corrected "
                        "submission with comprehensive documentation can succeed on a second review."
                    ),
                    evidence_tips=[
                        "The original PA denial letter (to understand what was missing)",
                        "Complete clinical documentation supporting medical necessity",
                        "Peer-reviewed evidence supporting the requested service",
                    ],
                ),
                AppealAngle(
                    headline="Appeal the PA denial on clinical grounds",
                    description=(
                        "If the PA was denied on medical-necessity grounds, treat this as a medical "
                        "necessity appeal. Gather clinical evidence and a strong letter of medical "
                        "necessity from your provider."
                    ),
                    evidence_tips=[
                        "Clinical practice guidelines supporting the treatment",
                        "A detailed letter of medical necessity",
                        "Documentation of alternative treatments tried and failed",
                    ],
                ),
            ],
            cta_text=(
                "A prior authorization denial is often a paperwork problem with a paperwork solution. "
                "Use our appeal generator to build a thorough response."
            ),
            keywords=[
                "prior authorization appeal",
                "retroactive authorization",
                "prior auth denial appeal",
                "PA denial appeal letter",
            ],
        ),
        DenialReason(
            slug="quantity-limit",
            title="Quantity Limit",
            subtitle="Your insurer limits how much of a medication you can receive in a given period",
            meta_description="Understand quantity limit denials for prescription drugs, why insurers impose dispensing limits, and proven strategies to appeal when you need more than the allowed amount.",
            what_it_means=(
                "A quantity limit denial means your health plan has a cap on the amount of a medication "
                "you can receive within a specific time period (e.g., 30 tablets per 30 days). If your "
                "doctor prescribes a higher dose or more frequent dosing than the plan's limit allows, "
                "the pharmacy claim will be denied. These limits can apply to both the number of units "
                "and the total days' supply."
            ),
            why_insurers_use_it=(
                "Quantity limits are another cost-control and safety tool. For some medications (such as "
                "opioids), limits are intended to reduce the risk of misuse or diversion. For others "
                "(such as specialty drugs), limits are designed to control spending by capping the "
                "amount dispensed per fill. The limits are based on FDA-approved dosing, but real-world "
                "prescribing sometimes requires different dosing."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Medical necessity for higher dosing",
                    description=(
                        "Your doctor can submit documentation explaining why you require a higher dose "
                        "or more frequent dosing than the standard. This includes clinical evidence that "
                        "the FDA-approved dose is insufficient for your specific case."
                    ),
                    evidence_tips=[
                        "A physician statement explaining the clinical need for higher dosing",
                        "Published literature supporting off-label dosing for your condition",
                        "Documentation of failed response at the standard dose",
                    ],
                ),
                AppealAngle(
                    headline="Request a quantity limit override or exception",
                    description=(
                        "Most plans have a formal quantity limit exception process. Your doctor submits "
                        "a request explaining why the limit should be overridden for you, with supporting "
                        "clinical evidence."
                    ),
                    evidence_tips=[
                        "The plan's quantity limit exception request form",
                        "Clinical documentation supporting the requested quantity",
                        "A letter of medical necessity from your provider",
                    ],
                ),
                AppealAngle(
                    headline="Split-fill or dose-optimization alternative",
                    description=(
                        "In some cases, a higher-strength formulation can reduce the number of units "
                        "needed per month while delivering the same total dose. Work with your doctor "
                        "and pharmacist to explore whether a different strength could satisfy the limit."
                    ),
                    evidence_tips=[
                        "Pharmacist consultation on therapeutic alternatives",
                        "Prescriber documentation of dose optimization rationale",
                        "Prior authorization for a higher-strength formulation",
                    ],
                ),
            ],
            cta_text=(
                "Quantity limits can often be overridden with proper documentation. Our appeal generator "
                "can help you build the case for an exception."
            ),
            keywords=[
                "quantity limit override",
                "medication quantity limit appeal",
                "pharmacy quantity limit exception",
                "drug quantity limit denial",
            ],
        ),
        DenialReason(
            slug="out-of-network",
            title="Out-of-Network",
            subtitle="Your insurer won't fully cover care from a provider outside its network",
            meta_description="Learn what out-of-network denials mean, why insurers use narrow networks, and what evidence and strategies can help you appeal or reduce your out-of-pocket costs.",
            what_it_means=(
                "An out-of-network denial happens when you receive care from a healthcare provider "
                "(doctor, hospital, lab) that does not have a contract with your insurance plan. "
                "Depending on your plan type, the insurer may pay nothing at all, or may pay a lower "
                "percentage of the charges, leaving you with a much larger bill. Some plans (like HMOs) "
                "provide no out-of-network coverage except in emergencies."
            ),
            why_insurers_use_it=(
                "Insurers build provider networks to negotiate lower rates with doctors and hospitals. "
                "When you stay in-network, the insurer pays negotiated rates. Out-of-network providers "
                "can charge whatever they want, and the insurer has no contract to control those costs. "
                "Narrow networks are a core cost-control strategy — but they can leave patients with "
                "surprise bills, especially in emergencies or when in-network specialists are unavailable."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Emergency or urgent care exception",
                    description=(
                        "Under the federal No Surprises Act, emergency services provided at an "
                        "out-of-network facility must be covered at in-network rates. Similarly, if you "
                        "received care at an in-network facility but were treated by an out-of-network "
                        "provider without your knowledge or consent, the No Surprises Act protects you."
                    ),
                    evidence_tips=[
                        "Emergency department records showing the visit was for an emergency condition",
                        "The No Surprises Act reference (effective January 1, 2022)",
                        "Documentation showing you did not knowingly choose out-of-network care",
                    ],
                ),
                AppealAngle(
                    headline="Network adequacy / no in-network alternative available",
                    description=(
                        "If no in-network provider within a reasonable distance and timeframe can provide "
                        "the needed service, you can argue that the network is inadequate. Many states "
                        "require insurers to maintain adequate networks, and federal rules also apply."
                    ),
                    evidence_tips=[
                        "Evidence that no in-network provider is available within a reasonable distance",
                        "Documentation of wait times for in-network specialists",
                        "A doctor's statement that the specific out-of-network provider was uniquely qualified",
                    ],
                ),
                AppealAngle(
                    headline="Continuity of care / transition of care",
                    description=(
                        "If you were in the middle of a treatment course with a provider who became "
                        "out-of-network (e.g., due to a plan change or provider contract termination), "
                        "you may qualify for continuity-of-care coverage to complete your treatment."
                    ),
                    evidence_tips=[
                        "Records showing you were in active treatment when the provider became out-of-network",
                        "A doctor's statement explaining the risks of interrupting care",
                        "The plan's continuity-of-care policy (request it in writing)",
                    ],
                ),
            ],
            cta_text=(
                "Don't pay an out-of-network bill without challenging it first. Our appeal generator "
                "can help you build a strong case for coverage."
            ),
            keywords=[
                "out of network appeal",
                "surprise medical bill appeal",
                "no surprises act appeal",
                "out of network coverage exception",
            ],
        ),
        DenialReason(
            slug="non-medical-switching-forced-switch",
            title="Non-Medical Switching / Forced Switch",
            subtitle="Your insurer wants to change your medication to a different one for non-clinical reasons",
            meta_description="Understand non-medical switching denials, why insurers force medication changes for cost reasons, and the appeal strategies and evidence that can protect your prescribed treatment.",
            what_it_means=(
                "Non-medical switching is when an insurer requires you to switch from a medication you "
                "are stable on to a different medication — not because your doctor recommends it, but "
                "because of cost, formulary changes, or rebate arrangements. You may receive a letter "
                "stating your current medication will no longer be covered, or that you must try and "
                "fail a preferred alternative first. This is essentially step therapy applied to a "
                "medication you are already taking successfully."
            ),
            why_insurers_use_it=(
                "Insurers and PBMs periodically renegotiate their formularies. When a manufacturer offers "
                "a better rebate on a competing drug, the insurer may drop the old drug and push patients "
                "to the new preferred option. This is purely a financial decision driven by rebate "
                "negotiations — not by clinical evidence that the switch is safe or effective for you."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Continuity of care / stable patient exemption",
                    description=(
                        "If you are stable on your current medication, your doctor can argue that "
                        "switching poses a clinical risk — loss of disease control, side effects from "
                        "the new drug, or irreversible progression during the transition period."
                    ),
                    evidence_tips=[
                        "Medical records demonstrating stability on the current medication",
                        "A doctor's statement detailing the risks of switching",
                        "Documentation of past adverse reactions to the proposed alternative",
                    ],
                ),
                AppealAngle(
                    headline="Clinical differences between the drugs",
                    description=(
                        "Even drugs in the same class can have meaningful differences in efficacy, "
                        "side-effect profile, or drug interactions. Your doctor can document why the "
                        "proposed alternative is not clinically equivalent for you."
                    ),
                    evidence_tips=[
                        "Pharmacology data on differences between the two drugs",
                        "Published studies comparing efficacy and safety",
                        "Documentation of your specific response to the current medication",
                    ],
                ),
                AppealAngle(
                    headline="State-level protections against non-medical switching",
                    description=(
                        "Many states have passed laws restricting non-medical switching for patients "
                        "with chronic or serious conditions. Check whether your state offers these "
                        "protections and cite the relevant statute in your appeal."
                    ),
                    evidence_tips=[
                        "Your state's law on non-medical switching (if applicable)",
                        "Documentation that your condition qualifies under the state statute",
                        "A letter invoking your rights under the relevant law",
                    ],
                ),
            ],
            cta_text=(
                "You should not have to switch a working medication for financial reasons. "
                "Use our appeal generator to push back against a forced medication switch."
            ),
            keywords=[
                "non medical switching appeal",
                "forced medication change appeal",
                "formulary change appeal",
                "medication switch denial appeal",
            ],
        ),
        DenialReason(
            slug="coverage-terminated-no-longer-covered",
            title="Coverage Terminated / No Longer Covered",
            subtitle="Your insurer says the service or medication was dropped from your plan",
            meta_description="Learn what 'coverage terminated' means in an insurance denial, why insurers drop coverage for certain services, and the strongest strategies to appeal and seek alternative paths to coverage.",
            what_it_means=(
                "A 'coverage terminated' or 'no longer covered' denial means the insurer has decided "
                "that the specific service, drug, or category of care is no longer a covered benefit "
                "under your plan. This is different from a medical-necessity denial — the insurer is not "
                "saying the care is unnecessary, but rather that your plan simply does not include it. "
                "This can happen mid-year due to formulary changes, or at plan renewal when benefits change."
            ),
            why_insurers_use_it=(
                "Insurers routinely adjust plan benefits to manage costs. Dropping coverage for expensive "
                "treatments, particularly new drugs or therapies, reduces their financial exposure. "
                "Employers who self-fund their health plans also sometimes exclude certain benefits to "
                "keep premiums lower. The insurer is often legally permitted to change covered benefits "
                "at plan renewal, though mid-year changes are more restricted."
            ),
            appeal_angles=[
                AppealAngle(
                    headline="Grandfathering / transition of care",
                    description=(
                        "If you were already receiving the treatment when coverage was terminated, you "
                        "may qualify for transition-of-care or grandfathering provisions that allow you "
                        "to continue the current course of treatment for a defined period."
                    ),
                    evidence_tips=[
                        "Proof you were receiving the treatment before coverage changed",
                        "Your plan's transition-of-care or continuation-of-care policy",
                        "A doctor's statement on the risks of interrupting ongoing treatment",
                    ],
                ),
                AppealAngle(
                    headline="Reclassify the service under a covered benefit",
                    description=(
                        "Sometimes a service can be reclassified. For example, if a specific drug category "
                        "is excluded, but the drug can be justified under a different covered benefit "
                        "(such as durable medical equipment or preventive care), you may be able to gain "
                        "coverage through an alternative pathway."
                    ),
                    evidence_tips=[
                        "A detailed review of your plan's covered benefits (Summary Plan Description)",
                        "A provider letter reclassifying the service under a covered category",
                        "Documentation supporting the alternative classification",
                    ],
                ),
                AppealAngle(
                    headline="State or federal mandate coverage",
                    description=(
                        "Some services are required to be covered by state or federal law, regardless "
                        "of what the plan document says. For example, certain preventive services, "
                        "mental health parity requirements, or state-mandated benefits may override "
                        "a plan exclusion."
                    ),
                    evidence_tips=[
                        "Citation of the relevant state or federal mandate",
                        "Documentation that your service falls within the mandated benefit",
                        "Your state insurance department's guidance on mandated benefits",
                    ],
                ),
            ],
            cta_text=(
                "When coverage is terminated, you still have options. Our appeal generator can help "
                "you explore every path to get your care covered."
            ),
            keywords=[
                "coverage terminated appeal",
                "benefit exclusion appeal",
                "health plan coverage dropped",
                "service no longer covered appeal",
            ],
        ),
    ]


# ── public API (mirrors state_help.py) ─────────────────────────────────


@lru_cache(maxsize=1)
def _load_reasons_cached() -> List[DenialReason]:
    """Load denial reasons in display order (cached in memory)."""
    return _build_reasons()


def load_reasons() -> List[DenialReason]:
    """Return all denial reasons in display order."""
    return list(_load_reasons_cached())


def get_reason(slug: str) -> Optional[DenialReason]:
    """Look up a single denial reason by slug. Returns None if not found."""
    by_slug = {r.slug: r for r in _load_reasons_cached()}
    return by_slug.get(slug)


def get_reasons_map() -> Dict[str, DenialReason]:
    """Return a dict of slug -> DenialReason (useful for cross-linking)."""
    return {r.slug: r for r in _load_reasons_cached()}

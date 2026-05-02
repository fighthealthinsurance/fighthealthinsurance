"""
Financial assistance directory for prescription drug and treatment denials.

Curated catalog of:
  * Pharma copay foundations and patient assistance programs (NeedyMeds,
    PAN, Good Days, HealthWell, Patient Advocate Foundation, TAF, NORD).
  * State Medicaid pathways (linked into the existing state_help module so
    we don't duplicate the per-state agency catalog).
  * 340B-eligible safety-net providers (FQHCs, Ryan White HIV/AIDS clinics,
    hemophilia treatment centers, etc.) and the HRSA finder.

Searchable by drug name (matched against the pharmacy_coupon_detector
canonical names and brand aliases) or by diagnosis keyword. Returns a
combined `FinancialAssistanceResults` aggregate suitable for rendering on
microsites or returning from REST endpoints.

Curation principles:
  * Only national or near-national programs - per-disease state-by-state
    foundations would balloon the catalog without much marginal value.
  * Plain-text descriptions; the templates handle styling.
  * Each entry tagged with a coarse category (copay_foundation,
    patient_assistance, safety_net, medicaid) so callers can filter.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from fighthealthinsurance.pharmacy_coupon_detector import detect_drug


@dataclass(frozen=True)
class AssistanceProgram:
    """A single financial-assistance program entry."""

    name: str
    url: str
    description: str
    # Coarse category for filtering / template grouping.
    # One of: "copay_foundation", "patient_assistance", "safety_net",
    # "medicaid", "manufacturer".
    category: str
    # Diagnosis keywords this program serves. Matched case-insensitively
    # against the diagnosis text. Empty set = matches any diagnosis.
    diagnoses: frozenset[str] = field(default_factory=frozenset)
    # Drug names (canonical generic names) this program serves. Matched
    # against the canonical drug name from pharmacy_coupon_detector. Empty
    # set = matches any drug.
    drugs: frozenset[str] = field(default_factory=frozenset)
    # Optional eligibility note (income limits, residency, etc.).
    eligibility_note: Optional[str] = None
    # Phone for programs where calling is the primary path.
    phone: Optional[str] = None


# General-purpose copay/patient-assistance foundations. These accept
# applications across many diseases - they're listed without diagnosis
# filters so they show up for every search.
GENERAL_PROGRAMS: tuple[AssistanceProgram, ...] = (
    AssistanceProgram(
        name="NeedyMeds",
        url="https://www.needymeds.org/",
        description=(
            "Searchable database of 5,000+ patient assistance, copay, and "
            "diagnosis-specific programs. Start here if you are not sure "
            "where to look."
        ),
        category="copay_foundation",
        eligibility_note=(
            "No eligibility check - the directory itself is free. Individual "
            "programs have their own income and insurance criteria."
        ),
    ),
    AssistanceProgram(
        name="Patient Advocate Foundation Co-Pay Relief",
        url="https://www.copays.org/",
        description=(
            "Direct copay assistance for insured patients with chronic, "
            "life-threatening, or rare conditions. Covers many disease funds."
        ),
        category="copay_foundation",
        eligibility_note=(
            "Generally requires insurance coverage and income up to 400% of "
            "the federal poverty level (varies by fund)."
        ),
        phone="1-866-512-3861",
    ),
    AssistanceProgram(
        name="HealthWell Foundation",
        url="https://www.healthwellfoundation.org/",
        description=(
            "Independent charity providing copay, premium, and travel "
            "assistance grants across 70+ disease funds."
        ),
        category="copay_foundation",
        eligibility_note=(
            "Must have insurance covering the prescribed treatment; income "
            "thresholds vary by fund. Funds open and close as donations allow."
        ),
    ),
    AssistanceProgram(
        name="The Assistance Fund (TAF)",
        url="https://tafcares.org/",
        description=(
            "Copay, insurance premium, and travel assistance for patients "
            "with chronic and rare diseases."
        ),
        category="copay_foundation",
        eligibility_note=(
            "Must have insurance and meet income limits (typically up to "
            "500% FPL, fund-dependent)."
        ),
    ),
    AssistanceProgram(
        name="Good Days",
        url="https://www.mygooddays.org/",
        description=(
            "Copay assistance and other support for chronic-disease patients."
        ),
        category="copay_foundation",
        eligibility_note=(
            "Funds vary by disease and open/close based on availability. "
            "Usually requires insurance and income under ~500% FPL."
        ),
    ),
    AssistanceProgram(
        name="PAN Foundation (Patient Access Network)",
        url="https://www.panfoundation.org/disease-funds/",
        description=(
            "Disease-specific copay assistance funds for ~70 conditions. "
            "Funds open and close throughout the year - check the website "
            "or sign up for fund-open alerts."
        ),
        category="copay_foundation",
        eligibility_note=(
            "Insurance required; income limits typically 400-500% of FPL "
            "depending on the fund."
        ),
    ),
    AssistanceProgram(
        name="NORD (National Organization for Rare Disorders)",
        url="https://rarediseases.org/for-patients-and-families/help-access-medications/",
        description=(
            "Patient assistance, copay, and travel programs for people "
            "living with rare diseases."
        ),
        category="copay_foundation",
        diagnoses=frozenset(
            {
                "rare",
                "orphan",
                "hemophilia",
                "cystic fibrosis",
                "huntington",
                "als",
                "sma",
                "spinal muscular atrophy",
                "duchenne",
            }
        ),
    ),
    AssistanceProgram(
        name="RxAssist Patient Assistance Program Center",
        url="https://www.rxassist.org/",
        description=(
            "Comprehensive directory of manufacturer patient assistance "
            "programs. Search by medication to find the manufacturer's "
            "free-drug program if you are uninsured or under-insured."
        ),
        category="patient_assistance",
    ),
)


# Diagnosis-specific copay foundations. Listed separately so they only
# show up when the search matches their condition keywords.
DIAGNOSIS_SPECIFIC_PROGRAMS: tuple[AssistanceProgram, ...] = (
    AssistanceProgram(
        name="CancerCare Co-Payment Assistance Foundation",
        url="https://www.cancercarecopay.org/",
        description=(
            "Copay assistance for chemotherapy and targeted therapies for "
            "specific cancer diagnoses. Funds open/close by cancer type."
        ),
        category="copay_foundation",
        diagnoses=frozenset(
            {
                "cancer",
                "carcinoma",
                "lymphoma",
                "leukemia",
                "myeloma",
                "tumor",
                "neoplasm",
                "metastatic",
            }
        ),
    ),
    AssistanceProgram(
        name="Leukemia & Lymphoma Society Co-Pay Assistance",
        url="https://www.lls.org/support-resources/financial-support/co-pay-assistance-program",
        description=(
            "Copay, insurance premium, and travel assistance for patients "
            "with blood cancers (leukemia, lymphoma, myeloma, MDS)."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"leukemia", "lymphoma", "myeloma", "mds", "blood cancer"}),
    ),
    AssistanceProgram(
        name="National Multiple Sclerosis Society",
        url="https://www.nationalmssociety.org/Resources-Support/Financial-Resources",
        description=(
            "Financial assistance, prescription help, and treatment-cost "
            "navigation for people with MS."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"multiple sclerosis", "ms"}),
    ),
    AssistanceProgram(
        name="Crohn's & Colitis Foundation Patient Aid",
        url="https://www.crohnscolitisfoundation.org/patientsandcaregivers/financial-resources",
        description=(
            "Financial-resource navigator for IBD patients, plus their own "
            "small emergency assistance fund."
        ),
        category="copay_foundation",
        diagnoses=frozenset(
            {"crohn", "ulcerative colitis", "ibd", "inflammatory bowel"}
        ),
    ),
    AssistanceProgram(
        name="HIV/AIDS Drug Assistance Program (ADAP)",
        url="https://ryanwhite.hrsa.gov/about/parts-and-initiatives/part-b-adap",
        description=(
            "State-run programs (federally funded under Ryan White Part B) "
            "providing HIV medications to low-income patients. Eligibility "
            "and formulary vary by state."
        ),
        category="patient_assistance",
        diagnoses=frozenset({"hiv", "aids"}),
    ),
    AssistanceProgram(
        name="Hepatitis Foundation International",
        url="https://www.hepatitisfoundation.org/",
        description=(
            "Patient navigation and links to assistance programs for "
            "hepatitis B and C treatment costs."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"hepatitis", "hcv", "hbv"}),
    ),
    AssistanceProgram(
        name="National Hemophilia Foundation",
        url="https://www.hemophilia.org/educational-programs/financial-assistance-and-insurance",
        description=(
            "Information on copay, insurance, and emergency assistance for "
            "hemophilia and related bleeding disorders."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"hemophilia", "von willebrand", "bleeding disorder"}),
    ),
    AssistanceProgram(
        name="Cystic Fibrosis Foundation Compass",
        url="https://www.cff.org/managing-cf/cf-foundation-compass",
        description=(
            "1-on-1 navigation for CF patients on insurance, financial, and "
            "legal questions. Free service."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"cystic fibrosis", "cf"}),
        phone="1-844-COMPASS",
    ),
    AssistanceProgram(
        name="National Psoriasis Foundation Patient Navigation",
        url="https://www.psoriasis.org/patient-navigation-center/",
        description=(
            "Free 1-on-1 help with insurance access, prior auth, and copay "
            "assistance for psoriasis and psoriatic arthritis."
        ),
        category="copay_foundation",
        diagnoses=frozenset({"psoriasis", "psoriatic"}),
    ),
)


# Manufacturer copay cards for specific brand-name drugs. Most are limited
# to commercially-insured (non-government) patients. Linked from the drug
# detector via canonical generic names where applicable.
MANUFACTURER_PROGRAMS: tuple[AssistanceProgram, ...] = (
    AssistanceProgram(
        name="Wegovy Savings Card (Novo Nordisk)",
        url="https://www.wegovy.com/coverage-and-savings.html",
        description=(
            "Manufacturer copay card for commercially-insured patients. "
            "May reduce out-of-pocket cost - read the fine print on coverage "
            "and OOP-max accumulator rules."
        ),
        category="manufacturer",
        drugs=frozenset({"wegovy"}),
        eligibility_note=(
            "Not available to patients with Medicare, Medicaid, or other "
            "government insurance."
        ),
    ),
    AssistanceProgram(
        name="Ozempic Savings Offer (Novo Nordisk)",
        url="https://www.ozempic.com/savings-and-resources/save-on-ozempic.html",
        description=(
            "Manufacturer copay assistance for commercially-insured patients "
            "with type-2 diabetes."
        ),
        category="manufacturer",
        drugs=frozenset({"ozempic"}),
        eligibility_note=(
            "Commercial insurance only. Off-label (e.g. weight-loss) use is "
            "typically excluded."
        ),
    ),
    AssistanceProgram(
        name="Mounjaro Savings Card (Lilly)",
        url="https://www.mounjaro.com/savings-resources",
        description=(
            "Lilly copay card for commercially-insured type-2 diabetes " "patients."
        ),
        category="manufacturer",
        drugs=frozenset({"mounjaro"}),
        eligibility_note="Commercial insurance only.",
    ),
    AssistanceProgram(
        name="Zepbound Savings Card (Lilly)",
        url="https://www.zepbound.lilly.com/coverage-savings",
        description=(
            "Lilly savings program for commercially-insured patients "
            "prescribed Zepbound for chronic weight management."
        ),
        category="manufacturer",
        drugs=frozenset({"zepbound"}),
        eligibility_note="Commercial insurance only.",
    ),
    AssistanceProgram(
        name="Humira Complete Savings Card (AbbVie)",
        url="https://www.humira.com/humira-complete/cost-and-copay",
        description=(
            "Copay card for commercially-insured Humira patients - may bring "
            "copay to as little as $5/month for eligible plans."
        ),
        category="manufacturer",
        drugs=frozenset({"humira"}),
        eligibility_note="Commercial insurance only.",
    ),
    AssistanceProgram(
        name="Enbrel Support Copay Card (Amgen)",
        url="https://www.enbrel.com/support/savings-and-cost",
        description="Manufacturer copay card for commercially-insured Enbrel patients.",
        category="manufacturer",
        drugs=frozenset({"enbrel"}),
        eligibility_note="Commercial insurance only.",
    ),
    AssistanceProgram(
        name="Dupixent MyWay Copay Card (Sanofi/Regeneron)",
        url="https://www.dupixent.com/support-savings/copay-card",
        description="Copay assistance for commercially-insured Dupixent patients.",
        category="manufacturer",
        drugs=frozenset({"dupixent"}),
        eligibility_note="Commercial insurance only.",
    ),
)


# Safety-net and 340B resources. These help patients connect to
# federally-supported clinics that offer drugs at deeply discounted
# 340B-program prices regardless of insurance.
SAFETY_NET_PROGRAMS: tuple[AssistanceProgram, ...] = (
    AssistanceProgram(
        name="HRSA Find a Health Center (FQHC locator)",
        url="https://findahealthcenter.hrsa.gov/",
        description=(
            "Federally Qualified Health Centers (FQHCs) provide primary "
            "care on a sliding-fee scale and dispense many medications at "
            "340B-discounted prices, regardless of insurance status. Often "
            "the cheapest path for uninsured or under-insured patients."
        ),
        category="safety_net",
    ),
    AssistanceProgram(
        name="Ryan White HIV/AIDS Program Service Locator",
        url="https://ryanwhite.hrsa.gov/hiv-care/services",
        description=(
            "Ryan White-funded clinics provide HIV care, medications, and "
            "wraparound services for low-income patients. Most are 340B "
            "providers with deeply discounted antiretroviral pricing."
        ),
        category="safety_net",
        diagnoses=frozenset({"hiv", "aids"}),
    ),
    AssistanceProgram(
        name="Hemophilia Treatment Center Directory",
        url="https://www.cdc.gov/hemophilia/htc.html",
        description=(
            "CDC-recognized HTCs are 340B-covered entities that dispense "
            "clotting-factor products at discount, often via in-house "
            "pharmacies."
        ),
        category="safety_net",
        diagnoses=frozenset({"hemophilia", "von willebrand", "bleeding disorder"}),
    ),
    AssistanceProgram(
        name="HRSA 340B Program (provider directory + program info)",
        url="https://www.hrsa.gov/opa",
        description=(
            "Background on the 340B Drug Pricing Program. Use to verify "
            "whether a particular clinic or hospital is a covered entity "
            "before assuming discounted pricing applies."
        ),
        category="safety_net",
    ),
    AssistanceProgram(
        name="State Pharmaceutical Assistance Program (SPAP) directory",
        url="https://www.medicare.gov/pharmaceutical-assistance-program/state-programs.aspx",
        description=(
            "State-run programs (mostly for seniors and people with "
            "disabilities) that supplement Medicare Part D and reduce "
            "prescription costs. Availability and benefits vary by state."
        ),
        category="medicaid",
    ),
    AssistanceProgram(
        name="Medicaid eligibility & application (Healthcare.gov)",
        url="https://www.healthcare.gov/medicaid-chip/getting-medicaid-chip/",
        description=(
            "If denied insurance is unaffordable, check Medicaid / CHIP "
            "eligibility - thresholds and pathways vary by state, and many "
            "expansion states cover adults up to 138% FPL."
        ),
        category="medicaid",
    ),
)


def _normalize(text: str) -> str:
    return text.lower()


def _diagnosis_matches(program: AssistanceProgram, diagnosis_text: str) -> bool:
    """Return True if any of `program.diagnoses` appears in the diagnosis text."""
    if not program.diagnoses:
        return False
    haystack = _normalize(diagnosis_text)
    for needle in program.diagnoses:
        # Match whole-word or prefix to handle e.g. "crohn" matching
        # "Crohn's disease".
        pattern = r"\b" + re.escape(needle)
        if re.search(pattern, haystack):
            return True
    return False


def _drug_matches(program: AssistanceProgram, canonical_drug: str) -> bool:
    return bool(program.drugs) and canonical_drug in program.drugs


@dataclass
class FinancialAssistanceResults:
    """
    Aggregated results from a financial-assistance search.

    Includes diagnosis-matched copay foundations, drug-matched manufacturer
    programs, the always-applicable general programs, safety-net resources,
    and (when a state was supplied) a link to the state-specific Medicaid
    pathway.
    """

    canonical_drug: Optional[str] = None
    diagnosis_text: Optional[str] = None
    state_abbreviation: Optional[str] = None

    diagnosis_specific: list[AssistanceProgram] = field(default_factory=list)
    manufacturer: list[AssistanceProgram] = field(default_factory=list)
    general: list[AssistanceProgram] = field(default_factory=list)
    safety_net: list[AssistanceProgram] = field(default_factory=list)

    # Populated when a state abbreviation is supplied and the state's
    # Medicaid info is available.
    state_medicaid_name: Optional[str] = None
    state_medicaid_url: Optional[str] = None
    state_medicaid_phone: Optional[str] = None

    def is_empty(self) -> bool:
        """
        True only when no programs at all and no state Medicaid pathway are
        attached. Note: `search()` always populates `general` and the
        untagged `safety_net` entries, so in practice this is rarely true -
        callers that want to gate on actual relevance to the patient should
        use `has_specific_matches()` instead.
        """
        return not (
            self.diagnosis_specific
            or self.manufacturer
            or self.general
            or self.safety_net
            or self.state_medicaid_name
        )

    def has_specific_matches(self) -> bool:
        """
        True when the search produced a program tied to the patient's
        specific drug, diagnosis, or state - i.e. the directory is more
        useful than just showing the always-included general copay
        foundation directories.

        Use this (not `is_empty()`) to decide whether to surface the
        directory section in a UI: it returns True only when there is
        something genuinely targeted to render.
        """
        return bool(
            self.diagnosis_specific or self.manufacturer or self.state_medicaid_name
        )

    def all_programs(self) -> list[AssistanceProgram]:
        """Convenience: every program in a single ordered list."""
        return [
            *self.diagnosis_specific,
            *self.manufacturer,
            *self.general,
            *self.safety_net,
        ]


def _resolve_canonical_drug(drug: Optional[str]) -> Optional[str]:
    """
    Resolve a drug-name input to a canonical lower-case name when - and only
    when - the detector recognizes it.

    Returns None for unrecognized inputs (e.g. non-drug procedures like
    "MRI of knee") so callers can treat `canonical_drug` as a reliable
    "we identified a known drug" signal rather than echoing back arbitrary
    procedure text.
    """
    if not drug:
        return None
    detected: Optional[str] = detect_drug(drug)
    return detected


def search(
    drug: Optional[str] = None,
    diagnosis: Optional[str] = None,
    denial_text: Optional[str] = None,
    state_abbreviation: Optional[str] = None,
) -> FinancialAssistanceResults:
    """
    Search the financial-assistance directory.

    At least one of `drug`, `diagnosis`, or `denial_text` should be supplied
    (otherwise only the general/safety-net catalog is returned).

    `state_abbreviation` (e.g. "CA", "MA") attaches a link to the state's
    Medicaid agency from the existing state_help catalog when available.
    """
    canonical_drug = _resolve_canonical_drug(drug)
    if canonical_drug is None:
        # Try to detect a drug from the denial text / diagnosis as a fallback.
        detected = detect_drug(denial_text, diagnosis)
        if detected:
            canonical_drug = detected

    diagnosis_haystack_parts = [s for s in (diagnosis, denial_text) if s]
    diagnosis_haystack = " ".join(diagnosis_haystack_parts)

    # Normalize the state once. We use the upper-cased form for both the
    # payload field and the state_help lookup so callers can pass "ca",
    # "Ca", or "CA" and get a consistent result.
    normalized_state: Optional[str] = (
        state_abbreviation.upper() if state_abbreviation else None
    )

    results = FinancialAssistanceResults(
        canonical_drug=canonical_drug,
        diagnosis_text=diagnosis or None,
        state_abbreviation=normalized_state,
    )

    if diagnosis_haystack:
        results.diagnosis_specific = [
            p
            for p in DIAGNOSIS_SPECIFIC_PROGRAMS
            if _diagnosis_matches(p, diagnosis_haystack)
        ]

    if canonical_drug:
        results.manufacturer = [
            p for p in MANUFACTURER_PROGRAMS if _drug_matches(p, canonical_drug)
        ]

    # General programs always apply - the directory's value is helping
    # patients who don't already know where to look.
    results.general = list(GENERAL_PROGRAMS)

    # Safety-net programs: include the un-tagged ones plus any that match
    # the diagnosis (e.g. Ryan White for HIV).
    results.safety_net = [
        p
        for p in SAFETY_NET_PROGRAMS
        if not p.diagnoses
        or (diagnosis_haystack and _diagnosis_matches(p, diagnosis_haystack))
    ]

    # State Medicaid pathway - look up via the existing state_help module so
    # we don't duplicate the per-state catalog.
    if normalized_state:
        try:
            from fighthealthinsurance.state_help import (
                get_state_help_by_abbreviation,
            )

            state_help = get_state_help_by_abbreviation(normalized_state)
            if state_help and state_help.medicaid:
                results.state_medicaid_name = state_help.medicaid.agency_name
                results.state_medicaid_url = state_help.medicaid.agency_url
                results.state_medicaid_phone = state_help.medicaid.agency_phone
        except Exception:
            # state_help is best-effort; never break callers if it fails.
            pass

    return results

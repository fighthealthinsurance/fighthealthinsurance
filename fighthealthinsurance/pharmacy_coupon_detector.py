"""
Pharmacy coupon and discount program detector.

Detects when a denial concerns a prescription medication and suggests
discount pharmacy alternatives (GoodRx, Cost Plus Drugs, Amazon Pharmacy)
that may serve as a "bridge" while the patient fights their denial.

Important caveat: amounts paid through discount programs typically do NOT
count toward the patient's deductible or out-of-pocket (OOP) maximum, so
these are bridge solutions for genuinely cheap drugs, not a substitute
for the appeal itself.
"""

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

# Drugs that are typically inexpensive without insurance (under ~$50/month
# at retail pharmacies via discount cards). Suitable as a "bridge" while
# appealing. Names are matched case-insensitively as whole words.
#
# Stored as a tuple so iteration order is stable across runs (and so we can
# still build a frozenset for fast membership checks below).
#
# Sources for inclusion: GoodRx low-cost lists, Mark Cuban Cost Plus Drugs
# catalog, common $4 generic lists at major retailers.
CHEAP_GENERIC_DRUGS_ORDERED: tuple[str, ...] = (
    # Cardiovascular
    "amlodipine",
    "atorvastatin",
    "carvedilol",
    "clopidogrel",
    "furosemide",
    "hydrochlorothiazide",
    "lisinopril",
    "losartan",
    "metoprolol",
    "pravastatin",
    "rosuvastatin",
    "simvastatin",
    # Diabetes (oral generics only - GLP-1s like Ozempic are NOT cheap)
    "glipizide",
    "metformin",
    # Mental health / neuro
    "alprazolam",
    "bupropion",
    "buspirone",
    "citalopram",
    "duloxetine",
    "escitalopram",
    "fluoxetine",
    "gabapentin",
    "lamotrigine",
    "mirtazapine",
    "paroxetine",
    "sertraline",
    "trazodone",
    "venlafaxine",
    # GI
    "famotidine",
    "omeprazole",
    "pantoprazole",
    "ranitidine",
    # Respiratory / allergy
    "albuterol",
    "cetirizine",
    "fluticasone",
    "loratadine",
    "montelukast",
    # Antibiotics
    "amoxicillin",
    "azithromycin",
    "cephalexin",
    "ciprofloxacin",
    "doxycycline",
    # Hormones / HRT
    "estradiol",
    "levothyroxine",
    "progesterone",
    "spironolactone",
    "testosterone",
    # Pain
    "diclofenac",
    "ibuprofen",
    "meloxicam",
    "naproxen",
    # Other common
    "allopurinol",
    "finasteride",
    "prednisone",
    "tamsulosin",
    "warfarin",
)
CHEAP_GENERIC_DRUGS: frozenset[str] = frozenset(CHEAP_GENERIC_DRUGS_ORDERED)


# Drug brand-name to generic-name aliases. Many patients/denials reference
# the brand name even when a cheap generic exists.
#
# IMPORTANT: only include brand names whose primary active ingredient IS the
# mapped generic. Combination products (e.g., Advair = fluticasone+salmeterol,
# Symbicort = budesonide+formoterol) MUST NOT be aliased to one component
# generic - the combination is typically expensive and not interchangeable
# with the single-ingredient generic. Combo brands belong in EXPENSIVE_DRUGS
# instead so they're detected but messaged correctly.
BRAND_TO_GENERIC: dict[str, str] = {
    "celexa": "citalopram",
    "cipro": "ciprofloxacin",
    "cymbalta": "duloxetine",
    "effexor": "venlafaxine",
    "flonase": "fluticasone",
    "glucophage": "metformin",
    "lasix": "furosemide",
    "lexapro": "escitalopram",
    "lipitor": "atorvastatin",
    "neurontin": "gabapentin",
    "norvasc": "amlodipine",
    "paxil": "paroxetine",
    "prilosec": "omeprazole",
    "prinivil": "lisinopril",
    "propecia": "finasteride",
    "prozac": "fluoxetine",
    "singulair": "montelukast",
    "synthroid": "levothyroxine",
    "ventolin": "albuterol",
    "wellbutrin": "bupropion",
    "xanax": "alprazolam",
    "zestril": "lisinopril",
    "zocor": "simvastatin",
    "zoloft": "sertraline",
    "zyrtec": "cetirizine",
}


# Drugs that are well-known to be expensive even with discount programs.
# These get a different message: discount programs may help marginally but
# the appeal is the patient's primary path.
#
# Includes combination inhalers/products like Advair (fluticasone+salmeterol)
# - they're commonly denied for non-formulary reasons and we want to detect
# them, but the cash-pay "bridge" guidance for cheap generics doesn't apply.
EXPENSIVE_DRUGS_ORDERED: tuple[str, ...] = (
    "advair",
    "dupixent",
    "enbrel",
    "humira",
    "mounjaro",
    "ozempic",
    "rinvoq",
    "saxenda",
    "skyrizi",
    "stelara",
    "symbicort",
    "trulicity",
    "wegovy",
    "zepbound",
)
EXPENSIVE_DRUGS: frozenset[str] = frozenset(EXPENSIVE_DRUGS_ORDERED)


# Generic/keyword cues that suggest the denial is for a prescription drug
# even if no specific drug name is detected.
PRESCRIPTION_CUES: tuple[str, ...] = (
    "formulary",
    "non-formulary",
    "non formulary",
    "step therapy",
    "prior authorization for medication",
    "prescription drug",
    "tier exception",
    "specialty drug",
    "specialty pharmacy",
    "preferred drug",
    "non-preferred drug",
    "medication is not covered",
    "drug is not covered",
)


@dataclass
class PharmacyOption:
    """A discount pharmacy option for a given drug."""

    name: str
    url: str
    description: str
    # Whether amounts paid here typically count toward the patient's OOP max.
    # For all of these (GoodRx, Cost Plus Drugs, Amazon Pharmacy without
    # insurance) the answer is False; insurance must process the claim for
    # OOP credit.
    counts_toward_oop_max: bool = False


@dataclass
class PharmacyCouponSuggestion:
    """
    A pharmacy/coupon suggestion produced by the detector.

    `drug_name` is the canonical (lower-case) name of the medication detected.
    `is_likely_cheap` indicates whether the drug is on our cheap-generics list
    and thus a viable bridge while fighting the denial. When False, the
    patient should still fight the denial because discount programs are
    unlikely to make the drug affordable on their own.
    """

    drug_name: str
    is_likely_cheap: bool
    pharmacy_options: list[PharmacyOption] = field(default_factory=list)
    bridge_message: str = ""
    oop_max_warning: str = (
        "Important: amounts paid out-of-pocket through discount programs "
        "typically do NOT count toward your insurance deductible or "
        "out-of-pocket maximum. Continue your appeal to get the medication "
        "covered through insurance."
    )


def _normalize(text: str) -> str:
    return text.lower()


def _contains_word(text: str, word: str) -> bool:
    """Return True if `word` appears as a whole word in `text` (case-insensitive)."""
    pattern = r"\b" + re.escape(word) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _build_goodrx_option(drug_name: str) -> PharmacyOption:
    slug = urllib.parse.quote(drug_name.lower())
    return PharmacyOption(
        name="GoodRx",
        url=f"https://www.goodrx.com/{slug}",
        description=(
            "Free discount card accepted at most US pharmacies. Compare prices "
            "across nearby pharmacies and present the GoodRx coupon at the counter."
        ),
    )


def _build_costplus_option(drug_name: str) -> PharmacyOption:
    slug = urllib.parse.quote(drug_name.lower())
    return PharmacyOption(
        name="Mark Cuban Cost Plus Drugs",
        url=f"https://costplusdrugs.com/medications/?search={slug}",
        description=(
            "Mail-order pharmacy with transparent pricing (manufacturer cost + "
            "15% markup + dispensing fee). Carries many common generics."
        ),
    )


def _build_amazon_option(drug_name: str) -> PharmacyOption:
    slug = urllib.parse.quote(drug_name.lower())
    return PharmacyOption(
        name="Amazon Pharmacy",
        url=f"https://pharmacy.amazon.com/search?query={slug}",
        description=(
            "Mail-order pharmacy. Prime members can see discounted prices "
            "without insurance via the Prime Rx benefit."
        ),
    )


def detect_drug(*texts: Optional[str]) -> Optional[str]:
    """
    Look for a known drug name across one or more text fields.

    Within a single text field we collect every brand/generic match (using
    ordered tuples so iteration is deterministic) and return the one whose
    match starts earliest in the text - that is usually the drug the writer
    is talking about, and removes any dependence on dict/set ordering.
    Brand names are normalized to their generic equivalent when known so
    callers receive a consistent canonical name. Texts are scanned in the
    order supplied; the first text containing any match wins.
    """
    for text in texts:
        if not text:
            continue
        normalized = _normalize(text)
        candidates: list[tuple[int, str]] = []

        # Brand names map to a generic; iterate dict in insertion order.
        for brand, generic in BRAND_TO_GENERIC.items():
            m = re.search(r"\b" + re.escape(brand) + r"\b", normalized, re.IGNORECASE)
            if m is not None:
                candidates.append((m.start(), generic))

        for drug in CHEAP_GENERIC_DRUGS_ORDERED:
            m = re.search(r"\b" + re.escape(drug) + r"\b", normalized, re.IGNORECASE)
            if m is not None:
                candidates.append((m.start(), drug))

        for drug in EXPENSIVE_DRUGS_ORDERED:
            m = re.search(r"\b" + re.escape(drug) + r"\b", normalized, re.IGNORECASE)
            if m is not None:
                candidates.append((m.start(), drug))

        if candidates:
            # Earliest position wins. Stable sort keeps brand-priority for
            # ties at the same position.
            candidates.sort(key=lambda c: c[0])
            return candidates[0][1]
    return None


def looks_like_prescription_denial(*texts: Optional[str]) -> bool:
    """Return True when the denial text contains prescription-drug cues."""
    for text in texts:
        if not text:
            continue
        normalized = _normalize(text)
        for cue in PRESCRIPTION_CUES:
            if cue in normalized:
                return True
    return False


def build_suggestion(drug_name: str) -> PharmacyCouponSuggestion:
    """
    Build a pharmacy/coupon suggestion for the given (already-detected) drug.

    Drug name is treated case-insensitively.
    """
    name = drug_name.strip().lower()
    is_cheap = name in CHEAP_GENERIC_DRUGS

    options = [
        _build_goodrx_option(name),
        _build_costplus_option(name),
        _build_amazon_option(name),
    ]

    if is_cheap:
        bridge_message = (
            f"{name.title()} is typically inexpensive without insurance. "
            "While you fight this denial, consider paying out-of-pocket using "
            "a pharmacy discount program as a short-term bridge so you don't "
            "miss doses."
        )
    else:
        bridge_message = (
            f"{name.title()} is usually expensive even with discount cards, "
            "so the appeal is your primary path to coverage. Discount programs "
            "below may still reduce the cost somewhat, but check the price "
            "before assuming they make the drug affordable."
        )

    return PharmacyCouponSuggestion(
        drug_name=name,
        is_likely_cheap=is_cheap,
        pharmacy_options=options,
        bridge_message=bridge_message,
    )


def suggest_for_denial(
    denial_text: Optional[str] = None,
    procedure: Optional[str] = None,
    diagnosis: Optional[str] = None,
    drug: Optional[str] = None,
) -> Optional[PharmacyCouponSuggestion]:
    """
    Top-level entry point. Given the fields available on a Denial (or a
    ChatLeads.drug value), return a pharmacy coupon suggestion if applicable.

    Returns None when nothing prescription-related is detected.
    """
    # Explicit drug field wins if provided.
    if drug:
        detected: Optional[str] = detect_drug(drug) or drug.strip().lower()
        if detected:
            return build_suggestion(detected)

    detected = detect_drug(procedure, diagnosis, denial_text)
    if detected:
        return build_suggestion(detected)

    # No specific drug, but the denial smells like a prescription denial -
    # return a generic suggestion pointing the patient at GoodRx so they can
    # look up their own medication.
    if looks_like_prescription_denial(denial_text, procedure, diagnosis):
        return PharmacyCouponSuggestion(
            drug_name="",
            is_likely_cheap=False,
            pharmacy_options=[
                PharmacyOption(
                    name="GoodRx",
                    url="https://www.goodrx.com/",
                    description=(
                        "Look up your specific medication to compare retail "
                        "prices at nearby pharmacies."
                    ),
                ),
                PharmacyOption(
                    name="Mark Cuban Cost Plus Drugs",
                    url="https://costplusdrugs.com/medications/",
                    description=(
                        "Transparent-pricing mail-order pharmacy. Worth checking "
                        "if your medication is a generic."
                    ),
                ),
                PharmacyOption(
                    name="Amazon Pharmacy",
                    url="https://pharmacy.amazon.com/",
                    description=(
                        "Mail-order pharmacy with discounted cash prices, "
                        "especially for Prime members."
                    ),
                ),
            ],
            bridge_message=(
                "This looks like a prescription drug denial. If your medication "
                "is a generic, paying cash with a discount program may be cheaper "
                "than your insurance copay - a useful bridge while you appeal."
            ),
        )

    return None

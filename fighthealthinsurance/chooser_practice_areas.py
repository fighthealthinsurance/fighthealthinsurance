import re
from typing import Optional

PRACTICE_AREAS = {
    "primary-care": "Primary care / general medicine",
    "cardiology": "Cardiology / heart care",
    "oncology": "Oncology / cancer care",
    "orthopedics": "Orthopedics / musculoskeletal care",
    "neurology": "Neurology",
    "mental-health": "Mental health / psychiatry",
    "womens-health": "Women's health / OB-GYN",
    "gender-affirming-care": "Gender-affirming / trans care",
    "pediatrics": "Pediatrics",
    "gastroenterology": "Gastroenterology / digestive health",
    "endocrinology": "Endocrinology / diabetes and hormone care",
    "pulmonology": "Pulmonology / respiratory care",
    "rheumatology": "Rheumatology / autoimmune care",
    "nephrology": "Nephrology / kidney care",
    "dermatology": "Dermatology / skin care",
    "physical-medicine": "Physical medicine and rehabilitation",
    "emergency-medicine": "Emergency medicine",
}


_PRACTICE_AREA_TERMS = {
    "primary-care": (
        "primary care",
        "general medicine",
        "family medicine",
        "annual physical",
        "routine checkup",
        "preventive",
        "screening",
        "vaccin*",
        "immuniz*",
        "hypertension",
        "blood pressure",
        "diabet*",
    ),
    "cardiology": (
        "cardiolog*",
        "heart",
        "cardiac",
        "coronary",
        "echocardi*",
        "electrocardi*",
        "ecg",
        "ekg",
        "arrhythm*",
        "atrial fibrillation",
        "hypertension",
        "cholesterol",
        "statin*",
        "angina",
        "myocardial",
        "pacemaker",
        "lvad",
        "tavr",
    ),
    "oncology": (
        "oncolog*",
        "cancer",
        "tumor",
        "tumour",
        "carcinoma",
        "malignan*",
        "chemotherap*",
        "immunotherap*",
        "metasta*",
        "lymphoma",
        "leukemia",
        "melanoma",
    ),
    "orthopedics": (
        "orthop*",
        "musculoskeletal",
        "knee",
        "hip",
        "shoulder",
        "back pain",
        "spine",
        "spinal",
        "fracture",
        "joint",
        "arthritis",
        "acl",
        "meniscus",
        "rotator cuff",
    ),
    "neurology": (
        "neurolog*",
        "brain",
        "seizure",
        "epilep*",
        "migraine",
        "stroke",
        "multiple sclerosis",
        "parkinson*",
        "dementia",
        "alzheimer*",
        "neuropath*",
        "headache",
        "concussion",
    ),
    "mental-health": (
        "mental health",
        "psychiatr*",
        "psycholog*",
        "depression",
        "depressive",
        "anxiety",
        "panic",
        "bipolar",
        "schizophren*",
        "ptsd",
        "trauma therapy",
        "antidepressant*",
        "ssri",
        "therapy session",
        "counseling",
        "counselling",
    ),
    "womens-health": (
        "ob gyn",
        "obgyn",
        "gynecolog*",
        "gynaecolog*",
        "obstetric*",
        "pregnan*",
        "prenatal",
        "postpartum",
        "menstru*",
        "uter*",
        "ovari*",
        "cervix",
        "cervical cancer",
        "cervical screening",
        "cervical dysplasia",
        "cervical polyp",
        "cervical biopsy",
        "hpv",
        "human papillomavirus",
        "breast",
        "mammogr*",
        "menopaus*",
        "endometri*",
        "pcos",
        "contracept*",
        "birth control",
    ),
    "gender-affirming-care": (
        # Identity and diagnosis vocabulary. "trans" is a whole-word term
        # (not "trans*") so transplant and transfusion stay out.
        "gender affirming",
        "gender affirmation",
        "gender confirming",
        "gender confirmation",
        "gender dysphoria",
        "f64",
        "gender incongruence",
        "gender identity",
        "gender reassignment",
        "sex reassignment",
        "transgender",
        "transsexual*",
        "trans",
        "nonbinary",
        "non binary",
        "genderqueer",
        "wpath",
        # Treatments: both the procedure and the diagnosis must match, so a
        # shared term like "mastectomy" still needs a gender diagnosis.
        "hormone therapy",
        "hrt",
        "estradiol",
        "estrogen",
        "testosterone",
        "spironolactone",
        "cyproterone",
        "puberty blocker",
        "puberty blockers",
        "gnrh",
        "leuprolide",
        "lupron",
        "triptorelin",
        "histrelin",
        "puberty suppression",
        "feminiz*",
        "masculiniz*",
        "top surgery",
        "bottom surgery",
        "vaginoplasty",
        "phalloplasty",
        "metoidioplasty",
        "orchiectom*",
        "mastectom*",
        "hysterectom*",
        "breast augmentation",
        "tracheal shave",
        "chondrolaryngoplasty",
        "electrolysis",
        "hair removal",
    ),
    "pediatrics": (
        "pediatric*",
        "paediatric*",
        "child",
        "children",
        "infant",
        "newborn",
        "neonat*",
        "adolescen*",
        "baby",
        "toddler",
    ),
    "gastroenterology": (
        "gastro*",
        "digestive",
        "stomach",
        "abdominal",
        "liver",
        "hepat*",
        "pancrea*",
        "colon",
        "intestin*",
        "bowel",
        "crohn*",
        "colitis",
        "celiac",
        "reflux",
        "gerd",
        "endoscopy",
        "colonoscopy",
        "diarrhea",
        "constipation",
    ),
    "endocrinology": (
        "endocrin*",
        "diabet*",
        "insulin",
        "thyroid",
        "hormone",
        "adrenal",
        "pituitary",
        "hypoglyc*",
        "hyperglyc*",
        "metabolic",
        "osteoporosis",
    ),
    "pulmonology": (
        "pulmon*",
        "lung",
        "respiratory",
        "asthma",
        "copd",
        "bronch*",
        "pneumonia",
        "dyspnea",
        "shortness of breath",
        "oxygen",
        "sleep apnea",
        "cpap",
    ),
    "rheumatology": (
        "rheumat*",
        "autoimmune",
        "lupus",
        "arthritis",
        "psoria*",
        "vasculitis",
        "scleroderma",
        "sjogren*",
        "ankylosing",
        "gout",
        "fibromyalgia",
    ),
    "nephrology": (
        "nephr*",
        "kidney",
        "renal",
        "dialysis",
        "creatinine",
        "proteinuria",
        "glomerul*",
        "electrolyte",
    ),
    "dermatology": (
        "dermat*",
        "skin",
        "rash",
        "eczema",
        "psoriasis",
        "acne",
        "melanoma",
        "skin lesion",
        "mole",
    ),
    "physical-medicine": (
        "physical medicine",
        "rehabilitation",
        "rehab",
        "physical therapy",
        "occupational therapy",
        "physiatr*",
        "mobility",
        "prosthetic*",
        "orthotic*",
        "gait",
    ),
    "emergency-medicine": (
        "emergency",
        "emergency department",
        "emergency room",
        "trauma",
        "resuscitation",
        "triage",
        "overdose",
        "poisoning",
        "anaphylaxis",
        "chest pain",
    ),
}


# Vocabulary that rules text out of an area even when one of its terms
# matches. Gender-affirming care shares treatment and hormone words with
# other specialties ("mastectomy", "estrogen", "testosterone"), so without
# this a mastectomy for estrogen-receptor-positive breast cancer, or
# testosterone for hypogonadism, would pass as gender-affirming care.
_PRACTICE_AREA_EXCLUDED_TERMS = {
    "gender-affirming-care": (
        "cancer",
        "carcinoma",
        "malignan*",
        "tumor",
        "tumour",
        "metasta*",
        "neoplas*",
        "oncolog*",
        "lymphoma",
        "leukemia",
        "sarcoma",
        "hypogonad*",
        "low testosterone",
        "testosterone deficiency",
    ),
}


def _has_any_term(terms: tuple, tokens: set, normalized: str) -> bool:
    """Whether any term matches: a trailing "*" is a single-token prefix,
    anything else a whole-word phrase in the space-joined ``normalized``."""
    for term in terms:
        if term.endswith("*"):
            if any(token.startswith(term[:-1]) for token in tokens):
                return True
        elif f" {term} " in normalized:
            return True
    return False


def practice_area_text_matches(raw_area: object, text: object) -> bool:
    """Whether generated text contains vocabulary for the requested area."""
    area = normalize_practice_area(raw_area)
    if area is None or not isinstance(text, str):
        return False
    words = re.findall(r"[a-z0-9]+", text.casefold())
    tokens = set(words)
    normalized = f" {' '.join(words)} "
    if _has_any_term(_PRACTICE_AREA_EXCLUDED_TERMS.get(area, ()), tokens, normalized):
        return False
    return _has_any_term(_PRACTICE_AREA_TERMS[area], tokens, normalized)


def normalize_practice_area(raw: object) -> Optional[str]:
    """A supported practice-area slug, or None for absent/unknown input."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if value in PRACTICE_AREAS else None


def practice_area_label(raw: object) -> Optional[str]:
    """The display and generation label for a supported practice area."""
    slug = normalize_practice_area(raw)
    return PRACTICE_AREAS.get(slug) if slug is not None else None

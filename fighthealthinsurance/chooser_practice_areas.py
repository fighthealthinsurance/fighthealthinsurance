import re
from typing import Optional, Sequence

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
        # Only gender-specific vocabulary; the treatments it shares with
        # other specialties are in _PRACTICE_AREA_SHARED_TERMS. "trans" is
        # a whole-word term (not "trans*") so transplant and transfusion
        # stay out.
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
        "feminiz*",
        # Not "feminis*"/"masculinis*": those read "feminist"/"masculinist".
        "feminisation",
        "feminising",
        "feminised",
        "masculiniz*",
        "masculinisation",
        "masculinising",
        "masculinised",
        "transmasc*",
        # Not "transfem*": that reads "transfemoral" (an amputation, a
        # catheter route) as gender-affirming care.
        "transfem",
        "transfemme*",
        "transfemin*",
        "top surgery",
        "bottom surgery",
        "vaginoplasty",
        "phalloplasty",
        "metoidioplasty",
        "tracheal shave",
        "chondrolaryngoplasty",
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


# Vocabulary an area shares with other specialties: it counts toward the
# area only alongside one of the area's own terms (see
# practice_area_texts_match). Gender-affirming care's hormones and surgeries
# are also cancer, menopause and hypogonadism care, so "Mastectomy" for
# "Estrogen-receptor-positive DCIS" must not pass as gender-affirming care,
# while "Mastectomy" for "Gender dysphoria" must.
_PRACTICE_AREA_SHARED_TERMS = {
    "gender-affirming-care": (
        "hormone therapy",
        "hormone replacement therapy",
        "hrt",
        "estradiol",
        "estrogen",
        "testosterone",
        "spironolactone",
        "cyproterone",
        "puberty blocker",
        "puberty blockers",
        "puberty suppression",
        "gnrh",
        "leuprolide",
        "lupron",
        "triptorelin",
        "histrelin",
        "orchiectom*",
        "mastectom*",
        "hysterectom*",
        "breast augmentation",
        "breast reduction",
        "chest reconstruction",
        "gonadectom*",
        "scrotectom*",
        "voice therapy",
        "laryngoplasty",
        "glottoplasty",
        "electrolysis",
        "hair removal",
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


def _normalized(text: str) -> tuple:
    """The text's lowercase word tokens, and those words space-joined."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return set(words), f" {' '.join(words)} "


def practice_area_texts_match(raw_area: object, texts: Sequence[object]) -> bool:
    """Whether generated texts that describe one case (a procedure and a
    diagnosis, or a conversation's user turns) all belong to the requested
    area: each must contain the area's vocabulary, and at least one must
    contain a term of the area's own rather than one it shares."""
    area = normalize_practice_area(raw_area)
    if area is None or not texts or not all(isinstance(t, str) for t in texts):
        return False
    own = _PRACTICE_AREA_TERMS[area]
    shared = _PRACTICE_AREA_SHARED_TERMS.get(area, ())
    anchored = False
    for text in texts:
        tokens, normalized = _normalized(str(text))
        if _has_any_term(own, tokens, normalized):
            anchored = True
        elif not _has_any_term(shared, tokens, normalized):
            return False
    return anchored


def practice_area_text_matches(raw_area: object, text: object) -> bool:
    """Whether one piece of generated text belongs to the requested area."""
    return practice_area_texts_match(raw_area, [text])


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

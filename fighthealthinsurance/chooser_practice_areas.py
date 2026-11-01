from typing import Optional

PRACTICE_AREAS = {
    "primary-care": "Primary care / general medicine",
    "cardiology": "Cardiology / heart care",
    "oncology": "Oncology / cancer care",
    "orthopedics": "Orthopedics / musculoskeletal care",
    "neurology": "Neurology",
    "mental-health": "Mental health / psychiatry",
    "womens-health": "Women's health / OB-GYN",
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

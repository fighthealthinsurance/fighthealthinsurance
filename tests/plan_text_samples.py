"""Plan document text written for tests, and the kinds of garbage a PDF's
text layer can read as. Made up: no real plan or person."""

import random

PLAN_TEXT = (
    "Summary of Benefits and Coverage: what this plan covers and what you pay "
    "for covered services. This is only a summary. If you have other family "
    "members on the plan, each member must meet their own deductible until the "
    "total amount paid by all members meets the family deductible. You will pay "
    "less if you use a provider in the plan's network. Prior authorization is "
    "required for imaging and for an inpatient stay."
)
TABLE_TEXT = """Primary care visit to treat an injury or illness $30 copay/visit 40% coinsurance None
Specialist visit $60 copay/visit 40% coinsurance Prior authorization required
Diagnostic test (x-ray, blood work) 20% coinsurance 40% coinsurance None
Imaging (CT/PET scans, MRIs) 20% coinsurance 40% coinsurance Preauthorization required
Emergency room care $300 copay/visit $300 copay/visit Copay waived if admitted
Urgent care $50 copay/visit 40% coinsurance None"""
CONTRACT_TEXT = (
    "Section 7. Claims and appeals. A member or the member's authorized "
    "representative may file an appeal of an adverse benefit determination "
    "within 180 days of receipt of the notice. The plan administrator shall "
    "review the appeal and notify the member in writing of its decision. "
    "Experimental or investigational services are excluded except as required "
    "by applicable law."
)
FORMULARY_TEXT = """Drug Name Drug Tier Requirements/Limits
atorvastatin calcium tab 10mg, 20mg, 40mg 1 QL (30 per 30 days)
lisinopril tab 2.5mg, 5mg, 10mg 1
metformin hcl er tab 500mg, 750mg 1 QL (120 per 30 days)
sertraline hcl tab 25mg, 50mg, 100mg 1
gabapentin cap 100mg, 300mg, 400mg 2
levothyroxine sodium tab 25mcg, 50mcg 1"""
SPANISH_TEXT = (
    "El Resumen de Beneficios y Cobertura le ayuda a elegir un plan de salud. "
    "Le muestra cómo usted y el plan comparten el costo de los servicios de "
    "atención médica cubiertos. Generalmente, usted debe pagar todos los costos "
    "de los proveedores hasta el monto del deducible antes de que este plan "
    "comience a pagar."
)
RATE_TABLE_TEXT = "Age Premium\n" + "\n".join(
    f"{age} ${300 + age * 3.17:.2f}" for age in range(21, 65)
)
CHINESE_TEXT = "本福利和保障摘要将帮助您选择健康计划。摘要显示您和计划将如何分担承保医疗服务的费用。" * 4


def shifted(text: str, by: int) -> str:
    """Each letter moved ``by`` places along the alphabet, as a font with its
    own encoding can read."""

    def move(ch: str) -> str:
        if "a" <= ch <= "z":
            return chr((ord(ch) - 97 + by) % 26 + 97)
        if "A" <= ch <= "Z":
            return chr((ord(ch) - 65 + by) % 26 + 65)
        return ch

    return "".join(move(ch) for ch in text)


def spaced(text: str) -> str:
    """Every letter on its own, as some text layers read."""
    return " ".join(ch for ch in text if not ch.isspace())


def dots_and_symbols(length: int = 12000) -> str:
    rng = random.Random(7)
    return "".join(rng.choice(".•·¨˛˘˙°…:;,'\"!|") for _ in range(length))


def private_use_characters(length: int = 3000) -> str:
    rng = random.Random(7)
    return "".join(
        chr(rng.randint(0xE000, 0xE0FF)) if rng.random() < 0.8 else " "
        for _ in range(length)
    )

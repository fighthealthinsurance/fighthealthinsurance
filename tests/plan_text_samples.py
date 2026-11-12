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


# A person's own papers, and the plan documents and other text they are
# told apart from (plan_document_kind).
EOB = """Crumpler Mutual Health
Explanation of Benefits
THIS IS NOT A BILL
Member: Rebecca Lee Crumpler
Member ID: W123456789
Claim number: 2026-0451-778
Provider: Lee Family Clinic
Date of service: 03/14/2026
Amount billed $412.00  Plan discount $120.00  Plan paid $232.00
You may owe the provider $60.00
"""
DENIAL_LETTER = """March 20, 2026

Dear Rebecca Lee Crumpler,

Member ID: W123456789
Claim number: 2026-0451-778

We have denied your request for an MRI of the lumbar spine. Your plan
does not cover imaging without prior authorization. You have the right to
appeal this decision within 180 days.
"""
PROVIDER_BILL = """Lee Family Clinic
Statement date: 04/02/2026
Patient name: Rebecca Lee Crumpler
Account number: 55012-77
Date of birth: 02/08/1931
Date of service 03/14/2026  Office visit  $412.00
Insurance payments  -$232.00
Amount due  $60.00
"""
ID_CARD = """Crumpler Mutual Health  Gold PPO
Member name: Rebecca Lee Crumpler
Member ID: W123456789
Group: 1234567  Rx BIN: 610014
Copays: PCP $30  Specialist $60  ER $300
"""
SPANISH_EOB = """Crumpler Mutual Health
Explicación de beneficios
Esto no es una factura
Nombre del paciente: Rebecca Lee Crumpler
Número de miembro: W123456789
Fecha de servicio: 14/03/2026
"""
SPANISH_DENIAL = """Estimada Rebecca Lee Crumpler:

Número de afiliado: W123456789
Fecha de nacimiento: 08/02/1931

Su solicitud ha sido denegada porque el plan no cubre este servicio.
"""
PERSONALISED_SUMMARY = """Summary of Benefits and Coverage: Crumpler Mutual Health Gold PPO
Coverage Period: 01/01/2026 - 12/31/2026
Member name: Rebecca Lee Crumpler
Member ID: W123456789
""" + PLAN_TEXT
SPD = (
    "Summary Plan Description\nCrumpler Manufacturing Employee Health Plan\n\n"
    + CONTRACT_TEXT
    + "\nAfter each claim you receive an Explanation of Benefits. If a claim "
    "has been denied you may appeal. Show your member ID card at each visit.\n"
    + PLAN_TEXT
)
RECIPE = (
    "Grandmother's cornbread. Heat the oven and grease a cast iron skillet "
    "while it warms. Whisk the cornmeal, flour, sugar, baking powder and salt "
    "together in a large bowl. In a second bowl beat the eggs with the milk "
    "and the melted butter until smooth, then fold the wet ingredients into "
    "the dry ones with a few strokes, leaving some lumps. Pour the batter "
    "into the hot skillet, smooth the top, and bake until the edges pull "
    "away from the pan and a toothpick comes out clean. Let it rest for ten "
    "minutes before slicing, and serve it warm with honey or with a bowl of "
    "beans and greens on a cold winter evening when the whole family gathers "
    "around the kitchen table to talk about the week."
)


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

# Seed MedicationContext rows for the most commonly-denied therapeutic
# classes. This complements the schema migration (0164) by populating the
# table on every fresh database — without this, _collect_medication_context()
# would return no matches and the drug-class guidance feature would be
# silently disabled in production.
#
# Maintenance notes:
#   * Use `get_or_create` keyed on `drug_class` so re-running this migration
#     is a no-op and operators can safely tweak rows in admin afterwards.
#   * Add a new RunPython migration when introducing a new class — do not
#     edit this one in place.

from django.db import migrations

GLP1_APPEAL_CONTEXT = (
    "Frame obesity as a chronic, relapsing disease per the AMA (2013) and "
    "the Obesity Medicine Association — not a lifestyle issue. Cite the "
    "patient's documented BMI, weight-related comorbidities, and prior "
    "weight-loss attempts (lifestyle, prior pharmacotherapy, surgery if "
    "applicable). For type 2 diabetes denials, document A1C trajectory and "
    "failure or intolerance of metformin / sulfonylureas / SGLT2s. For "
    "cardiovascular indications, reference the SELECT trial outcome data. "
    'Counter "cosmetic" denials by emphasizing the FDA-approved chronic '
    "weight management indication and the cardiovascular / metabolic harms "
    "of untreated obesity. If step-therapy is invoked, document specific "
    "contraindications, intolerance, or prior failure of each required step. "
    "Note that ACA Section 2713 preventive-services protections apply to "
    "obesity screening and counseling and reinforce coverage parity."
)

GLP1_FDA_INDICATIONS = (
    "Semaglutide (Wegovy) and tirzepatide (Zepbound) are FDA-approved for "
    "chronic weight management in adults with BMI >=30 or BMI >=27 with at "
    "least one weight-related comorbidity (hypertension, dyslipidemia, type 2 "
    "diabetes, obstructive sleep apnea, cardiovascular disease). "
    "Semaglutide (Ozempic, Rybelsus) and tirzepatide (Mounjaro) are "
    "FDA-approved for type 2 diabetes mellitus. Semaglutide 2.4 mg has an "
    "FDA-approved indication to reduce major adverse cardiovascular events "
    "in adults with established cardiovascular disease and either obesity "
    "or overweight (SELECT trial)."
)

GLP1_DENIAL_REASONS = (
    '"Cosmetic" / not for weight loss; failure to document BMI or '
    "comorbidities; step-therapy requirement to try phentermine, orlistat, "
    "or older weight-loss drugs first; quantity limits; non-formulary "
    "status; employer plan exclusion of obesity drugs."
)

CGRP_APPEAL_CONTEXT = (
    "Document the patient's monthly migraine days and headache days from a "
    "headache diary. Per American Headache Society 2024 consensus, "
    "anti-CGRP mAbs are appropriate first-line preventive therapy and step "
    "therapy through older oral preventives is no longer required to "
    "demonstrate medical necessity. List all prior preventives tried with "
    "dates, duration, dose, and reason for discontinuation (lack of "
    "efficacy, intolerance, contraindication). Highlight comorbidities "
    "that contraindicate older preventives (e.g. asthma + propranolol, "
    "weight gain on amitriptyline, kidney stones / cognitive side effects "
    "with topiramate, hepatotoxicity risk with divalproex in women of "
    "childbearing age). For Vyepti site-of-care denials, document the "
    "clinical rationale for IV vs SC administration. Do NOT accept "
    '"experimental" framing — cite the FDA approval date and AHS guideline.'
)

CGRP_FDA_INDICATIONS = (
    "All four anti-CGRP monoclonal antibodies (erenumab, fremanezumab, "
    "galcanezumab, eptinezumab) are FDA-approved for the preventive "
    "treatment of migraine in adults. Galcanezumab is also FDA-approved "
    "for episodic cluster headache."
)

CGRP_DENIAL_REASONS = (
    "Step therapy requiring failure of two or three older preventives "
    "(topiramate, propranolol, amitriptyline, divalproex); quantity limits; "
    "site-of-care restriction for Vyepti infusions; non-formulary status; "
    '"experimental" denials despite long-standing FDA approval.'
)

TNF_APPEAL_CONTEXT = (
    "Document the patient's diagnosis with ICD-10 code and disease "
    "activity (DAS28, BASDAI, SES-CD, Mayo score, PASI, depending on "
    "indication). List prior csDMARD therapy (methotrexate, sulfasalazine, "
    "leflunomide, hydroxychloroquine) with dose, duration, and reason for "
    "discontinuation — or document contraindication (hepatotoxicity, "
    "pregnancy, ILD). Cite ACR / EULAR / AGA / AAD guidelines that support "
    "TNF inhibitor use. For non-medical switching denials, cite "
    "continuity-of-care protections and document any prior loss of "
    "response after a forced biosimilar switch. Push back on biosimilar "
    "mandates with specific clinical reasoning if the originator product "
    "is medically necessary (e.g. IBD patient established on Remicade with "
    "stable trough levels)."
)

TNF_FDA_INDICATIONS = (
    "TNF inhibitors are FDA-approved across rheumatoid arthritis, juvenile "
    "idiopathic arthritis, psoriatic arthritis, ankylosing spondylitis, "
    "Crohn's disease, ulcerative colitis, plaque psoriasis, hidradenitis "
    "suppurativa, and uveitis (specific indications vary by agent)."
)

TNF_DENIAL_REASONS = (
    "Step therapy requiring methotrexate or other csDMARD first; "
    "mandatory switch to a biosimilar; non-medical switching between "
    "brands; quantity limits; site-of-care for infusions; "
    '"not first-line" denials despite contemporary guideline support.'
)

DUPI_APPEAL_CONTEXT = (
    "Document body surface area involvement, EASI / SCORAD / IGA scores, "
    "itch NRS, sleep disruption, and quality-of-life impact (DLQI). For "
    "asthma indications, document blood eosinophil count, FeNO, oral "
    "steroid burden, and exacerbation history. Cite the AAD 2024 atopic "
    "dermatitis guideline and GINA asthma guideline. For step therapy "
    "requiring older systemic immunosuppressants, cite Black-Box-warning "
    "risks (cyclosporine nephrotoxicity, methotrexate hepatotoxicity, "
    "mycophenolate teratogenicity) as basis to skip those steps in "
    "appropriate patients."
)

DUPI_FDA_INDICATIONS = (
    "Dupilumab is FDA-approved for moderate-to-severe atopic dermatitis, "
    "asthma with eosinophilic phenotype or oral corticosteroid dependence, "
    "chronic rhinosinusitis with nasal polyps, eosinophilic esophagitis, "
    "and prurigo nodularis. Tralokinumab is FDA-approved for "
    "moderate-to-severe atopic dermatitis."
)

DUPI_DENIAL_REASONS = (
    "Step therapy requiring topical corticosteroids or topical calcineurin "
    'inhibitors first; quantity limits; "not severe enough" framing; '
    "requirement to fail systemic immunosuppressants (cyclosporine, "
    "methotrexate) before biologic therapy."
)

GAHT_APPEAL_CONTEXT = (
    "Cite the WPATH Standards of Care, version 8 (2022), and the Endocrine "
    "Society's 2017 clinical practice guideline on gender-dysphoric / "
    "gender-incongruent persons. Document the diagnosis (ICD-10 F64.0 in "
    "adolescents/adults), persistent dysphoria, informed consent, and "
    "mental health stability per WPATH SOC 8. Many ACA marketplace plans "
    "and most state-regulated commercial plans cannot lawfully exclude "
    "gender-affirming care under HHS Section 1557 (45 CFR 92). For "
    "employer self-funded plans, cite Bostock v. Clayton County (2020) and "
    "EEOC guidance treating GAHT exclusions as sex discrimination. Counter "
    '"cosmetic" framing by documenting functional impairment, gender '
    "dysphoria's recognized morbidity, and suicide-risk reduction with "
    "treatment access."
)

GAHT_FDA_INDICATIONS = (
    "Gender-affirming hormone therapy is recognized as medically necessary "
    "treatment for gender dysphoria by all major US medical organizations, "
    "including the AMA, AAP, APA, and Endocrine Society. The WPATH "
    "Standards of Care 8 (2022) provide evidence-based clinical guidelines."
)

GAHT_DENIAL_REASONS = (
    'Plan exclusion of gender-affirming care; "cosmetic" framing; '
    "requirement for additional letters beyond WPATH standards; age "
    "restrictions inconsistent with current guidelines; denial of "
    "adolescent care despite Endocrine Society support."
)


SEED_ROWS = [
    {
        "drug_class": "GLP-1 receptor agonist (obesity / type 2 diabetes)",
        "brand_names": (
            "Ozempic\nWegovy\nMounjaro\nZepbound\nRybelsus\n"
            "Saxenda\nVictoza\nTrulicity"
        ),
        "generic_names": ("semaglutide\ntirzepatide\nliraglutide\ndulaglutide"),
        "regex": (
            r"(ozempic|wegovy|mounjaro|zepbound|rybelsus|saxenda|victoza|"
            r"trulicity|semaglutide|tirzepatide|liraglutide|dulaglutide)"
        ),
        "fda_indications": GLP1_FDA_INDICATIONS,
        "common_denial_reasons": GLP1_DENIAL_REASONS,
        "appeal_context": GLP1_APPEAL_CONTEXT,
        "pubmed_ids": "37952131\n33567185\n33888385",
        "active": True,
    },
    {
        "drug_class": "Anti-CGRP monoclonal antibody (migraine prevention)",
        "brand_names": "Aimovig\nAjovy\nEmgality\nVyepti",
        "generic_names": ("erenumab\nfremanezumab\ngalcanezumab\neptinezumab"),
        "regex": (
            r"(aimovig|ajovy|emgality|vyepti|erenumab|fremanezumab|"
            r"galcanezumab|eptinezumab)"
        ),
        "fda_indications": CGRP_FDA_INDICATIONS,
        "common_denial_reasons": CGRP_DENIAL_REASONS,
        "appeal_context": CGRP_APPEAL_CONTEXT,
        "pubmed_ids": "38716826\n30586866\n32165007",
        "active": True,
    },
    {
        "drug_class": "TNF inhibitor (rheumatology / IBD / dermatology)",
        "brand_names": (
            "Humira\nRemicade\nEnbrel\nSimponi\nSimponi Aria\nCimzia\n"
            "Inflectra\nRenflexis\nAvsola\nCyltezo\nHadlima\nHyrimoz\nYusimry"
        ),
        "generic_names": (
            "adalimumab\ninfliximab\netanercept\ngolimumab\ncertolizumab pegol"
        ),
        "regex": (
            r"(humira|remicade|enbrel|simponi|cimzia|inflectra|renflexis|"
            r"avsola|cyltezo|hadlima|hyrimoz|yusimry|adalimumab|infliximab|"
            r"etanercept|golimumab|certolizumab)"
        ),
        "fda_indications": TNF_FDA_INDICATIONS,
        "common_denial_reasons": TNF_DENIAL_REASONS,
        "appeal_context": TNF_APPEAL_CONTEXT,
        "pubmed_ids": "33841974\n30192054\n31278846",
        "active": True,
    },
    {
        "drug_class": "IL-4/IL-13 inhibitor (atopic disease)",
        "brand_names": "Dupixent\nAdbry",
        "generic_names": "dupilumab\ntralokinumab",
        "regex": r"(dupixent|adbry|dupilumab|tralokinumab)",
        "fda_indications": DUPI_FDA_INDICATIONS,
        "common_denial_reasons": DUPI_DENIAL_REASONS,
        "appeal_context": DUPI_APPEAL_CONTEXT,
        "pubmed_ids": "37182567\n30449804",
        "active": True,
    },
    {
        "drug_class": "Gender-affirming hormone therapy",
        "brand_names": (
            "Depo-Testosterone\nTestopel\nAndrogel\nEstradiol patch\n"
            "Climara\nVivelle-Dot\nEstrogel\nProvera"
        ),
        "generic_names": (
            "testosterone cypionate\ntestosterone enanthate\nestradiol\n"
            "estradiol valerate\nspironolactone\nfinasteride\nprogesterone"
        ),
        "regex": (
            r"(gender[\s-]?affirming|gender[\s-]?dysphoria|transgender|"
            r"cross[\s-]?sex hormone|"
            r"testosterone (cypionate|enanthate|valerate)|"
            r"estradiol (patch|valerate|cypionate)|"
            r"spironolactone|cyproterone|leuprolide|histrelin)"
        ),
        "fda_indications": GAHT_FDA_INDICATIONS,
        "common_denial_reasons": GAHT_DENIAL_REASONS,
        "appeal_context": GAHT_APPEAL_CONTEXT,
        "pubmed_ids": "36238954\n28945902",
        "active": True,
    },
]


def seed_medication_contexts(apps, schema_editor):
    MedicationContext = apps.get_model("fighthealthinsurance", "MedicationContext")
    for row in SEED_ROWS:
        MedicationContext.objects.get_or_create(
            drug_class=row["drug_class"], defaults=row
        )


def remove_medication_contexts(apps, schema_editor):
    MedicationContext = apps.get_model("fighthealthinsurance", "MedicationContext")
    MedicationContext.objects.filter(
        drug_class__in=[row["drug_class"] for row in SEED_ROWS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0165_medicationcontext"),
    ]

    operations = [
        migrations.RunPython(seed_medication_contexts, remove_medication_contexts),
    ]

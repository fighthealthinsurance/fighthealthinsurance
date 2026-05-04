"""
Seed an initial set of UnitedHealthcare prior-authorization requirement entries.

These rows are a representative starter set sourced from public UHC prior
authorization / advance notification requirement lists. They are intentionally
conservative (a handful of well-known PA-required code categories) and are
meant as a foundation that operations staff can expand. Each row includes a
source pointer so it can be audited and refreshed when UHC publishes updates.

The starter set is sourced from UHC's Commercial *and* Medicare Advantage PA
lists, so each entry is materialized as two rows — one per LOB. Medicaid /
exchange / DSNP rules are intentionally not seeded here because they were
not in the source documents.

The migration is idempotent: it uses get_or_create keyed on
(insurance_company, line_of_business, cpt_hcpcs_code, code_range_start,
code_range_end) so re-running it (or running it after operators add their own
rows) does not produce duplicates.
"""

import datetime
from typing import Any, Dict, List

from django.db import migrations

# Lines of business this starter set was actually sourced for. Each entry
# in UHC_PA_SEED_ENTRIES is materialized once per LOB so we never present a
# rule for a product (e.g. Medicaid, exchange, DSNP) it wasn't sourced from.
SEED_LINES_OF_BUSINESS = ("commercial", "medicare_advantage")

# Seed entries covering common UHC PA categories.
#
# The shape mirrors UHC's PA requirement PDFs: each entry pins a specific
# code (or range), the coverage criteria document (UHC Medical Policy or
# Coverage Determination Guideline), and the submission channel. Categories
# chosen here are ones our denial corpus shows most frequently for UHC PA
# appeals.
UHC_PA_SEED_ENTRIES: List[Dict[str, Any]] = [
    # --- Genetic and molecular testing (UHC requires PA for most BRCA panels) ---
    {
        "cpt_hcpcs_code": "81162",
        "code_description": "BRCA1, BRCA2 (hereditary breast/ovarian) full sequence + duplication/deletion analysis",
        "pa_category": "Genetic and Molecular Testing",
        "criteria_reference": "UHC Medical Policy: Molecular Oncology Testing for Cancer Diagnosis, Prognosis, and Treatment Decisions",
    },
    {
        "cpt_hcpcs_code": "81163",
        "code_description": "BRCA1, BRCA2 full sequence analysis",
        "pa_category": "Genetic and Molecular Testing",
        "criteria_reference": "UHC Medical Policy: Molecular Oncology Testing for Cancer Diagnosis, Prognosis, and Treatment Decisions",
    },
    {
        "cpt_hcpcs_code": "81479",
        "code_description": "Unlisted molecular pathology procedure",
        "pa_category": "Genetic and Molecular Testing",
        "criteria_reference": "UHC Medical Policy: Molecular Pathology / Molecular Diagnostics",
    },
    # --- Sleep medicine ---
    {
        "cpt_hcpcs_code": "95810",
        "code_description": "Polysomnography; sleep staging with 4+ additional parameters of sleep, attended",
        "pa_category": "Sleep Medicine",
        "criteria_reference": "UHC Medical Policy: Attended Polysomnography for Evaluation of Sleep Disorders",
    },
    {
        "cpt_hcpcs_code": "95811",
        "code_description": "Polysomnography with initiation of CPAP/BiPAP",
        "pa_category": "Sleep Medicine",
        "criteria_reference": "UHC Medical Policy: Attended Polysomnography for Evaluation of Sleep Disorders",
    },
    # --- Continuous glucose monitors / DME ---
    {
        "cpt_hcpcs_code": "K0553",
        "code_description": "Supply allowance for therapeutic continuous glucose monitor (CGM)",
        "pa_category": "Durable Medical Equipment - CGM",
        "criteria_reference": "UHC Medical Policy: Continuous Glucose Monitors",
    },
    {
        "cpt_hcpcs_code": "K0554",
        "code_description": "Receiver (monitor), dedicated, for use with therapeutic CGM",
        "pa_category": "Durable Medical Equipment - CGM",
        "criteria_reference": "UHC Medical Policy: Continuous Glucose Monitors",
    },
    # --- Specialty injectable / infused drugs (J-codes) ---
    {
        "cpt_hcpcs_code": "J0490",
        "code_description": "Injection, belimumab (Benlysta), 10 mg",
        "pa_category": "Outpatient Injectable Medication",
        "criteria_reference": "UHC Medical Benefit Drug Policy: Belimumab",
    },
    {
        "cpt_hcpcs_code": "J9035",
        "code_description": "Injection, bevacizumab (Avastin), 10 mg",
        "pa_category": "Outpatient Injectable Medication",
        "criteria_reference": "UHC Medical Benefit Drug Policy: Bevacizumab and Biosimilars",
    },
    # --- Advanced imaging (radiology benefits manager territory) ---
    {
        "cpt_hcpcs_code": "70551",
        "code_description": "MRI, brain (including brain stem); without contrast",
        "pa_category": "Advanced Outpatient Imaging",
        "criteria_reference": "UHC Radiology / RBM (managed by Optum) - Advanced Outpatient Imaging guidelines",
    },
    {
        "cpt_hcpcs_code": "74177",
        "code_description": "CT abdomen and pelvis with contrast",
        "pa_category": "Advanced Outpatient Imaging",
        "criteria_reference": "UHC Radiology / RBM (managed by Optum) - Advanced Outpatient Imaging guidelines",
    },
    # --- Spine surgery (range) ---
    {
        "cpt_hcpcs_code": "",
        "code_range_start": "22510",
        "code_range_end": "22515",
        "code_description": "Percutaneous vertebroplasty / vertebral augmentation",
        "pa_category": "Spine Surgery",
        "criteria_reference": "UHC Medical Policy: Vertebroplasty, Kyphoplasty, and Sacroplasty",
    },
    # --- Office E&M visits explicitly NOT requiring PA (negative entry) ---
    {
        "cpt_hcpcs_code": "99213",
        "code_description": "Office or other outpatient visit, established patient (low complexity)",
        "pa_category": "Evaluation and Management",
        "criteria_reference": "UHC standard E&M coverage",
        "requires_pa": False,
        "notes": "Standard office visit codes are not subject to PA on UHC commercial / MA plans.",
    },
    {
        "cpt_hcpcs_code": "99214",
        "code_description": "Office or other outpatient visit, established patient (moderate complexity)",
        "pa_category": "Evaluation and Management",
        "criteria_reference": "UHC standard E&M coverage",
        "requires_pa": False,
        "notes": "Standard office visit codes are not subject to PA on UHC commercial / MA plans.",
    },
]


SOURCE_DOCUMENT = (
    "UnitedHealthcare Prior Authorization Requirements (commercial and Medicare Advantage); "
    "starter set seeded by FHI"
)
SOURCE_DOCUMENT_DATE = datetime.date(2025, 1, 1)
SUBMISSION_CHANNEL_DEFAULT = (
    "UHCprovider.com Prior Authorization and Notification tool, "
    "or 866-889-8054 (UHC commercial); 800-711-4555 (UHC OptumRx for pharmacy J-code review)."
)

# Exact aliases for the canonical UnitedHealthcare row, in priority order.
# We deliberately avoid icontains/substring fallbacks so the migration can
# never silently seed a UHC-affiliated subsidiary (e.g. "UnitedHealthcare
# Community Plan", which targets Medicaid).
_UHC_NAME_ALIASES = (
    "UnitedHealthcare",
    "United Healthcare",
    "United Health Care",
    "UnitedHealth",
)


def _get_uhc(InsuranceCompany):
    """Return the canonical UnitedHealthcare ``InsuranceCompany`` row.

    Tries each known alias as an exact (case-insensitive) match. Returns
    ``None`` if no match is found so the migration can skip cleanly on a
    fresh test DB. Raises if multiple aliases match different rows — that
    indicates a data-integrity issue we don't want to paper over.
    """
    matched = None
    for alias in _UHC_NAME_ALIASES:
        row = InsuranceCompany.objects.filter(name__iexact=alias).first()
        if row is None:
            continue
        if matched is not None and row.pk != matched.pk:
            raise RuntimeError(
                f"Ambiguous UnitedHealthcare seed target: aliases match both "
                f"id={matched.pk!r} ({matched.name!r}) and "
                f"id={row.pk!r} ({row.name!r}). Resolve the duplicate "
                "InsuranceCompany rows before re-running this migration."
            )
        matched = row
    return matched


def seed_uhc_pa_requirements(apps, schema_editor):
    InsuranceCompany = apps.get_model("fighthealthinsurance", "InsuranceCompany")
    PayerPriorAuthRequirement = apps.get_model(
        "fighthealthinsurance", "PayerPriorAuthRequirement"
    )

    uhc = _get_uhc(InsuranceCompany)
    if uhc is None:
        # The insurance_companies fixture loads UHC, but it may not be applied
        # in fresh test DBs. Skip silently — operators can re-run loaddata
        # followed by `migrate --run-syncdb` to populate.
        return

    for entry in UHC_PA_SEED_ENTRIES:
        defaults = {
            "code_description": entry.get("code_description", ""),
            "pa_category": entry.get("pa_category", ""),
            "requires_pa": entry.get("requires_pa", True),
            "notification_only": entry.get("notification_only", False),
            "submission_channel": entry.get(
                "submission_channel", SUBMISSION_CHANNEL_DEFAULT
            ),
            "criteria_reference": entry.get("criteria_reference", ""),
            "criteria_url": entry.get("criteria_url", ""),
            "source_document": SOURCE_DOCUMENT,
            "source_document_date": SOURCE_DOCUMENT_DATE,
            "notes": entry.get("notes", ""),
        }
        # Materialize one row per LOB the source list actually covered, so
        # Medicaid / exchange / DSNP lookups don't pick these up.
        explicit_lob = entry.get("line_of_business")
        lobs = (explicit_lob,) if explicit_lob else SEED_LINES_OF_BUSINESS
        for lob in lobs:
            PayerPriorAuthRequirement.objects.get_or_create(
                insurance_company=uhc,
                line_of_business=lob,
                state=entry.get("state", ""),
                cpt_hcpcs_code=entry.get("cpt_hcpcs_code", ""),
                code_range_start=entry.get("code_range_start", ""),
                code_range_end=entry.get("code_range_end", ""),
                defaults=defaults,
            )


def remove_uhc_pa_requirements(apps, schema_editor):
    InsuranceCompany = apps.get_model("fighthealthinsurance", "InsuranceCompany")
    PayerPriorAuthRequirement = apps.get_model(
        "fighthealthinsurance", "PayerPriorAuthRequirement"
    )

    uhc = _get_uhc(InsuranceCompany)
    if uhc is None:
        return
    PayerPriorAuthRequirement.objects.filter(
        insurance_company=uhc,
        source_document=SOURCE_DOCUMENT,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0164_payerpriorauthrequirement"),
    ]

    operations = [
        migrations.RunPython(seed_uhc_pa_requirements, remove_uhc_pa_requirements),
    ]

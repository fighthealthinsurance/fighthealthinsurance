"""Add PA requirement list URL fields to ``InsuranceCompany``.

Separates the per-payer PA requirement document (a list of CPT/HCPCS codes
that require prior authorization, often a downloadable Excel/PDF/HTML
table) from the medical-policy index already on the model. The
``ingest_pa_requirements`` command + refresh actor use these fields to
discover which payers have parseable PA lists and which URLs to fetch.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0179_denial_use_external_default_true"),
    ]

    operations = [
        migrations.AddField(
            model_name="insurancecompany",
            name="pa_requirement_list_url",
            field=models.URLField(
                blank=True,
                help_text=(
                    "URL to the payer's published prior-authorization requirement list "
                    "(e.g., a downloadable Excel/PDF/HTML table of CPT/HCPCS codes that "
                    "require PA). Leave blank when no public list is available."
                ),
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="pa_requirement_list_url_is_parseable",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when a registered parser exists for this URL's host/format and "
                    "the document can be auto-fetched and ingested into "
                    "PayerPriorAuthRequirement rows. Set False for search portals or "
                    "documents that require interactive navigation."
                ),
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="pa_requirement_list_notes",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Human-readable notes about the PA requirement list: what lines of "
                    "business it covers, how often it is updated, known parsing quirks, etc."
                ),
            ),
        ),
    ]

# Hand-written migration for payer policy fields on InsuranceCompany.
# Format matches Django 5.2 auto-generated output.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0164_ucr_models_and_denial_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="insurancecompany",
            name="is_major_commercial_payer",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True if this is a major national commercial carrier whose "
                    "public medical policies should be prioritized as comparative "
                    "industry evidence in appeals (Aetna, UnitedHealthcare, Cigna, "
                    "Anthem, Humana, Kaiser, etc.). Used to order the comparative-"
                    "payer section so it surfaces nationally recognized carriers "
                    "ahead of regional BCBS affiliates."
                ),
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="medical_policy_url",
            field=models.URLField(
                blank=True,
                help_text="URL to the company's public medical/coverage/clinical-policy index",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="medical_policy_name",
            field=models.CharField(
                max_length=200,
                blank=True,
                help_text=(
                    "Name the company uses for its medical-policy documents "
                    "(e.g., 'Clinical Policy Bulletins', 'Coverage Policy', "
                    "'Medical Policy', 'Clinical UM Guideline')"
                ),
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="medical_policy_url_is_static_index",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True if medical_policy_url points to a static, directly-browsable "
                    "index (an HTML page or PDF that lists policies and links to "
                    "individual policy documents without requiring search or state "
                    "selection). False (the default) means the URL is a search "
                    "portal, requires state/region selection, or otherwise needs "
                    "interactive navigation. Used to distinguish links that could be "
                    "pre-fetched and parsed from links that are pointers only."
                ),
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="medical_policy_notes",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Short description of how this payer's medical policies are used / "
                    "what they assert (e.g., medical-necessity criteria, "
                    "experimental/investigational determinations)."
                ),
            ),
        ),
    ]

# Generated manually for insurance company and plan models

import re
from django.db import migrations, models
import django.db.models.deletion
import regex_field.fields


class Migration(migrations.Migration):

    dependencies = [
        (
            "fighthealthinsurance",
            "0143_denial_asn_denial_asn_name_denial_ip_address_and_more",
        ),
    ]

    operations = [
        # Create InsuranceCompany model
        migrations.CreateModel(
            name="InsuranceCompany",
            fields=[
                (
                    "id",
                    models.AutoField(primary_key=True, serialize=False),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="Official name of the insurance company (e.g., Anthem, Blue Cross Blue Shield)",
                        max_length=300,
                        unique=True,
                    ),
                ),
                (
                    "alt_names",
                    models.TextField(
                        blank=True,
                        help_text="Alternative names or abbreviations, one per line (e.g., BCBS for Blue Cross Blue Shield)",
                    ),
                ),
                (
                    "regex",
                    regex_field.fields.RegexField(
                        blank=True,
                        help_text="Regular expression pattern to match this company in denial text",
                        max_length=800,
                        re_flags=re.IGNORECASE | re.UNICODE | re.MULTILINE,
                    ),
                ),
                (
                    "negative_regex",
                    regex_field.fields.RegexField(
                        blank=True,
                        help_text="Pattern to exclude false matches",
                        max_length=800,
                        re_flags=re.IGNORECASE | re.UNICODE | re.MULTILINE,
                    ),
                ),
                (
                    "website",
                    models.URLField(blank=True, help_text="Company's official website"),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        help_text="Additional notes about this insurance company",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Insurance Companies",
                "ordering": ["name"],
            },
        ),
        # Create InsurancePlan model
        migrations.CreateModel(
            name="InsurancePlan",
            fields=[
                (
                    "id",
                    models.AutoField(primary_key=True, serialize=False),
                ),
                (
                    "plan_name",
                    models.CharField(
                        help_text="Specific plan name (e.g., Medicaid California, Gold PPO)",
                        max_length=300,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        blank=True,
                        help_text="Two-letter state code if plan is state-specific",
                        max_length=2,
                    ),
                ),
                (
                    "regex",
                    regex_field.fields.RegexField(
                        blank=True,
                        help_text="Regular expression pattern to match this specific plan",
                        max_length=800,
                        re_flags=re.IGNORECASE | re.UNICODE | re.MULTILINE,
                    ),
                ),
                (
                    "negative_regex",
                    regex_field.fields.RegexField(
                        blank=True,
                        help_text="Pattern to exclude false matches",
                        max_length=800,
                        re_flags=re.IGNORECASE | re.UNICODE | re.MULTILINE,
                    ),
                ),
                (
                    "plan_id_prefix",
                    models.CharField(
                        blank=True,
                        help_text="Common prefix for plan IDs (helps with identification)",
                        max_length=50,
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True, help_text="Additional notes about this plan"
                    ),
                ),
                (
                    "insurance_company",
                    models.ForeignKey(
                        help_text="The insurance company that offers this plan",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="plans",
                        to="fighthealthinsurance.insurancecompany",
                    ),
                ),
                (
                    "plan_type",
                    models.ForeignKey(
                        blank=True,
                        help_text="Type of plan (HMO, PPO, etc.)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="fighthealthinsurance.plantype",
                    ),
                ),
                (
                    "plan_source",
                    models.ForeignKey(
                        blank=True,
                        help_text="Source of plan (Medicaid, Medicare, Employer, etc.)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="fighthealthinsurance.plansource",
                    ),
                ),
            ],
            options={
                "verbose_name": "Insurance Plan",
                "ordering": ["insurance_company__name", "plan_name"],
            },
        ),
        # Add unique constraint to InsurancePlan
        migrations.AlterUniqueTogether(
            name="insuranceplan",
            unique_together={("insurance_company", "plan_name", "state")},
        ),
        # Add fields to Denial model
        migrations.AddField(
            model_name="denial",
            name="insurance_company_obj",
            field=models.ForeignKey(
                blank=True,
                help_text="Structured reference to insurance company",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="denials",
                to="fighthealthinsurance.insurancecompany",
            ),
        ),
        migrations.AddField(
            model_name="denial",
            name="insurance_plan_obj",
            field=models.ForeignKey(
                blank=True,
                help_text="Specific insurance plan (e.g., Anthem Medicaid California)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="denials",
                to="fighthealthinsurance.insuranceplan",
            ),
        ),
    ]

# Hand-written migration for PayerPolicyEntry model.
# Format matches Django 5.2 auto-generated output.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0165_insurancecompany_payer_policy_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PayerPolicyEntry",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "insurance_company",
                    models.ForeignKey(
                        help_text="The payer that publishes this policy",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="policy_entries",
                        to="fighthealthinsurance.insurancecompany",
                    ),
                ),
                (
                    "title",
                    models.CharField(
                        help_text="Policy title as listed in the payer's index (e.g., 'Acupuncture')",
                        max_length=500,
                    ),
                ),
                (
                    "url",
                    models.URLField(
                        help_text="Direct URL to the individual policy document (PDF or HTML)",
                        max_length=2000,
                    ),
                ),
                (
                    "payer_policy_id",
                    models.CharField(
                        blank=True,
                        help_text="Payer-internal policy id (e.g. 'mm_0540', '0001'), if extractable",
                        max_length=100,
                    ),
                ),
                ("last_indexed", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Payer Policy Entry",
                "verbose_name_plural": "Payer Policy Entries",
                "unique_together": {("insurance_company", "url")},
                "indexes": [
                    models.Index(
                        fields=["insurance_company", "title"],
                        name="fighthlth_payerp_ic_title_idx",
                    ),
                ],
            },
        ),
    ]

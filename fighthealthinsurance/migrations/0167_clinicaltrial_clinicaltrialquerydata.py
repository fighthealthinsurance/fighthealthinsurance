# Generated for ClinicalTrials.gov API integration

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0166_insurancecompany_is_major_commercial_payer_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClinicalTrial",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "nct_id",
                    models.CharField(max_length=20, unique=True),
                ),
                ("brief_title", models.TextField(blank=True)),
                ("official_title", models.TextField(blank=True)),
                ("overall_status", models.CharField(blank=True, max_length=50)),
                ("phases", models.TextField(blank=True)),
                ("study_type", models.CharField(blank=True, max_length=50)),
                ("conditions", models.TextField(blank=True)),
                ("interventions", models.TextField(blank=True)),
                ("brief_summary", models.TextField(blank=True)),
                ("start_date", models.CharField(blank=True, max_length=20)),
                ("completion_date", models.CharField(blank=True, max_length=20)),
                ("last_update_date", models.CharField(blank=True, max_length=20)),
                ("has_results", models.BooleanField(default=False)),
                (
                    "created",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        null=True,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ClinicalTrialQueryData",
            fields=[
                ("internal_id", models.AutoField(primary_key=True, serialize=False)),
                ("query", models.TextField(max_length=300)),
                ("condition", models.TextField(blank=True, null=True)),
                ("intervention", models.TextField(blank=True, null=True)),
                ("nct_ids", models.TextField(null=True)),
                (
                    "created",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        null=True,
                    ),
                ),
                (
                    "denial_id",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["query", "condition", "intervention", "created"],
                        name="cttq_cache_lookup_idx",
                    ),
                    models.Index(
                        fields=["denial_id", "created"], name="cttq_denial_idx"
                    ),
                ],
            },
        ),
    ]

# Generated for IMRDecision model

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0160_niceguidance_denial_nice_context_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="IMRDecision",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("ca_dmhc", "California DMHC"),
                            ("ny_dfs", "New York DFS"),
                        ],
                        max_length=20,
                    ),
                ),
                ("case_id", models.CharField(max_length=100)),
                ("state", models.CharField(max_length=2)),
                ("decision_year", models.IntegerField(blank=True, null=True)),
                ("decision_date", models.DateField(blank=True, null=True)),
                ("diagnosis", models.CharField(blank=True, default="", max_length=500)),
                (
                    "diagnosis_category",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("treatment", models.CharField(blank=True, default="", max_length=500)),
                (
                    "treatment_category",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                (
                    "treatment_subcategory",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                (
                    "determination",
                    models.CharField(
                        choices=[
                            ("overturned", "Overturned"),
                            ("upheld", "Upheld"),
                            ("overturned_in_part", "Overturned in part"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=30,
                    ),
                ),
                (
                    "decision_type",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                (
                    "insurance_type",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                ("age_range", models.CharField(blank=True, default="", max_length=50)),
                ("gender", models.CharField(blank=True, default="", max_length=30)),
                ("findings", models.TextField(blank=True, default="")),
                ("summary", models.TextField(blank=True, default="")),
                ("source_url", models.URLField(blank=True, default="", max_length=500)),
                ("raw_data", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["diagnosis", "treatment"],
                        name="imrdec_diag_treat_idx",
                    ),
                    models.Index(
                        fields=["treatment_category", "determination"],
                        name="imrdec_tcat_det_idx",
                    ),
                    models.Index(
                        fields=["state", "determination"],
                        name="imrdec_state_det_idx",
                    ),
                    models.Index(
                        fields=["source", "determination"],
                        name="imrdec_source_det_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["source", "case_id"],
                        name="imrdecision_unique_source_case",
                    ),
                ],
            },
        ),
    ]

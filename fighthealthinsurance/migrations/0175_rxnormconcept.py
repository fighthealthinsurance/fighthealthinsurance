# Generated migration for RxNorm concept cache.

from django.db import migrations, models
from django.db.models.functions import Now


class Migration(migrations.Migration):
    # This migration originally shipped as 0173_rxnormconcept (depending on
    # 0172) and was renamed to 0175 when 0173_medicationcontext landed (#787).
    # Databases that migrated before the rename have it recorded under the old
    # name; ``replaces`` makes Django treat those as already applied instead
    # of re-creating the table. The dependency stays 0172 (its original) so
    # such databases don't trip the applied-before-dependency consistency
    # check; 0176 depends on both this and 0174 to keep the graph linear.
    replaces = [("fighthealthinsurance", "0173_rxnormconcept")]

    dependencies = [
        ("fighthealthinsurance", "0172_payerpriorauthrequirement"),
    ]

    operations = [
        migrations.CreateModel(
            name="RxNormConcept",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("query", models.CharField(max_length=300, unique=True)),
                ("rxcui", models.CharField(blank=True, default="", max_length=32)),
                (
                    "canonical_name",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("tty", models.CharField(blank=True, default="", max_length=16)),
                ("score", models.IntegerField(blank=True, null=True)),
                ("related_json", models.JSONField(blank=True, default=dict)),
                (
                    "created",
                    models.DateTimeField(db_default=Now(), null=True),
                ),
                ("last_used", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["rxcui"], name="rxnorm_rxcui_idx"),
                ],
            },
        ),
    ]

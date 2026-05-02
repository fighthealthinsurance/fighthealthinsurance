# Generated migration for RxNorm concept cache.

from django.db import migrations, models
from django.db.models.functions import Now


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0164_ucr_models_and_denial_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="RxNormConcept",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("query", models.CharField(db_index=True, max_length=300)),
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
                    models.Index(fields=["query"], name="rxnorm_query_idx"),
                    models.Index(fields=["rxcui"], name="rxnorm_rxcui_idx"),
                ],
            },
        ),
    ]

# Generated for ECRI Guidelines Trust integration.

import django_prometheus.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0159_chatdocument"),
    ]

    operations = [
        migrations.CreateModel(
            name="ECRIGuideline",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "guideline_id",
                    models.CharField(db_index=True, max_length=128, unique=True),
                ),
                (
                    "source",
                    models.CharField(default="ECRI Guidelines Trust", max_length=64),
                ),
                ("title", models.CharField(max_length=500)),
                (
                    "developer_organization",
                    models.CharField(blank=True, max_length=300),
                ),
                ("publication_date", models.DateField(blank=True, null=True)),
                ("last_updated", models.DateField(blank=True, null=True)),
                ("recommendations_summary", models.TextField(blank=True)),
                ("intended_population", models.TextField(blank=True)),
                ("intended_users", models.CharField(blank=True, max_length=300)),
                ("evidence_quality", models.CharField(blank=True, max_length=100)),
                ("procedure_keywords", models.JSONField(blank=True, default=list)),
                ("diagnosis_keywords", models.JSONField(blank=True, default=list)),
                ("topics", models.JSONField(blank=True, default=list)),
                ("url", models.URLField(blank=True, max_length=2000)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "ECRI Clinical Guideline",
                "verbose_name_plural": "ECRI Clinical Guidelines",
                "ordering": ["-publication_date", "title"],
            },
            bases=(
                django_prometheus.models.ExportModelOperationsMixin("ECRIGuideline"),
                models.Model,
            ),
        ),
        migrations.AddIndex(
            model_name="ecriguideline",
            index=models.Index(
                fields=["source", "is_active"],
                name="fighthealth_source_2c0b85_idx",
            ),
        ),
    ]

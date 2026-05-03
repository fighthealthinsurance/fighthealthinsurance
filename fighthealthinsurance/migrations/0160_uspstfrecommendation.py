# Hand-written migration for USPSTFRecommendation model

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0159_chatdocument"),
    ]

    operations = [
        migrations.CreateModel(
            name="USPSTFRecommendation",
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
                ("uspstf_id", models.CharField(max_length=128, unique=True)),
                ("title", models.TextField()),
                ("grade", models.CharField(blank=True, default="", max_length=4)),
                (
                    "status",
                    models.CharField(blank=True, default="current", max_length=32),
                ),
                ("topic", models.TextField(blank=True, default="")),
                ("population", models.TextField(blank=True, default="")),
                ("short_description", models.TextField(blank=True, default="")),
                ("rationale", models.TextField(blank=True, default="")),
                ("clinical_considerations", models.TextField(blank=True, default="")),
                ("url", models.TextField(blank=True, default="")),
                (
                    "date_issued",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("raw_data", models.JSONField(blank=True, null=True)),
                ("last_synced", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["grade"], name="fighthealth_grade_idx"),
                    models.Index(fields=["topic"], name="fighthealth_topic_idx"),
                ],
            },
        ),
    ]

# Generated for CMS Medicare Coverage Database integration

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0159_chatdocument"),
    ]

    operations = [
        migrations.CreateModel(
            name="CMSCoverageCache",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("procedure", models.CharField(max_length=300)),
                ("diagnosis", models.CharField(max_length=300)),
                ("generic_citations", models.JSONField(default=list)),
                ("medicare_citations", models.JSONField(default=list)),
                (
                    "generic_updated_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "medicare_updated_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["procedure", "diagnosis"],
                        name="cms_cov_proc_diag_idx",
                    ),
                ],
            },
        ),
    ]

# Cross-pod record of the last outcome of calls to an external service, one
# row per service (models.ExternalServiceHealth). Written by the TypeSafe
# scoring call site, read by the staff status page. Unique on service; the
# other columns are read one row at a time, so no further index.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0205_proposedappeal_quality_score"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalServiceHealth",
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
                ("service", models.CharField(max_length=64, unique=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("last_failure_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_failure",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]

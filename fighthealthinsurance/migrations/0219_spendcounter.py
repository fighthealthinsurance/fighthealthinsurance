# Per-day spend counters for paid model providers (ml/spend.py): one row per
# UTC day and "<provider>:<use>" name, amounts in micro-dollars. Numbers only.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0213_proposedappeal_professional_pick"),
    ]

    operations = [
        migrations.CreateModel(
            name="SpendCounter",
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
                ("day", models.DateField()),
                ("name", models.CharField(max_length=80)),
                ("amount", models.BigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("day", "name"), name="spend_counter_day_name"
                    )
                ],
            },
        ),
    ]

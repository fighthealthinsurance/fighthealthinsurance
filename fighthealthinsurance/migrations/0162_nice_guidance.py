# Generated for NICE syndication API integration

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0161_loststripesession_secure_token"),
    ]

    operations = [
        migrations.CreateModel(
            name="NICEGuidance",
            fields=[
                (
                    "internal_id",
                    models.AutoField(primary_key=True, serialize=False),
                ),
                (
                    "guidance_id",
                    models.CharField(db_index=True, max_length=50, unique=True),
                ),
                ("title", models.TextField(blank=True)),
                ("url", models.TextField(blank=True)),
                ("guidance_type", models.CharField(blank=True, max_length=200)),
                ("summary", models.TextField(blank=True)),
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
            name="NICEQueryData",
            fields=[
                (
                    "internal_id",
                    models.AutoField(primary_key=True, serialize=False),
                ),
                ("query", models.TextField()),
                ("results", models.TextField(null=True)),
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
        ),
        migrations.AddField(
            model_name="denial",
            name="nice_context",
            field=models.TextField(blank=True, null=True),
        ),
    ]

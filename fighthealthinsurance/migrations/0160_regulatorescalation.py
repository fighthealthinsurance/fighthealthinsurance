# Generated for the escalation packet feature.

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0159_chatdocument"),
    ]

    operations = [
        migrations.CreateModel(
            name="RegulatorEscalation",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.CharField(
                        default=uuid.uuid4,
                        editable=False,
                        max_length=100,
                        unique=True,
                    ),
                ),
                ("hashed_email", models.CharField(max_length=300)),
                (
                    "recipient_type",
                    models.CharField(
                        choices=[
                            ("doi", "State DOI / Insurance Commissioner"),
                            ("medical_director", "Plan Medical Director"),
                            ("dol_ebsa", "DOL EBSA (ERISA)"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "recipient_name",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("recipient_address", models.TextField(blank=True, default="")),
                (
                    "recipient_phone",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                (
                    "recipient_url",
                    models.CharField(blank=True, default="", max_length=400),
                ),
                ("letter_text", models.TextField(blank=True, default="")),
                ("chosen", models.BooleanField(default=False)),
                ("editted", models.BooleanField(default=False)),
                ("sent", models.BooleanField(default=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("mod_date", models.DateTimeField(auto_now=True)),
                (
                    "for_denial",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="regulator_escalations",
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["for_denial"], name="reg_escal_denial_idx"),
                    models.Index(fields=["hashed_email"], name="reg_escal_email_idx"),
                ],
            },
        ),
    ]

import uuid

import django.db.models.deletion
from django.db import migrations, models

import django_prometheus.models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0187_ip_address_charfields"),
    ]

    operations = [
        migrations.CreateModel(
            name="GenericCallScript",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("insurer_name", models.CharField(max_length=300)),
                ("denial_reason", models.CharField(max_length=600)),
                (
                    "goal",
                    models.CharField(
                        choices=[
                            ("info_gathering", "Information Gathering"),
                            ("escalation", "Escalation"),
                        ],
                        max_length=32,
                    ),
                ),
                ("prompt_version", models.CharField(default="v1", max_length=32)),
                ("script_text", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "insurer_name",
                            "denial_reason",
                            "goal",
                            "prompt_version",
                        ),
                        name="generic_call_script_key",
                    ),
                ],
            },
            bases=(
                django_prometheus.models.ExportModelOperationsMixin(
                    "GenericCallScript"
                ),
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name="CallScript",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "goal",
                    models.CharField(
                        choices=[
                            ("info_gathering", "Information Gathering"),
                            ("escalation", "Escalation"),
                        ],
                        max_length=32,
                    ),
                ),
                ("insurer_name", models.CharField(max_length=300)),
                ("encrypted_denial_reason", models.BinaryField()),
                ("encrypted_script_text", models.BinaryField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "for_denial",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="call_scripts",
                        to="fighthealthinsurance.denial",
                    ),
                ),
                (
                    "generic_script",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="fighthealthinsurance.genericcallscript",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["for_denial", "goal"],
                        name="fighthealth_for_den_dcea70_idx",
                    )
                ],
            },
            bases=(
                django_prometheus.models.ExportModelOperationsMixin("CallScript"),
                models.Model,
            ),
        ),
    ]

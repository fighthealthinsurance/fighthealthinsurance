import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0228_assistanthandoff_bound"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssistantDraft",
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
                ("draft_id_digest", models.CharField(max_length=64, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("waiting_for_agreement", "waiting_for_agreement"),
                            ("reading", "reading"),
                            ("questions", "questions"),
                            ("drafting", "drafting"),
                            ("ready", "ready"),
                            ("on_site", "on_site"),
                            ("stopped", "stopped"),
                            ("expired", "expired"),
                            ("site_only", "site_only"),
                        ],
                        default="waiting_for_agreement",
                        max_length=24,
                    ),
                ),
                ("status_at", models.DateTimeField(auto_now_add=True)),
                ("questions", models.JSONField(blank=True, default=list)),
                ("answers_at", models.DateTimeField(blank=True, null=True)),
                ("procedure", models.CharField(blank=True, default="", max_length=80)),
                ("condition", models.CharField(blank=True, default="", max_length=80)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "denial",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assistant_drafts",
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
        ),
    ]

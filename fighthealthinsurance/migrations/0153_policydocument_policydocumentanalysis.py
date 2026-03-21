import django.db.models.deletion
import django_encrypted_filefield.fields
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0152_ongoingchat_edited_chat_history"),
    ]

    operations = [
        migrations.CreateModel(
            name="PolicyDocument",
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
                    "document_enc",
                    django_encrypted_filefield.fields.EncryptedFileField(
                        null=True, upload_to=""
                    ),
                ),
                (
                    "document_type",
                    models.CharField(
                        choices=[
                            ("summary_of_benefits", "Summary of Benefits"),
                            ("medical_policy", "Medical Policy"),
                            ("other", "Other Policy Document"),
                        ],
                        default="other",
                        max_length=50,
                    ),
                ),
                ("filename", models.CharField(blank=True, max_length=255)),
                (
                    "hashed_email",
                    models.CharField(blank=True, max_length=200, null=True),
                ),
                (
                    "session_key",
                    models.CharField(blank=True, max_length=100, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Policy Document",
                "verbose_name_plural": "Policy Documents",
            },
        ),
        migrations.AddIndex(
            model_name="policydocument",
            index=models.Index(
                fields=["hashed_email"],
                name="fighthealti_hashed__4b3c6e_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="policydocument",
            index=models.Index(
                fields=["session_key"],
                name="fighthealti_session_pd_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="policydocument",
            index=models.Index(
                fields=["created_at"],
                name="fighthealti_created_pd_idx",
            ),
        ),
        migrations.CreateModel(
            name="PolicyDocumentAnalysis",
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
                ("user_question", models.TextField(blank=True)),
                ("exclusions", models.JSONField(default=list)),
                ("inclusions", models.JSONField(default=list)),
                ("appeal_clauses", models.JSONField(default=list)),
                ("summary", models.TextField(blank=True)),
                ("quotable_sections", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "policy_document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="analyses",
                        to="fighthealthinsurance.policydocument",
                    ),
                ),
                (
                    "chat",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="policy_analyses",
                        to="fighthealthinsurance.ongoingchat",
                    ),
                ),
            ],
            options={
                "verbose_name": "Policy Document Analysis",
                "verbose_name_plural": "Policy Document Analyses",
            },
        ),
        migrations.AddIndex(
            model_name="policydocumentanalysis",
            index=models.Index(
                fields=["policy_document"],
                name="fighthealti_policy__pda_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="policydocumentanalysis",
            index=models.Index(
                fields=["created_at"],
                name="fighthealti_created_pda_idx",
            ),
        ),
    ]

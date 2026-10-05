import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0229_assistantdraft"),
    ]

    operations = [
        migrations.AlterField(
            model_name="assistantdraft",
            name="denial",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="assistant_drafts",
                to="fighthealthinsurance.denial",
            ),
        ),
        migrations.CreateModel(
            name="AssistantAgreementCount",
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
                ("day", models.DateField(db_index=True)),
                ("key", models.CharField(max_length=64)),
                ("count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("day", "key"),
                        name="assistant_agreement_count_day_key",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="AssistantContinueLink",
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
                (
                    "token_digest",
                    models.CharField(max_length=64, null=True, unique=True),
                ),
                ("expires_at", models.DateTimeField(db_index=True)),
                (
                    "wrong_email_attempts",
                    models.PositiveSmallIntegerField(default=0),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "denial",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assistant_continue_link",
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
        ),
    ]

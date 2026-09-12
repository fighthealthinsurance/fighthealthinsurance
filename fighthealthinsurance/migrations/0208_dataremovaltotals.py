# Running totals of what delete-my-data requests removed
# (models.DataRemovalTotals): one row, counts only, no identifiers and no
# per-request timestamps. Updated by RemoveDataHelper inside the deletion's
# transaction, read by the staff status page so lifetime totals survive
# deletion. The single row is created lazily by the helper.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "fighthealthinsurance",
            "0207_merge_0206_denial_triage_0206_externalservicehealth",
        ),
    ]

    operations = [
        migrations.CreateModel(
            name="DataRemovalTotals",
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
                ("requests", models.PositiveIntegerField(default=0)),
                ("denials", models.PositiveIntegerField(default=0)),
                ("drafts", models.PositiveIntegerField(default=0)),
                ("faxes_delivered", models.PositiveIntegerField(default=0)),
                ("people_with_draft", models.PositiveIntegerField(default=0)),
                ("since", models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]

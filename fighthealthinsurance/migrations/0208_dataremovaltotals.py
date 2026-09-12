# Two single-row counter tables for the staff status page's lifetime numbers
# (models.LifetimeCounters, models.DataRemovalTotals) -- counters that only go
# up and that deleting a person's data cannot change -- plus a flag on Denial
# marking people already counted. No new identifiers. The lifetime row and
# the flags are seeded from the rows present when this migration runs.

from django.db import migrations, models

from fighthealthinsurance.lifetime_counters import seed_from_present_rows


class Migration(migrations.Migration):

    dependencies = [
        (
            "fighthealthinsurance",
            "0207_merge_0206_denial_triage_0206_externalservicehealth",
        ),
    ]

    operations = [
        migrations.CreateModel(
            name="LifetimeCounters",
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
                ("appeals_generated", models.PositiveIntegerField(default=0)),
                ("people_with_draft", models.PositiveIntegerField(default=0)),
                ("faxes_sent", models.PositiveIntegerField(default=0)),
                ("faxes_delivered", models.PositiveIntegerField(default=0)),
                ("since", models.DateTimeField(blank=True, null=True)),
            ],
        ),
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
                ("since", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddField(
            model_name="denial",
            name="person_counted",
            # db_default too: pods still on the previous image keep inserting
            # denials without this column until the rollout replaces them.
            field=models.BooleanField(db_default=False, default=False, editable=False),
        ),
        migrations.AddField(
            model_name="faxestosend",
            name="attempt_counted",
            field=models.BooleanField(db_default=False, default=False, editable=False),
        ),
        migrations.AddField(
            model_name="faxestosend",
            name="delivery_counted",
            field=models.BooleanField(db_default=False, default=False, editable=False),
        ),
        migrations.RunPython(seed_from_present_rows, migrations.RunPython.noop),
    ]

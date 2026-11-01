import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0252_mailinglistsubscriber_cleanup_review"),
    ]

    operations = [
        migrations.AddField(
            model_name="chooservote",
            name="all_bad",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="rationale",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="chooservote",
            name="chosen_candidate",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="votes",
                to="fighthealthinsurance.choosercandidate",
            ),
        ),
        migrations.AddConstraint(
            model_name="chooservote",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("all_bad", True), ("chosen_candidate__isnull", True)
                )
                | models.Q(("all_bad", False), ("chosen_candidate__isnull", False)),
                name="chooser_vote_outcome_matches_pick",
            ),
        ),
    ]

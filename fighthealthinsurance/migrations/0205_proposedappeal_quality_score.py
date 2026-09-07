# Draft quality scores from TypeSafe System One (ml/letter_quality.py).
# Three nullable columns, no index: the dashboard aggregates by model_name and
# created_at, both already indexed, and the page never queries by score.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0204_intakejourneyevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedappeal",
            name="quality_score",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="proposedappeal",
            name="grounding_score",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="proposedappeal",
            name="quality_scorer",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="proposedappeal",
            name="quality_scored_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]

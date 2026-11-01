from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0253_chooser_review_outcomes"),
    ]

    operations = [
        migrations.AddField(
            model_name="choosertask",
            name="scoring_json",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

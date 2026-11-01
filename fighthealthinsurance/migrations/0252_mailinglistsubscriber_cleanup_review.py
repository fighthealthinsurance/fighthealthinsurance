from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0251_chooser_vote_skip_voter"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailinglistsubscriber",
            name="cleanup_reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mailinglistsubscriber",
            name="cleanup_reviewed_by",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0197_denial_candidate_denial_text_summary"),
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

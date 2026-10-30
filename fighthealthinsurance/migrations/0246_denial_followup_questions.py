from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0245_chooser_case_version_and_ties"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="followup_questions",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

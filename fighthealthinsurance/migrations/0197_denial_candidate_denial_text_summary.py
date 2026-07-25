from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0196_proposedappeal_context_level_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="candidate_denial_text_summary",
            field=models.TextField(blank=True, null=True),
        ),
    ]

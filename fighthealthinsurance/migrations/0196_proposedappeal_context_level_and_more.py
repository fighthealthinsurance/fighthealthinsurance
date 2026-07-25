from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0195_denial_denial_text_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedappeal",
            name="context_level",
            field=models.CharField(blank=True, db_index=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="proposedappeal",
            name="speculative",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]

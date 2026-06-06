from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0181_denial_rag_imr_context"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="extract_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]

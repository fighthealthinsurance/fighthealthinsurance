from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0194_regulator_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="denial_text_summary",
            field=models.TextField(blank=True, null=True),
        ),
    ]

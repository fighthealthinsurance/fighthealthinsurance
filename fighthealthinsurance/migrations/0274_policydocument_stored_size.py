from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0273_policydocument_reading_outcomes"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydocument",
            name="stored_size",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]

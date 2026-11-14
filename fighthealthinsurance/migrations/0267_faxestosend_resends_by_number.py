from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0266_choosercandidate_synthesized_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="faxestosend",
            name="resends_by_number",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

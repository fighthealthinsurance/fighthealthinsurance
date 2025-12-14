# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0136_add_microsite_slug_to_denial"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatleads",
            name="microsite_slug",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]

# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0137_add_microsite_slug_to_chatleads"),
    ]

    operations = [
        migrations.AddField(
            model_name="ongoingchat",
            name="microsite_slug",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]

# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0138_add_microsite_slug_to_ongoingchat"),
    ]

    operations = [
        migrations.AlterField(
            model_name="denial",
            name="microsite_slug",
            field=models.CharField(
                blank=True, db_index=True, max_length=100, null=True
            ),
        ),
        migrations.AlterField(
            model_name="chatleads",
            name="microsite_slug",
            field=models.CharField(
                blank=True, db_index=True, max_length=100, null=True
            ),
        ),
        migrations.AlterField(
            model_name="ongoingchat",
            name="microsite_slug",
            field=models.CharField(
                blank=True, db_index=True, max_length=100, null=True
            ),
        ),
    ]

# Increase OngoingChat.denied_item / denied_reason from 500 to 2000 chars
# to avoid DataError when LLM analysis returns long values.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0176_add_1day_followup_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ongoingchat",
            name="denied_item",
            field=models.CharField(
                blank=True,
                help_text="The item that was denied, extracted from chat context",
                max_length=2000,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="ongoingchat",
            name="denied_reason",
            field=models.CharField(
                blank=True,
                help_text="The reason for denial, extracted from chat context",
                max_length=2000,
                null=True,
            ),
        ),
    ]

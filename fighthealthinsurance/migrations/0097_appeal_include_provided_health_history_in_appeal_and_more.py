# Generated by Django 5.1.5 on 2025-03-21 18:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0096_alter_faxestosend_combined_document_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="appeal",
            name="include_provided_health_history_in_appeal",
            field=models.BooleanField(default=False, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="include_provided_health_history",
            field=models.BooleanField(default=False),
        ),
    ]

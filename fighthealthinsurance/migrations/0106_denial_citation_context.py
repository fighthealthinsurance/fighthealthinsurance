# Generated by Django 5.1.5 on 2025-04-07 07:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "fighthealthinsurance",
            "0105_denial_dental_claim_denial_good_appeal_example_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="citation_context",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

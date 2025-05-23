# Generated by Django 5.1.5 on 2025-03-25 04:33

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0101_remove_loststripesession_item_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="loststripesession",
            name="cancel_url",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="loststripesession",
            name="email",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="loststripesession",
            name="payment_type",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="loststripesession",
            name="session_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="loststripesession",
            name="success_url",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]

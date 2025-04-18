# Generated by Django 5.1.4 on 2025-01-26 05:36

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0060_alter_plantype_alt_name_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="denial",
            name="denial_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="denial_type_text",
            field=models.TextField(blank=True, max_length=200, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="employer_name",
            field=models.CharField(blank=True, max_length=300, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="health_history",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="insurance_company",
            field=models.CharField(blank=True, max_length=300, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="raw_email",
            field=models.TextField(blank=True, max_length=300, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="reference_summary",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="denial",
            name="references",
            field=models.TextField(blank=True, null=True),
        ),
    ]

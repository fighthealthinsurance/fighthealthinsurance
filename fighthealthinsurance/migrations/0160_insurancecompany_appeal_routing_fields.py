# Migration adding appeal-routing fields to InsuranceCompany and InsurancePlan
# so that once a denial is matched to a known carrier we can suggest where
# (portal, fax, mailing address, etc.) to send the appeal.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0159_chatdocument"),
    ]

    operations = [
        migrations.AddField(
            model_name="insurancecompany",
            name="member_services_url",
            field=models.URLField(
                blank=True,
                help_text="Member services / contact-us page where appeal info is published",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeals_info_url",
            field=models.URLField(
                blank=True,
                help_text="Specific URL on the carrier's site that documents the appeals process (source for the values below)",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeal_address",
            field=models.TextField(
                blank=True,
                help_text="Default mailing address for written appeals (multi-line, free-form)",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeal_fax_number",
            field=models.CharField(
                blank=True,
                help_text="Default fax number for appeals (e.g., 877-815-4827)",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeal_phone_number",
            field=models.CharField(
                blank=True,
                help_text="Default member services / appeals phone number",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeal_email",
            field=models.EmailField(
                blank=True,
                help_text="Email address for submitting appeals, if accepted",
                max_length=254,
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="appeals_portal_url",
            field=models.URLField(
                blank=True,
                help_text="Online portal URL for submitting appeals electronically",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="parent_company",
            field=models.ForeignKey(
                blank=True,
                help_text="Parent / umbrella company (e.g., Elevance Health for Anthem brands)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="subsidiaries",
                to="fighthealthinsurance.insurancecompany",
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeal_address",
            field=models.TextField(
                blank=True,
                help_text="Plan-specific mailing address for appeals (overrides company default)",
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeal_fax_number",
            field=models.CharField(
                blank=True,
                help_text="Plan-specific appeals fax number (overrides company default)",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeal_phone_number",
            field=models.CharField(
                blank=True,
                help_text="Plan-specific appeals phone number (overrides company default)",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeal_email",
            field=models.EmailField(
                blank=True,
                help_text="Plan-specific email for submitting appeals (overrides company default)",
                max_length=254,
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeals_portal_url",
            field=models.URLField(
                blank=True,
                help_text="Plan-specific online appeals portal URL",
            ),
        ),
        migrations.AddField(
            model_name="insuranceplan",
            name="appeals_info_url",
            field=models.URLField(
                blank=True,
                help_text="URL documenting the appeals process for this specific plan (source)",
            ),
        ),
    ]

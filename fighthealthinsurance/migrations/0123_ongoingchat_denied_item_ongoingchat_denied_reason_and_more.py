# Generated by Django 5.1.5 on 2025-05-18 04:18

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fhi_users", "0041_professionaluser_credentials"),
        ("fighthealthinsurance", "0122_appeal_chat_priorauthrequest_chat"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ongoingchat",
            name="denied_item",
            field=models.CharField(
                blank=True,
                help_text="The item that was denied, extracted from chat context",
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="ongoingchat",
            name="denied_reason",
            field=models.CharField(
                blank=True,
                help_text="The reason for denial, extracted from chat context",
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="ongoingchat",
            name="is_patient",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="ongoingchat",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="chats",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="ongoingchat",
            name="professional_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="fhi_users.professionaluser",
            ),
        ),
    ]

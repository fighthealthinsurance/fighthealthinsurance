# Generated by Django 5.1.1 on 2024-09-25 03:54

import django.core.files.storage
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0016_denial_plan_documents"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="denial",
            name="plan_documents",
        ),
        migrations.CreateModel(
            name="PlanDocuments",
            fields=[
                (
                    "plan_document_id",
                    models.AutoField(primary_key=True, serialize=False),
                ),
                (
                    "plan_document",
                    models.FileField(
                        null=True,
                        storage=django.core.files.storage.FileSystemStorage(
                            location="external_data"
                        ),
                        upload_to="",
                    ),
                ),
                (
                    "denial",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fighthealthinsurance.denial",
                    ),
                ),
            ],
        ),
    ]

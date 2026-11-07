from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0259_denial_other_insurance"),
    ]

    operations = [
        migrations.CreateModel(
            name="UploadCount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("day", models.DateField(db_index=True)),
                ("period", models.CharField(max_length=16)),
                ("key", models.CharField(max_length=64)),
                ("count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("period", "key"), name="upload_count_period_key"
                    )
                ],
            },
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0275_sharedcopyretry"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydocument",
            name="insurer_name_source",
            field=models.CharField(
                blank=True,
                choices=[("model", "Our model"), ("staff", "Staff")],
                db_default="",
                default="",
                max_length=10,
                verbose_name="insurer found by",
            ),
        ),
        migrations.AddField(
            model_name="policydocument",
            name="plan_year_source",
            field=models.CharField(
                blank=True,
                choices=[("model", "Our model"), ("staff", "Staff")],
                db_default="",
                default="",
                max_length=10,
                verbose_name="plan year found by",
            ),
        ),
        migrations.AddField(
            model_name="policydocument",
            name="plan_category_source",
            field=models.CharField(
                blank=True,
                choices=[("model", "Our model"), ("staff", "Staff")],
                db_default="",
                default="",
                max_length=10,
                verbose_name="plan type found by",
            ),
        ),
    ]

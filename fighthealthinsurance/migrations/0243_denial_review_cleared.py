from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0242_policydocument_sharing"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="review_cleared",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                editable=False,
                max_length=300,
                null=True,
            ),
        ),
    ]

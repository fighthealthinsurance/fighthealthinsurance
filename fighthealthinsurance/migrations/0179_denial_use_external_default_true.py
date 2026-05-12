from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0178_clinicaltrial_clinicaltrialquerydata"),
    ]

    operations = [
        migrations.AlterField(
            model_name="denial",
            name="use_external",
            field=models.BooleanField(default=True),
        ),
    ]

# Marks the chosen rows the professional flow writes, so its "one pick per
# denial" replacement deletes only its own earlier picks. Every existing row
# defaults to False and is therefore never replaced.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0212_proposedappeal_presented_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedappeal",
            name="professional_pick",
            field=models.BooleanField(default=False),
        ),
    ]

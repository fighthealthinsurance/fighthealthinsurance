import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0233_letter_review"),
    ]

    operations = [
        migrations.AddField(
            model_name="assistantdraft",
            name="spend_reservation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assistant_drafts",
                to="fighthealthinsurance.spendreservation",
            ),
        ),
    ]

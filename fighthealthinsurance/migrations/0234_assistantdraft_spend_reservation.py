import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0232_followup_subjects_plain"),
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

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0265_interestedprofessional_blocked"),
    ]

    operations = [
        migrations.AddField(
            model_name="choosercandidate",
            name="synthesized_by",
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]

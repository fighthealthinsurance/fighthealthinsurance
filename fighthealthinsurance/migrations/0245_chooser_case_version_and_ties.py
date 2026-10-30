from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0244_professionalinquiry"),
    ]

    operations = [
        migrations.AddField(
            model_name="choosertask",
            name="case_version",
            field=models.PositiveSmallIntegerField(db_default=1, default=1),
        ),
        migrations.AddField(
            model_name="chooservote",
            name="tied_candidate_ids",
            field=models.JSONField(blank=True, default=list, null=True),
        ),
    ]

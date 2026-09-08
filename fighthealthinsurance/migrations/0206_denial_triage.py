# Denial triage columns from TypeSafe System One (ml/denial_triage.py).
# All nullable, no indexes: read per denial, aggregated only on the staff
# side over created_at, which is already indexed.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0205_proposedappeal_quality_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="denial",
            name="triage_category",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_category_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_regulation",
            field=models.CharField(blank=True, max_length=24, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_regulation_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_pre_service",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_urgent",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="appeal_deadline",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="appeal_deadline_label",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="appeal_deadline_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_source",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triage_text_hash",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="denial",
            name="triaged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

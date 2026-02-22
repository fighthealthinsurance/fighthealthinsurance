# Generated migration for patient dashboard performance optimization

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0150_add_appeal_progress_tracking"),
    ]

    operations = [
        # Index for patient dashboard appeals query (via Denial)
        migrations.AddIndex(
            model_name="denial",
            index=models.Index(
                fields=["patient_user", "-created"],
                name="denial_patient_created_idx",
            ),
        ),
        # Note: InsuranceCallLog(patient_user, -call_date) and
        # PatientEvidence(patient_user, -created_at) indexes are already
        # defined in their model Meta classes.
        #
        # Index for follow-up queries (partial index for non-null follow_up_date)
        migrations.AddIndex(
            model_name="insurancecalllog",
            index=models.Index(
                fields=["follow_up_date", "outcome"],
                name="calllog_followup_idx",
                condition=models.Q(follow_up_date__isnull=False),
            ),
        ),
    ]

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
                fields=["patient_user", "-created_at"],
                name="denial_patient_created_idx",
            ),
        ),
        # Index for call logs query
        migrations.AddIndex(
            model_name="insurancecalllog",
            index=models.Index(
                fields=["patient_user", "-call_date"],
                name="calllog_patient_date_idx",
            ),
        ),
        # Index for evidence query
        migrations.AddIndex(
            model_name="patientevidence",
            index=models.Index(
                fields=["patient_user", "-created_at"],
                name="evidence_patient_created_idx",
            ),
        ),
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

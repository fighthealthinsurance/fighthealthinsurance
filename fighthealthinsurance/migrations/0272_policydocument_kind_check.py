from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0271_policydocument_contact_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydocument",
            name="staff_says_plan",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AlterField(
            model_name="policydocument",
            name="processing_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("received", "Received, not read yet"),
                    ("ready_for_review", "Read and cleaned, ready for review"),
                    ("unreadable", "No text could be read"),
                    ("removed", "File and text deleted at the person's request"),
                    ("link", "A link, never fetched: staff open it by hand"),
                    ("potential_spam", "Potential spam: kept, never read or counted"),
                    ("personal", "A person's own papers: file kept, no text"),
                    ("not_a_plan", "Read, but not a plan document"),
                ],
                db_default="",
                default="",
                max_length=20,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0272_policydocument_kind_check"),
    ]

    operations = [
        migrations.AddField(
            model_name="policydocument",
            name="unreadable_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    (
                        "cannot_open",
                        "The file would not open (locked, broken or cut short)",
                    ),
                    ("garbled_text", "Its text was garbage"),
                    ("nothing_after_ocr", "Scanned pages OCR found nothing on"),
                    ("ocr_unavailable", "OCR could not run"),
                    ("did_not_finish", "The reading timed out or failed every try"),
                    ("file_not_read", "The stored file could not be fetched"),
                ],
                db_default="",
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="policydocument",
            name="read_attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="policydocument",
            name="read_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="policydocument",
            name="company_lookup_failures",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
    ]

"""Add denormalized search_text field to IMRDecision."""

from django.db import migrations, models


def backfill_search_text(apps, schema_editor):
    IMRDecision = apps.get_model("fighthealthinsurance", "IMRDecision")
    db_alias = schema_editor.connection.alias
    rows = (
        IMRDecision.objects.using(db_alias)
        .all()
        .only(
            "id",
            "treatment",
            "treatment_category",
            "treatment_subcategory",
            "diagnosis",
            "diagnosis_category",
        )
    )
    for row in rows.iterator():
        parts = [
            row.treatment,
            row.treatment_category,
            row.treatment_subcategory,
            row.diagnosis,
            row.diagnosis_category,
        ]
        row.search_text = " ".join(p.lower() for p in parts if p).strip()
        row.save(update_fields=["search_text"])


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0161_imrdecision"),
    ]

    operations = [
        migrations.AddField(
            model_name="imrdecision",
            name="search_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_search_text, migrations.RunPython.noop),
    ]

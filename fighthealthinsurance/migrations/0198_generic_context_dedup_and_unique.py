from django.db import migrations, models


def dedupe_generic_context(apps, schema_editor):
    """Keep only the most recent GenericContextGeneration row per
    (procedure, diagnosis) pair so the unique constraint below can apply.

    "Most recent" is the latest ``created_at``, with the higher primary key
    as a stable tie-breaker (rows created in the same auto_now_add instant).
    """
    GenericContextGeneration = apps.get_model(
        "fighthealthinsurance", "GenericContextGeneration"
    )
    seen: dict[tuple[str, str], int] = {}
    duplicate_ids: list[int] = []
    for row_id, procedure, diagnosis in (
        GenericContextGeneration.objects.order_by("-created_at", "-id")
        .values_list("id", "procedure", "diagnosis")
        .iterator()
    ):
        key = (procedure, diagnosis)
        if key in seen:
            duplicate_ids.append(row_id)
        else:
            seen[key] = row_id
    # Chunked so a large duplicate backlog can't build one giant IN clause.
    chunk_size = 500
    for start in range(0, len(duplicate_ids), chunk_size):
        GenericContextGeneration.objects.filter(
            id__in=duplicate_ids[start : start + chunk_size]
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("fighthealthinsurance", "0197_denial_candidate_denial_text_summary"),
    ]

    operations = [
        migrations.RunPython(
            dedupe_generic_context,
            # Deleted duplicates are unrecoverable; reversing the constraint
            # doesn't need them back.
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="genericcontextgeneration",
            constraint=models.UniqueConstraint(
                fields=("procedure", "diagnosis"),
                name="generic_ctx_proc_diag_uniq",
            ),
        ),
    ]

"""Data migration to clean up PubMedQueryData rows with empty or missing articles."""

from django.db import migrations
from django.db.models import Q


def cleanup_empty_pubmed_query_data(apps, schema_editor):
    PubMedQueryData = apps.get_model("fighthealthinsurance", "PubMedQueryData")
    # Delete rows where articles is null, empty string, or empty JSON array.
    # Q(articles__isnull=True) needed because SQL IN doesn't match NULL.
    deleted, _ = (
        PubMedQueryData.objects.using(schema_editor.connection.alias)
        .filter(
            Q(articles__isnull=True) | Q(articles__in=["", "[]", "null"]),
        )
        .delete()
    )
    if deleted:
        print(f"  Deleted {deleted} PubMedQueryData rows with empty articles")


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0153_useddeletetoken"),
    ]

    operations = [
        migrations.RunPython(
            cleanup_empty_pubmed_query_data,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

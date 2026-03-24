"""Data migration to clean up PubMedQueryData rows with empty or missing articles."""

from django.db import migrations


def cleanup_empty_pubmed_query_data(apps, schema_editor):
    PubMedQueryData = apps.get_model("fighthealthinsurance", "PubMedQueryData")
    # Delete rows where articles is null, empty string, or empty JSON array
    deleted, _ = PubMedQueryData.objects.filter(
        articles__in=[None, "", "[]", "null"],
    ).delete()
    if deleted:
        print(f"  Deleted {deleted} PubMedQueryData rows with empty articles")


class Migration(migrations.Migration):

    dependencies = [
        ("fighthealthinsurance", "0152_ongoingchat_edited_chat_history"),
    ]

    operations = [
        migrations.RunPython(
            cleanup_empty_pubmed_query_data,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

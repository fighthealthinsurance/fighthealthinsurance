"""The release before the share page saves PolicyDocument rows without the
columns migration 0242 adds. Staging migrates the shared database before
prod runs the new code, and a rollback runs the old code on the migrated
database, so every new required column has a default in the database too."""

import uuid

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance.models import PolicyDocument

# PolicyDocument's columns before migration 0242, as the last release saves them.
OLD_COLUMNS = (
    "id",
    "document_enc",
    "document_type",
    "plan_category",
    "filename",
    "hashed_email",
    "session_key",
    "created_at",
)


class TheLastReleaseCanStillSaveTest(TestCase):
    def save_the_old_way(self) -> uuid.UUID:
        pk = uuid.uuid4()
        raw = {
            "id": pk,
            "document_enc": None,
            "document_type": "summary_of_benefits",
            "plan_category": "unknown",
            "filename": "policy.pdf",
            "hashed_email": None,
            "session_key": None,
            "created_at": timezone.now(),
        }
        values = [
            PolicyDocument._meta.get_field(name).get_db_prep_value(
                raw[name], connection
            )
            for name in OLD_COLUMNS
        ]
        quote = connection.ops.quote_name
        columns = ", ".join(quote(name) for name in OLD_COLUMNS)
        marks = ", ".join(["%s"] * len(OLD_COLUMNS))
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {quote(PolicyDocument._meta.db_table)} "
                f"({columns}) VALUES ({marks})",
                values,
            )
        return pk

    def test_a_row_saved_without_the_new_columns_takes_their_defaults(self):
        doc = PolicyDocument.objects.get(pk=self.save_the_old_way())
        self.assertEqual(doc.source, PolicyDocument.SOURCE_EXPLAIN_POLICY)
        for name in (
            "state",
            "insurer_name",
            "employer_name",
            "premium_period",
            "who_is_covered",
            "fingerprint",
            "shared_link",
            "shared_link_fingerprint",
            "processing_status",
            "cleaned_text",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(doc, name), "")

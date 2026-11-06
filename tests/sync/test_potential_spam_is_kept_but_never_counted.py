"""Staff mark junk uploads as potential spam instead of deleting them.

From the PolicyDocument admin list, "Mark as potential spam" keeps a row and
its file but sets it aside: the hourly reading never picks it up, and no
count of shares, plans or uploads includes it. The staff dashboard shows how
many shared files are set aside. "Not spam: read again" undoes it, sending a
shared file back to be read. A removal request still deletes a marked row's
file like any other.
"""

import datetime
import uuid

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_documents_processing import awaiting_batches
from fighthealthinsurance.shared_plan_documents import (
    dashboard_counts,
    distinct_plan_count,
    plan_shares,
    remove_shared_documents,
)

User = get_user_model()

SPAM = PolicyDocument.PROCESSING_SPAM
READY = PolicyDocument.PROCESSING_READY
PLAN_TEXT = "Summary Plan Description, Crumpler Cooperative. " * 20
AN_HOUR_AGO = timezone.now() - datetime.timedelta(hours=1)


def shared_row(status=READY, source=PolicyDocument.SOURCE_SHARED, **fields):
    defaults = {
        "share_batch": uuid.uuid4(),
        "shared_at": AN_HOUR_AGO,
        "fingerprint": uuid.uuid4().hex,
        "cleaned_text": PLAN_TEXT,
    }
    defaults.update(fields)
    return PolicyDocument.objects.create(
        source=source, processing_status=status, **defaults
    )


class _AdminTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="sarah_loguen_fraser", password="pw-for-tests", email=""
        )
        self.client.force_login(self.admin)

    def act(self, action, *docs):
        return self.client.post(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"action": action, "_selected_action": [str(d.pk) for d in docs]},
            follow=True,
        )


class MarkingAsPotentialSpamTest(_AdminTestCase):
    def test_marked_rows_are_kept_with_the_new_status(self):
        doc = shared_row()
        self.act("mark_potential_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, SPAM)

    def test_an_explain_policy_upload_can_be_marked_too(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY
        )
        self.act("mark_potential_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, SPAM)

    def test_a_removed_row_stays_removed(self):
        doc = shared_row(status=PolicyDocument.PROCESSING_REMOVED)
        self.act("mark_potential_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, PolicyDocument.PROCESSING_REMOVED)

    def test_the_admin_says_how_many_were_marked(self):
        response = self.act("mark_potential_spam", shared_row(), shared_row())
        self.assertContains(response, "2 marked as potential spam")


class NotSpamTest(_AdminTestCase):
    def test_a_shared_file_goes_back_to_be_read(self):
        doc = shared_row(status=SPAM, processed_at=AN_HOUR_AGO)
        self.act("not_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.processed_at, doc.cleaned_text),
            (PolicyDocument.PROCESSING_RECEIVED, None, ""),
        )

    def test_a_shared_link_is_a_link_again(self):
        doc = shared_row(status=SPAM, source=PolicyDocument.SOURCE_SHARED_LINK)
        self.act("not_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, PolicyDocument.PROCESSING_LINK)

    def test_an_explain_policy_upload_has_no_reading_status_again(self):
        doc = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY, processing_status=SPAM
        )
        self.act("not_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, "")

    def test_rows_not_marked_are_left_alone(self):
        doc = shared_row()
        self.act("not_spam", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, READY)


class PotentialSpamIsNeverReadTest(TestCase):
    def test_the_sweep_does_not_pick_it_up(self):
        shared_row(status=SPAM)
        self.assertEqual(async_to_sync(awaiting_batches)(0, 10), [])


class PotentialSpamIsNeverCountedTest(TestCase):
    def test_it_is_not_a_share(self):
        shared_row(status=SPAM)
        self.assertEqual(plan_shares(), [])

    def test_it_is_not_a_plan(self):
        shared_row(status=SPAM)
        self.assertEqual(distinct_plan_count(), 0)

    def test_a_real_share_of_the_same_file_still_counts(self):
        spam = shared_row(status=SPAM)
        shared_row(fingerprint=spam.fingerprint)
        self.assertEqual(distinct_plan_count(), 1)

    def test_the_dashboard_counts_it_apart(self):
        shared_row(status=SPAM)
        counts = dashboard_counts(timezone.localdate())
        reading = {row["status"]: row["files"] for row in counts["reading"]}
        sources = {row["source"]: row["rows"] for row in counts["sources"]}
        self.assertEqual(
            (reading[SPAM], sources[PolicyDocument.SOURCE_SHARED]), (1, 0)
        )

    def test_explain_policy_uploads_marked_are_not_counted(self):
        PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY, processing_status=SPAM
        )
        counts = dashboard_counts(timezone.localdate())
        self.assertEqual(counts["explain_uploads"]["files"], 0)


class RemovalStillDeletesPotentialSpamTest(TestCase):
    def test_a_marked_row_is_removed_like_any_other(self):
        doc = shared_row(status=SPAM, hashed_email="crumpler-hash")
        remove_shared_documents("crumpler-hash")
        self.assertFalse(PolicyDocument.objects.filter(pk=doc.pk).exists())

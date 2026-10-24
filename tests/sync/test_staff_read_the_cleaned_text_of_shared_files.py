"""Staff read a shared file's cleaned text, and find files by their reading.

What a reading stored (the file's text with personal details taken out as
best we could) is shown to staff the same safe way as the raw text view:
plain text that runs nothing, never cached, behind the staff login. The
admin lists shared files by where their reading is, offers the cleaned text
first, never puts the cleaned text in its form, and can set files marked
unreadable back to waiting so they are read again.
"""

import datetime
import time

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.shared_plan_documents import render_token

User = get_user_model()

CLEANED = "Member name: [removed]\nGroup Number: 1234567890\nDeductible: $1,500"


class _CleanedTextTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def shared(self, status=PolicyDocument.PROCESSING_READY, cleaned=CLEANED):
        self.client.post(
            reverse("share_plan_documents"),
            {
                "plan_documents": [SimpleUploadedFile("sbc.txt", b"Summary")],
                "agree": "on",
                "rendered": render_token(at=time.time() - 60),
            },
        )
        doc = PolicyDocument.objects.latest("created_at")
        PolicyDocument.objects.filter(pk=doc.pk).update(
            processing_status=status,
            cleaned_text=cleaned,
            processed_at=timezone.now() if cleaned else None,
        )
        return doc

    def staff(self):
        return User.objects.create_user(
            username="sarah_loguen_fraser", password="pw-for-tests", is_staff=True
        )

    def read(self, doc, user=None):
        client = Client()
        if user is not None:
            client.force_login(user)
        return client.get(
            reverse("staff_policy_document_cleaned_text", kwargs={"pk": doc.pk})
        )


class StaffReadTheCleanedTextTest(_CleanedTextTestCase):
    def test_staff_read_the_cleaned_text_with_its_status(self):
        response = self.read(self.shared(), self.staff())
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        self.assertIn("Status: Read and cleaned, ready for review", text)
        self.assertIn(CLEANED, text)

    def test_it_is_plain_text_that_runs_nothing_and_is_never_cached(self):
        doc = self.shared(cleaned="<script>alert('Crumpler')</script>")
        response = self.read(doc, self.staff())
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            response["Content-Security-Policy"], "default-src 'none'; sandbox"
        )
        self.assertEqual(response["Content-Disposition"], "inline")
        self.assertIn("no-store", response["Cache-Control"])

    def test_people_who_are_not_staff_cannot_read_it(self):
        doc = self.shared()
        member = User.objects.create_user(
            username="mary_eliza_mahoney", password="pw-for-tests", is_staff=False
        )
        for response in (self.read(doc, member), self.read(doc)):
            with self.subTest(status=response.status_code):
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b"1234567890", response.content)

    def test_a_file_not_read_yet_or_with_no_text_says_so(self):
        staff = self.staff()
        waiting = self.read(
            self.shared(status=PolicyDocument.PROCESSING_RECEIVED, cleaned=""), staff
        )
        self.assertIn("Not read yet.", waiting.content.decode())
        unreadable = self.read(
            self.shared(status=PolicyDocument.PROCESSING_UNREADABLE, cleaned=""), staff
        )
        self.assertIn("Download the original.", unreadable.content.decode())

    def test_files_not_shared_for_research_have_no_cleaned_text_page(self):
        doc = PolicyDocument.objects.create(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.assertEqual(self.read(doc, self.staff()).status_code, 404)


class TheAdminFindsFilesByTheirReadingTest(_CleanedTextTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            username="susan_la_flesche_picotte", password="pw-for-tests"
        )
        self.client.force_login(self.admin)

    def test_the_list_filters_by_processing_status(self):
        ready = self.shared()
        unreadable = self.shared(status=PolicyDocument.PROCESSING_UNREADABLE, cleaned="")
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"processing_status__exact": PolicyDocument.PROCESSING_READY},
        ).content.decode()
        self.assertIn(str(ready.pk), page)
        self.assertNotIn(str(unreadable.pk), page)

    def test_the_change_page_offers_the_cleaned_text_first_and_never_as_a_field(self):
        doc = self.shared()
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_change", args=[doc.pk])
        ).content.decode()
        cleaned = reverse("staff_policy_document_cleaned_text", kwargs={"pk": doc.pk})
        raw = reverse("staff_policy_document_text", kwargs={"pk": doc.pk})
        self.assertLess(page.index(cleaned), page.index(raw))
        self.assertNotIn('name="cleaned_text"', page)
        self.assertNotIn('name="processing_status"', page)
        self.assertNotIn("1234567890", page)

    def test_no_cleaned_text_link_before_a_reading_is_ready(self):
        doc = self.shared(status=PolicyDocument.PROCESSING_RECEIVED, cleaned="")
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_change", args=[doc.pk])
        ).content.decode()
        self.assertNotIn(
            reverse("staff_policy_document_cleaned_text", kwargs={"pk": doc.pk}), page
        )


class StaffCanAskForAFileToBeReadAgainTest(_CleanedTextTestCase):
    """The admin's "Read again" sets shared files marked unreadable back to
    waiting, so the hourly sweep reads them again. Nothing else changes."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            username="mary_eliza_mahoney", password="pw-for-tests"
        )
        self.client.force_login(self.admin)

    def read_again(self, *docs):
        return self.client.post(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"action": "read_again", "_selected_action": [str(d.pk) for d in docs]},
            follow=True,
        )

    def test_unreadable_shared_files_go_back_to_waiting(self):
        unreadable = self.shared(status=PolicyDocument.PROCESSING_UNREADABLE, cleaned="")
        response = self.read_again(unreadable)
        unreadable.refresh_from_db()
        self.assertEqual(unreadable.processing_status, PolicyDocument.PROCESSING_RECEIVED)
        self.assertIsNone(unreadable.processed_at)
        self.assertContains(response, "1 file(s) will be read again")

    def test_other_files_are_left_as_they_are(self):
        ready = self.shared()
        waiting = self.shared(status=PolicyDocument.PROCESSING_RECEIVED, cleaned="")
        not_shared = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY
        )
        response = self.read_again(ready, waiting, not_shared)
        self.assertContains(response, "0 file(s) will be read again")
        ready.refresh_from_db()
        self.assertEqual(
            (ready.processing_status, ready.cleaned_text),
            (PolicyDocument.PROCESSING_READY, CLEANED),
        )
        for doc in (waiting, not_shared):
            before = doc.processing_status
            doc.refresh_from_db()
            self.assertEqual(doc.processing_status, before)

    def test_the_sweep_finds_them_again(self):
        unreadable = self.shared(status=PolicyDocument.PROCESSING_UNREADABLE, cleaned="")
        PolicyDocument.objects.filter(pk=unreadable.pk).update(
            shared_at=timezone.now() - datetime.timedelta(days=2)
        )
        self.assertEqual(async_to_sync(processing.awaiting_batches)(15, 10), [])
        self.read_again(unreadable)
        self.assertEqual(
            async_to_sync(processing.awaiting_batches)(15, 10),
            [str(unreadable.share_batch)],
        )


class TheStaffDashboardNamesEveryWorkflowTest(TestCase):
    def test_the_temporal_link_is_not_described_as_fax_only(self):
        self.client.force_login(
            User.objects.create_user(
                username="sarah_loguen_fraser", password="pw-for-tests", is_staff=True
            )
        )
        page = self.client.get(reverse("staff_dashboard")).content.decode()
        self.assertIn("Workflow runs and full event histories", page)
        self.assertNotIn("Fax workflow runs", page)

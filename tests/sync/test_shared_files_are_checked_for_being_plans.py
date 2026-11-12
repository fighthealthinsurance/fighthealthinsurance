"""A shared file's reading checks whether it is a plan document
(plan_document_kind).

A person's own papers (a bill, an EOB, a letter about them) keep their
encrypted file but no text, and are never counted. A file with no health
plan words keeps its text but is marked not a plan. Staff can mark a row as
personal papers, or say it is a plan, after which its next reading skips the
check. check_shared_document_kinds does the same for rows read before the
check, counting unless --apply. Only shared files are ever checked.
"""

import uuid
from io import StringIO
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_sponsors import identify_sponsor, link_sponsor
from tests.plan_text_samples import EOB, PLAN_TEXT, RECIPE
from tests.sync.test_shared_plans_are_counted_by_company import SPD_WITH_EIN
from tests.sync.test_shared_plan_documents_are_read_and_cleaned import (
    _ReadingTest,
    share,
    upload,
)

User = get_user_model()

READY = PolicyDocument.PROCESSING_READY
RECEIVED = PolicyDocument.PROCESSING_RECEIVED
PERSONAL = PolicyDocument.PROCESSING_PERSONAL
NOT_A_PLAN = PolicyDocument.PROCESSING_NOT_A_PLAN


class ReadingChecksForPlansTest(_ReadingTest):
    def test_a_persons_own_papers_keep_their_file_and_no_text(self):
        status, doc = self.read("eob.txt", EOB.encode())
        self.assertEqual(status, PERSONAL)
        self.assertEqual(doc.cleaned_text, "")
        self.assertTrue(doc.document_enc)
        self.assertIsNotNone(doc.processed_at)

    def test_a_persons_own_papers_are_never_looked_up_for_a_company(self):
        with patch.object(processing, "alink_company", new=AsyncMock()) as link:
            self.read("eob.txt", EOB.encode())
        link.assert_not_awaited()

    def test_a_file_with_no_plan_words_keeps_its_text_as_not_a_plan(self):
        status, doc = self.read("recipe.txt", RECIPE.encode())
        self.assertEqual(status, NOT_A_PLAN)
        self.assertIn("cornbread", doc.cleaned_text)

    def test_a_file_with_no_plan_words_is_never_looked_up_for_a_company(self):
        with patch.object(processing, "alink_company", new=AsyncMock()) as link:
            self.read("recipe.txt", RECIPE.encode())
        link.assert_not_awaited()

    def test_a_plan_is_still_ready_for_review(self):
        status, _ = self.read("plan.txt", PLAN_TEXT.encode())
        self.assertEqual(status, READY)

    def test_a_file_staff_said_is_a_plan_skips_the_check(self):
        (doc,) = share(upload("eob.txt", EOB.encode()))
        PolicyDocument.objects.filter(pk=doc.pk).update(staff_says_plan=True)
        self.assertEqual(self.process(doc), READY)

    def test_what_the_check_logs_names_the_row_and_its_kind_only(self):
        (doc,) = share(upload("eob.txt", EOB.encode()))
        with patch.object(processing.logger, "info") as info:
            self.process(doc)
        info.assert_any_call(f"Shared plan document {doc.pk} reads as personal")
        for call in info.call_args_list:
            self.assertNotIn("Crumpler", str(call))


def shared_row(status=READY, source=PolicyDocument.SOURCE_SHARED, **fields):
    defaults = {
        "share_batch": uuid.uuid4(),
        "shared_at": timezone.now(),
        "processed_at": timezone.now(),
        "cleaned_text": PLAN_TEXT,
    }
    defaults.update(fields)
    return PolicyDocument.objects.create(
        source=source, processing_status=status, **defaults
    )


class _AdminTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="mary_eliza_mahoney", password="pw-for-tests", email=""
        )
        self.client.force_login(self.admin)

    def act(self, action, *docs):
        return self.client.post(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"action": action, "_selected_action": [str(d.pk) for d in docs]},
            follow=True,
        )


class StaffMarkPersonalPapersTest(_AdminTestCase):
    def test_a_ready_row_loses_its_text_and_is_marked_personal(self):
        doc = shared_row()
        self.act("mark_personal", doc)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.cleaned_text), (PERSONAL, "")
        )

    def test_the_line_quoted_for_its_company_goes_too(self):
        doc = shared_row(plan_sponsor_evidence="Plan sponsor: Crumpler Mutual")
        self.act("mark_personal", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor_evidence, "")

    def test_a_row_still_waiting_is_left_alone(self):
        doc = shared_row(status=RECEIVED, cleaned_text="")
        self.act("mark_personal", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, RECEIVED)

    def test_an_explain_policy_upload_is_left_alone(self):
        doc = shared_row(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.act("mark_personal", doc)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, READY)


class StaffSayItIsAPlanTest(_AdminTestCase):
    def test_a_personal_row_waits_to_be_read_again_as_a_plan(self):
        doc = shared_row(status=PERSONAL, cleaned_text="")
        self.act("read_again_as_a_plan", doc)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.processing_status, doc.processed_at, doc.staff_says_plan),
            (RECEIVED, None, True),
        )

    def test_a_not_a_plan_row_waits_to_be_read_again_as_a_plan(self):
        doc = shared_row(status=NOT_A_PLAN)
        self.act("read_again_as_a_plan", doc)
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.cleaned_text), (RECEIVED, ""))

    def test_a_ready_row_is_left_alone(self):
        doc = shared_row()
        self.act("read_again_as_a_plan", doc)
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.staff_says_plan), (READY, False))


class TheListFiltersByTheNewStatusesTest(_AdminTestCase):
    def test_the_status_filter_offers_both(self):
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist")
        ).content.decode()
        self.assertIn("A person&#x27;s own papers: file kept, no text", page)
        self.assertIn("Read, but not a plan document", page)

    def test_filtering_by_personal_lists_only_personal_rows(self):
        personal = shared_row(status=PERSONAL, cleaned_text="")
        ready = shared_row()
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_changelist"),
            {"processing_status__exact": PERSONAL},
        ).content.decode()
        self.assertIn(str(personal.pk), page)
        self.assertNotIn(str(ready.pk), page)


class StaffReadWhyThereIsNoTextTest(_AdminTestCase):
    def test_a_personal_row_says_why_it_has_no_text(self):
        doc = shared_row(status=PERSONAL, cleaned_text="")
        page = self.client.get(
            reverse("staff_policy_document_cleaned_text", kwargs={"pk": doc.pk})
        ).content.decode()
        self.assertIn("a person's own papers", page)


class CheckingRowsReadBeforeTest(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("check_shared_document_kinds", *args, stdout=out)
        return out.getvalue()

    def test_it_counts_each_kind_apart_and_changes_nothing(self):
        personal = shared_row(cleaned_text=EOB)
        other = shared_row(cleaned_text=RECIPE)
        out = self.run_command()
        self.assertIn("1 shared file(s) read as ready would become personal", out)
        self.assertIn("1 shared file(s) read as ready would become not a plan", out)
        for doc in (personal, other):
            doc.refresh_from_db()
            self.assertEqual(doc.processing_status, READY)

    def test_it_never_prints_a_rows_text(self):
        shared_row(cleaned_text=EOB)
        self.assertNotIn("Crumpler", self.run_command("--apply"))

    def test_applied_personal_papers_lose_their_text(self):
        doc = shared_row(cleaned_text=EOB)
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.cleaned_text), (PERSONAL, ""))

    def test_applied_personal_papers_lose_the_line_quoted_for_their_company(self):
        doc = shared_row(cleaned_text=EOB, plan_sponsor_evidence="Plan: Crumpler Mutual")
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor_evidence, "")

    def test_applied_a_file_with_no_plan_words_keeps_its_text(self):
        doc = shared_row(cleaned_text=RECIPE)
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.cleaned_text), (NOT_A_PLAN, RECIPE))

    def test_a_plan_is_left_ready(self):
        doc = shared_row()
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, READY)

    def test_a_row_staff_said_is_a_plan_is_left_alone(self):
        doc = shared_row(cleaned_text=EOB, staff_says_plan=True)
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_status, READY)

    def test_an_explain_policy_upload_is_left_alone(self):
        doc = shared_row(source=PolicyDocument.SOURCE_EXPLAIN_POLICY, cleaned_text=EOB)
        self.run_command("--apply")
        doc.refresh_from_db()
        self.assertEqual((doc.processing_status, doc.cleaned_text), (READY, EOB))


class TheCompanyLookupSkipsPersonalPapersTest(TestCase):
    def test_a_company_found_after_a_row_was_marked_personal_is_not_linked(self):
        doc = shared_row(status=PERSONAL, cleaned_text="")
        link_sponsor(doc.pk, identify_sponsor(SPD_WITH_EIN))
        doc.refresh_from_db()
        self.assertEqual((doc.plan_sponsor_id, doc.plan_sponsor_evidence), (None, ""))

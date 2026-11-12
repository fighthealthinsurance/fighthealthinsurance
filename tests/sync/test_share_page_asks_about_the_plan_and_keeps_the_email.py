"""What the share page asks, and what it keeps.

Employer and insurer show without opening "About the plan", both optional.
A typed employer that matches a company already on the list links it,
marked as the person's: staff see it, and it counts toward the goal only
once a reading or staff confirms it. A PDF with no text to read is kept and
the person told. An email given on the share page is kept encrypted, so we
can write about the files; a copy shared from another page keeps none, and
a removal clears it.
"""

import time
import uuid
from unittest import mock

from bs4 import BeautifulSoup
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import consent, plan_sponsors, shared_plan_documents
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import Denial, PlanSponsor, PolicyDocument
from fighthealthinsurance.shared_plan_documents import (
    employer_plan_count,
    render_token,
    typed_employer_disagreements,
)

PDF = b"%PDF-1.4 Summary Plan Description for Crumpler Cooperative" + b" x" * 600
EMAIL = "rebecca.crumpler@example.com"
PLAN_TEXT = "Summary Plan Description, Crumpler Cooperative Gold PPO. " * 10
TEXT_LAYERS = "fighthealthinsurance.chat_forms.pdf_text_layers"


def pdf(name="crumpler-spd.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


def company(name):
    return PlanSponsor.objects.create(
        name=name, normalized_name=plan_sponsors.normalize_name(name)
    )


class _ShareTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("share_plan_documents")
        # Most tests are not about scans: say the PDF has text.
        self.has_text = True
        patcher = mock.patch(
            TEXT_LAYERS, side_effect=lambda contents: [self.has_text] * len(contents)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def share(self, files=None, follow=False, **fields):
        data = {
            "rendered": render_token(at=time.time() - 60),
            "plan_documents": files if files is not None else [pdf()],
            "agree": "on",
            **fields,
        }
        return self.client.post(self.url, data, follow=follow)

    def row(self):
        return PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED)


class EmployerAndInsurerShowTest(_ShareTestCase):
    def test_they_sit_outside_the_fold_and_are_optional(self):
        soup = BeautifulSoup(self.client.get(self.url).content, "html.parser")
        fold = soup.find(id="about-the-plan")
        for name in ("employer_name", "insurer_name"):
            field = soup.find(attrs={"name": name})
            self.assertIsNotNone(field)
            self.assertNotIn(field, fold.find_all(attrs={"name": name}))
            self.assertFalse(field.has_attr("required"))
            label = soup.find("label", attrs={"for": field["id"]})
            self.assertIn("(optional)", label.get_text())

    def test_a_share_left_blank_is_saved(self):
        response = self.share()
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual((row.employer_name, row.insurer_name), ("", ""))
        self.assertIsNone(row.plan_sponsor)


class ATypedEmployerTest(_ShareTestCase):
    def setUp(self):
        super().setUp()
        # The goal's count is cached for every visitor; start each test fresh.
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

    def test_it_links_a_company_already_on_the_list_as_the_persons(self):
        crumpler = company("Crumpler Cooperative")
        self.share(
            employer_name="Crumpler Cooperative, LLC", insurer_name="Mahoney Mutual"
        )
        row = self.row()
        self.assertEqual(row.plan_sponsor, crumpler)
        self.assertEqual(row.plan_sponsor_source, plan_sponsors.SOURCE_PERSON)
        self.assertEqual(row.employer_name, "Crumpler Cooperative, LLC")
        self.assertEqual(row.insurer_name, "Mahoney Mutual")

    def test_a_name_not_on_the_list_makes_no_company(self):
        self.share(employer_name="Crumpler Clinic")
        row = self.row()
        self.assertIsNone(row.plan_sponsor)
        self.assertEqual(row.employer_name, "Crumpler Clinic")
        self.assertFalse(PlanSponsor.objects.exists())

    def test_it_needs_no_readable_file(self):
        company("Crumpler Cooperative")
        self.has_text = False
        self.share(employer_name="Crumpler Cooperative")
        self.assertEqual(self.row().plan_sponsor_source, plan_sponsors.SOURCE_PERSON)

    def read(self):
        PolicyDocument.objects.filter(pk=self.row().pk).update(
            processing_status=PolicyDocument.PROCESSING_READY, cleaned_text=PLAN_TEXT
        )

    def test_it_never_counts_toward_the_goal_on_its_own(self):
        company("Crumpler Cooperative")
        self.share(employer_name="Crumpler Cooperative")
        self.read()
        self.assertEqual(employer_plan_count(), 0)

    def test_a_reading_that_finds_the_same_company_confirms_it(self):
        crumpler = company("Crumpler Cooperative")
        self.share(employer_name="Crumpler Cooperative")
        self.read()
        plan_sponsors.link_sponsor(
            self.row().pk,
            plan_sponsors.Sponsor(
                "Crumpler Cooperative", "", source=plan_sponsors.SOURCE_MODEL
            ),
        )
        row = self.row()
        self.assertEqual(row.plan_sponsor, crumpler)
        self.assertEqual(row.plan_sponsor_source, plan_sponsors.SOURCE_MODEL)
        self.assertEqual(employer_plan_count(), 1)
        self.assertEqual(typed_employer_disagreements(), 0)

    def test_a_reading_that_finds_another_company_keeps_what_was_typed(self):
        company("Crumpler Cooperative")
        self.share(employer_name="Crumpler Cooperative")
        self.read()
        plan_sponsors.link_sponsor(
            self.row().pk,
            plan_sponsors.Sponsor(
                "Mahoney Shipyards", "", source=plan_sponsors.SOURCE_MODEL
            ),
        )
        row = self.row()
        self.assertEqual(row.plan_sponsor.name, "Mahoney Shipyards")
        self.assertEqual(row.employer_name, "Crumpler Cooperative")
        self.assertEqual(typed_employer_disagreements(), 1)

    def test_a_company_the_reading_found_is_never_replaced_by_a_typed_one(self):
        mahoney = company("Mahoney Shipyards")
        company("Crumpler Cooperative")
        self.share()
        plan_sponsors.link_sponsor(
            self.row().pk,
            plan_sponsors.Sponsor(
                "Mahoney Shipyards", "", source=plan_sponsors.SOURCE_MODEL
            ),
        )
        plan_sponsors.link_typed_employer([self.row().pk], "Crumpler Cooperative")
        self.assertEqual(self.row().plan_sponsor, mahoney)

    def test_staff_see_the_disagreement_on_the_dashboard(self):
        from django.contrib.auth import get_user_model

        staff = get_user_model().objects.create_user(
            username="mary_mahoney", password="pw-for-tests", is_staff=True
        )
        company("Crumpler Cooperative")
        self.share(employer_name="Crumpler Cooperative")
        plan_sponsors.link_sponsor(
            self.row().pk,
            plan_sponsors.Sponsor(
                "Mahoney Shipyards", "", source=plan_sponsors.SOURCE_MODEL
            ),
        )
        self.client.force_login(staff)
        soup = BeautifulSoup(
            self.client.get(reverse("plan_documents_dashboard")).content, "html.parser"
        )
        card = soup.find(id="typed-employer-disagreements").get_text(" ", strip=True)
        self.assertTrue(card.startswith("1 share where the employer"))


class AScanIsKeptAndTheNoteSaysSoTest(_ShareTestCase):
    def thanks(self, response):
        return BeautifulSoup(response.content, "html.parser")

    def test_a_pdf_with_no_text_is_saved_and_the_person_told(self):
        self.has_text = False
        response = self.share(files=[pdf("scanned-sbc.pdf")], follow=True)
        self.assertTrue(
            PolicyDocument.objects.filter(source=PolicyDocument.SOURCE_SHARED).exists()
        )
        note = self.thanks(response).find(id="share-scans").get_text(" ", strip=True)
        self.assertIn("scanned-sbc.pdf", note)
        self.assertIn("We saved this file, but it looks like a scan", note)
        self.assertNotIn("can't", note)

    def test_a_pdf_with_text_says_nothing_more(self):
        response = self.share(follow=True)
        self.assertIsNone(self.thanks(response).find(id="share-scans"))

    def test_a_check_that_could_not_tell_says_nothing(self):
        self.has_text = None
        response = self.share(follow=True)
        self.assertIsNone(self.thanks(response).find(id="share-scans"))

    def test_the_note_shows_once(self):
        self.has_text = False
        self.share(follow=True)
        again = self.client.get(self.url + "?shared=1")
        self.assertIsNone(self.thanks(again).find(id="share-scans"))


class TheTextLayerCheckTest(TestCase):
    def check(self, texts=None, error=None):
        from fighthealthinsurance.chat_forms import pdf_has_text_layer

        fake = mock.AsyncMock(return_value=texts, side_effect=error)
        with mock.patch(
            "fighthealthinsurance.pdf_text.aextract_pdf_page_texts", new=fake
        ):
            return pdf_has_text_layer(PDF), fake

    def test_text_on_the_first_pages_is_a_text_layer(self):
        found, _ = self.check(texts=["Summary of Benefits and Coverage " * 3])
        self.assertTrue(found)

    def test_empty_pages_are_a_scan(self):
        found, _ = self.check(texts=["", "  \n "])
        self.assertFalse(found)

    def test_a_check_that_fails_cannot_tell(self):
        found, _ = self.check(error=TimeoutError())
        self.assertIsNone(found)

    def test_it_reads_a_few_pages_and_no_ocr(self):
        from fighthealthinsurance import chat_forms

        _, fake = self.check(texts=["x" * 80])
        content, pages, chars, seconds = fake.call_args.args
        self.assertEqual(pages, chat_forms._TEXT_LAYER_PAGES)
        self.assertLessEqual(pages, 3)
        self.assertEqual(seconds, chat_forms.SCAN_CHECK_BUDGET_SECONDS)


class TheEmailIsKeptForTheShareTest(_ShareTestCase):
    def test_an_email_given_is_kept_encrypted_with_the_agreement(self):
        self.share(email="Rebecca.Crumpler@Example.com")
        row = self.row()
        self.assertEqual(row.contact_email, EMAIL)
        self.assertEqual(row.hashed_email, Denial.get_hashed_email(EMAIL))
        self.assertEqual(row.shared_consent_version, consent.SHARING_VERSION)
        self.assertIsNotNone(row.shared_at)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT contact_email FROM {PolicyDocument._meta.db_table} WHERE id = %s",
                [row.pk.hex if connection.vendor == "sqlite" else row.pk],
            )
            stored = cursor.fetchone()[0]
        self.assertNotIn("crumpler", str(stored).lower())

    def test_no_email_keeps_none(self):
        self.share()
        self.assertEqual(self.row().contact_email, "")

    def test_the_words_beside_the_box_say_we_keep_it_and_may_write(self):
        self.assertIn("We may email you about your files", consent.SHARING_EMAIL_HINT)
        self.assertIn("stored encrypted", consent.SHARING_EMAIL_DETAILS[0])

    def test_a_removal_clears_it(self):
        self.share(email=EMAIL, plan_year="2027")
        RemoveDataHelper.remove_data_for_email(EMAIL)
        for row in PolicyDocument.objects.all():
            self.assertEqual(row.contact_email, "")

    def test_a_copy_shared_from_another_page_keeps_no_email(self):
        upload = pdf()
        rows = shared_plan_documents.save_shared_documents(
            [upload],
            source=PolicyDocument.SOURCE_SHARED_APPEAL,
            details={},
            hashed_email=Denial.get_hashed_email(EMAIL),
            contact_email=EMAIL,
        )
        self.assertEqual([row.contact_email for row in rows], [""])
        rows[0].document_enc.delete(save=False)

    def test_the_email_is_not_logged(self):
        with mock.patch.object(shared_plan_documents.logger, "info") as info:
            self.share(email=EMAIL)
        for call in info.call_args_list:
            self.assertNotIn("crumpler", str(call).lower())


class StaffCanReadTheEmailTest(_ShareTestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.staff = get_user_model().objects.create_superuser(
            username="mary_eliza_mahoney", password="pw-for-tests", email=""
        )

    def admin(self, name, *args):
        return reverse(f"admin:fighthealthinsurance_policydocument_{name}", args=args)

    def test_on_the_rows_own_page_only(self):
        self.share(email=EMAIL)
        row = self.row()
        self.client.force_login(self.staff)
        page = self.client.get(self.admin("change", row.pk))
        self.assertContains(page, EMAIL)
        self.assertContains(page, "Email to write to (kept encrypted)")
        # Read only: no field that could write it back.
        soup = BeautifulSoup(page.content, "html.parser")
        self.assertIsNone(soup.find(attrs={"name": "contact_email"}))
        listing = self.client.get(self.admin("changelist"))
        self.assertNotContains(listing, EMAIL)
        searched = self.client.get(self.admin("changelist"), {"q": "rebecca"})
        self.assertNotContains(searched, str(row.pk))

    def test_nobody_else_sees_it(self):
        self.share(email=EMAIL)
        row = self.row()
        response = self.client.get(self.admin("change", row.pk))
        self.assertNotEqual(response.status_code, 200)
        public = self.client.get(self.url + "?shared=1")
        self.assertNotContains(public, EMAIL)


class TheScanCheckHasOneBudgetTest(TestCase):
    def test_every_pdf_is_checked_at_once_within_one_budget(self):
        from fighthealthinsurance import chat_forms

        fake = mock.AsyncMock(return_value=["Summary of Benefits " * 5])
        with mock.patch(
            "fighthealthinsurance.pdf_text.aextract_pdf_page_texts", new=fake
        ):
            found = chat_forms.pdf_text_layers([PDF] * 10)
        self.assertEqual(found, [True] * 10)
        self.assertEqual(fake.await_count, 10)
        budgets = {call.args[3] for call in fake.await_args_list}
        self.assertEqual(budgets, {chat_forms.SCAN_CHECK_BUDGET_SECONDS})
        self.assertLessEqual(chat_forms.SCAN_CHECK_BUDGET_SECONDS, 8)

    def test_a_slow_pdf_says_nothing_and_holds_up_nothing(self):
        import asyncio
        import time as clock

        from fighthealthinsurance import chat_forms

        async def extract(content, pages, chars, timeout):
            if content == b"slow":
                await asyncio.sleep(timeout + 5)
            return [""]

        async def bounded(content, pages, chars, timeout):
            return await asyncio.wait_for(
                extract(content, pages, chars, timeout), timeout
            )

        with mock.patch(
            "fighthealthinsurance.pdf_text.aextract_pdf_page_texts", new=bounded
        ):
            started = clock.monotonic()
            found = chat_forms.pdf_text_layers([b"slow", PDF], budget_seconds=0.5)
        self.assertEqual(found, [None, False])
        self.assertLess(clock.monotonic() - started, 3)

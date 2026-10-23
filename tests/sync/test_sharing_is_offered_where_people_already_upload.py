"""Where people already upload plan documents, they can also share a copy.

Explain Policy and the appeal's plan documents step each offer an unticked
box, in a highlighted card that stands apart from the boxes a person must
tick, with the sharing agreement just above it (consent.SHARING_OPT_IN_TERMS,
dated by the same SHARING_VERSION as the share page). Under the box, "What
does sharing mean?" opens in place, and its link opens the share page in a
new tab, so nothing on the form is lost. Unticked, the page shares nothing
and works exactly as before. Ticked, each file that passes the closer look a
shared file gets is copied into a row of its own: its own source, the
agreement's date, a neutral name, no session key and no link to the appeal,
so its life is its own. A copy that can't be made never stops the page's own
work. The person's scrambled email goes with the copy, so a removal request
takes it too. The copies carry no premium: only the share page asks.
"""

import time

from bs4 import BeautifulSoup
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from unittest.mock import patch

from fighthealthinsurance import consent, shared_plan_documents
from fighthealthinsurance.form_utils import SHARE_FOR_RESEARCH_TERMS_ID
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    PlanDocuments,
    PlanSource,
    PolicyDocument,
)

EMAIL = "rebecca.crumpler@test-fhi.com"
SEMI_SEKRET = "sekret-for-the-share-box"
PDF = b"%PDF-1.4 Summary of Benefits for the Crumpler Cooperative" + b" x" * 100
TXT = b"Summary Plan Description, Crumpler Cooperative, plan year 2026."


def pdf(name="Rebecca Lee Crumpler SBC.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def txt(name="Rebecca Lee Crumpler SPD.txt", content=TXT):
    return SimpleUploadedFile(name, content, content_type="text/plain")


# Only the share page asks about the premium; a post to these pages is ignored.
PREMIUM_POST = {
    "premium_amount": "250",
    "premium_period": "monthly",
    "who_is_covered": "family",
}


def assertNoPremium(test, copy):
    test.assertIsNone(copy.premium_amount)
    test.assertEqual(copy.premium_period, "")
    test.assertEqual(copy.who_is_covered, "")


class _UploadsTestCase(TestCase):
    def tearDown(self):
        # Rows roll back with the test; their stored files would not.
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)
        for doc in PlanDocuments.objects.all():
            if doc.plan_document_enc:
                doc.plan_document_enc.delete(save=False)

    def shared_copies(self):
        return PolicyDocument.objects.filter(source__in=PolicyDocument.SHARED_SOURCES)


class TheBoxIsOfferedUntickedAndStandsOutTest(TestCase):
    def soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def check_box(self, soup, heading_tag):
        card = soup.find(id="share-for-research")
        self.assertIsNotNone(card)
        self.assertIn("fhi-card-info", card["class"])
        heading = card.find(id="share-for-research-heading")
        self.assertEqual(heading.name, heading_tag)
        self.assertIn("fhi-card-banner", heading["class"])
        self.assertEqual(heading.get_text(strip=True), "Help other people fight back")
        box = card.find("input", attrs={"name": "share_for_research"})
        self.assertEqual(box["type"], "checkbox")
        self.assertFalse(box.has_attr("checked"))
        self.assertFalse(box.has_attr("required"))
        self.assertIn("fhi-check-large", box["class"])
        # The box sits in the highlighted band, with its words beside it.
        band = box.find_parent("div")
        self.assertIn("fhi-notice-info", band["class"])
        label = card.find("label", attrs={"for": box["id"]})
        self.assertEqual(label.get_text(strip=True), consent.SHARING_OPT_IN_CHECKBOX)
        # The agreement is just above the box, as on the share page, and read
        # out with it.
        self.assertEqual(box["aria-describedby"], SHARE_FOR_RESEARCH_TERMS_ID)
        terms = card.find(id=SHARE_FOR_RESEARCH_TERMS_ID)
        self.assertEqual(terms.get_text(strip=True), consent.SHARING_OPT_IN_TERMS)
        self.assertIs(band.find_previous_sibling(), terms)
        # The card says what we learn, as the share page does.
        self.assertEqual(
            card.find(id="share-for-research-intro").get_text(" ", strip=True),
            "Share a copy of these documents with us. We learn from them what "
            "care costs, how costs and coverage change over time, and what needs "
            "approval first, and we share what we learn so others can fight back.",
        )
        return card

    def check_learn_more(self, soup):
        """Under the box, "What does sharing mean?" opens in place, and its
        link opens the share page's explanation in a new tab."""
        card = soup.find(id="share-for-research")
        more = card.find("details", id="share-for-research-more")
        self.assertIn("fhi-accordion-item", more["class"])
        self.assertIn("fhi-accordion", more.parent["class"])
        self.assertFalse(more.has_attr("open"))
        self.assertEqual(
            more.find("summary").get_text(strip=True), "What does sharing mean?"
        )
        band = card.find("input", attrs={"name": "share_for_research"}).parent
        self.assertIs(more.parent.find_previous_sibling(), band)
        # Opening it sends nothing and changes no answer.
        self.assertEqual(more.find_all(["input", "select", "textarea", "button"]), [])
        link = more.find("a", id="share-for-research-read-more")
        self.assertEqual(
            link.get_text(strip=True),
            "Read more about sharing your plan documents (opens in a new tab)",
        )
        self.assertEqual(
            link["href"], reverse("share_plan_documents") + "#about-sharing"
        )
        self.assertEqual(link["target"], "_blank")
        self.assertIn("noopener", link["rel"])
        # The anchor it points at is on the share page.
        page = self.soup(Client().get(reverse("share_plan_documents")).content)
        self.assertIsNotNone(page.find(id="about-sharing"))

    def explain_policy_page(self):
        return self.soup(Client().get(reverse("understand_policy")).content)

    def appeal_plan_documents_step(self):
        denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )
        response = Client().post(
            reverse("hh"),
            {
                "denial_id": denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
            },
        )
        return self.soup(response.content)

    def test_explain_policy_explains_sharing_without_leaving_the_form(self):
        self.check_learn_more(self.explain_policy_page())

    def test_the_appeal_step_explains_sharing_without_leaving_the_form(self):
        self.check_learn_more(self.appeal_plan_documents_step())

    def test_explain_policy_offers_it_just_above_the_button(self):
        soup = self.explain_policy_page()
        card = self.check_box(soup, "h3")
        form = soup.find("form", id="understand_policy_form")
        self.assertIs(card.find_parent("form"), form)
        # After the required boxes, so it can't be mistaken for one of them.
        following = card.find_next("button", attrs={"type": "submit"})
        self.assertIsNotNone(following)
        self.assertIsNotNone(
            card.find_previous("input", attrs={"name": "privacy_policy"})
        )

    def test_the_appeal_step_offers_it_under_the_file_box(self):
        soup = self.appeal_plan_documents_step()
        card = self.check_box(soup, "h2")
        self.assertIsNotNone(
            card.find_previous("input", attrs={"name": "plan_documents"})
        )
        self.assertIsNotNone(card.find_next("button", id="next"))

    def test_the_agreement_says_only_the_tick_shares_and_removal_takes_the_copy(self):
        terms = consent.SHARING_OPT_IN_TERMS
        self.assertTrue(
            terms.startswith("If you tick the box, you agree to share a copy")
        )
        self.assertNotIn("By uploading", terms)
        # Every point of the share page's own agreement, word for word.
        body = consent.SHARING_TERMS.split("Insurance. ", 1)[1]
        self.assertIn(body, terms)
        self.assertTrue(
            terms.endswith(
                "If you ask us to delete your data, we delete this copy too."
            )
        )
        for words in (terms, consent.SHARING_OPT_IN_CHECKBOX):
            self.assertNotIn("\u2014", words)


class ExplainPolicyTest(_UploadsTestCase):
    def upload(self, policy_document=None, **extra):
        data = {
            "first_name": "Rebecca",
            "last_name": "Crumpler",
            "email": EMAIL,
            "document_type": "summary_of_benefits",
            "plan_category": "aca_marketplace",
            "tos_agreement": "on",
            "privacy_policy": "on",
            "policy_document": policy_document or pdf(),
        }
        data.update(extra)
        self.client = Client()
        return self.client.post(reverse("understand_policy"), data)

    def test_unticked_it_shares_nothing(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 1)
        self.assertFalse(self.shared_copies().exists())

    def test_ticked_it_keeps_a_copy_of_its_own(self):
        response = self.upload(share_for_research="on")
        self.assertEqual(response.status_code, 200)
        own = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        copy = self.shared_copies().get()
        self.assertEqual(copy.source, PolicyDocument.SOURCE_SHARED_EXPLAIN)
        self.assertEqual(copy.shared_consent_version, consent.SHARING_VERSION)
        self.assertIsNotNone(copy.shared_at)
        self.assertEqual(copy.document_type, "summary_of_benefits")
        self.assertEqual(copy.plan_category, "aca_marketplace")
        self.assertEqual(copy.hashed_email, own.hashed_email)
        self.assertFalse(copy.session_key)
        self.assertNotIn("Crumpler", copy.filename)
        self.assertRegex(copy.fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(read_and_decrypt_file(copy.document_enc), PDF)
        self.assertEqual(read_and_decrypt_file(own.document_enc), PDF)

    def test_ticked_the_chat_still_reads_the_persons_own_upload(self):
        self.upload(share_for_research="on")
        own = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)
        self.assertEqual(self.client.session["policy_document_id"], str(own.pk))

    def test_a_file_that_fails_the_closer_look_is_analysed_but_not_shared(self):
        fake = SimpleUploadedFile("policy.docx", b"PK\x03\x04 fake docx content")
        response = self.upload(policy_document=fake, share_for_research="on")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 1)
        self.assertFalse(self.shared_copies().exists())

    def test_a_copy_that_cant_be_saved_never_stops_the_analysis(self):
        with patch.object(
            shared_plan_documents,
            "save_shared_documents",
            side_effect=RuntimeError("storage went away"),
        ):
            response = self.upload(share_for_research="on")
        self.assertEqual(response.status_code, 200)
        self.assertIn("policy_document_id", self.client.session)
        self.assertFalse(self.shared_copies().exists())

    def test_removing_my_data_removes_the_copy_too(self):
        self.upload(share_for_research="on")
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_the_copy_carries_no_premium(self):
        self.upload(share_for_research="on", **PREMIUM_POST)
        assertNoPremium(self, self.shared_copies().get())


class AppealPlanDocumentsStepTest(_UploadsTestCase):
    def setUp(self):
        self.client = Client()
        self.company = InsuranceCompany.objects.create(name="Crumpler Health Mutual")
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
            your_state="MA",
            employer_name="Lee Crumpler Clinic",
            insurance_company="Crumpler Health Mutual",
            insurance_company_obj=self.company,
            plan_id="PLAN-123-MEMBER",
            claim_id="CLAIM-456",
        )

    def post(self, files, **extra):
        data = {
            "denial_id": self.denial.denial_id,
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
            "plan_documents": files,
        }
        data.update(extra)
        return self.client.post(reverse("dvc"), data)

    def test_unticked_it_shares_nothing(self):
        response = self.post([pdf(), txt()])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlanDocuments.objects.filter(denial=self.denial).count(), 2)
        self.assertFalse(PolicyDocument.objects.exists())

    def test_ticked_the_appeal_keeps_its_files_and_copies_are_shared(self):
        response = self.post([pdf(), txt()], share_for_research="on")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "entity_extract.html")
        self.assertEqual(PlanDocuments.objects.filter(denial=self.denial).count(), 2)
        copies = list(self.shared_copies())
        self.assertEqual(len(copies), 2)
        self.assertEqual(len({copy.share_batch for copy in copies}), 1)
        contents = {read_and_decrypt_file(copy.document_enc) for copy in copies}
        self.assertEqual(contents, {PDF, TXT})
        for copy in copies:
            with self.subTest(copy=copy.filename):
                self.assertEqual(copy.source, PolicyDocument.SOURCE_SHARED_APPEAL)
                self.assertEqual(copy.shared_consent_version, consent.SHARING_VERSION)
                self.assertEqual(copy.hashed_email, self.denial.hashed_email)
                self.assertFalse(copy.session_key)
                self.assertNotIn("Crumpler", copy.filename)

    def test_the_copies_carry_what_the_appeal_knows_about_the_plan(self):
        self.denial.plan_source.add(
            PlanSource.objects.create(name="State Marketplace / Affordable Care Act  ")
        )
        self.post([pdf()], share_for_research="on")
        copy = self.shared_copies().get()
        self.assertEqual(copy.state, "MA")
        self.assertEqual(copy.employer_name, "Lee Crumpler Clinic")
        self.assertEqual(copy.insurer_name, "Crumpler Health Mutual")
        self.assertEqual(copy.insurance_company_obj, self.company)
        self.assertEqual(copy.plan_category, "aca_marketplace")

    def test_the_copies_carry_no_premium(self):
        self.post([pdf()], share_for_research="on", **PREMIUM_POST)
        assertNoPremium(self, self.shared_copies().get())

    def test_the_copies_carry_nothing_about_the_person_or_the_claim(self):
        self.post([pdf()], share_for_research="on")
        copy = self.shared_copies().get()
        for field in PolicyDocument._meta.concrete_fields:
            value = str(field.value_from_object(copy) or "")
            with self.subTest(field=field.name):
                self.assertNotIn("PLAN-123", value)
                self.assertNotIn("CLAIM-456", value)
                self.assertNotIn("crumpler@test-fhi.com", value.lower())

    def test_a_private_employer_plan_is_not_assumed_to_be_erisa(self):
        self.denial.plan_source.add(
            PlanSource.objects.create(name="Employer -- Private   ")
        )
        self.post([pdf()], share_for_research="on")
        self.assertEqual(self.shared_copies().get().plan_category, "unknown")

    def test_a_state_that_isnt_one_is_left_blank(self):
        Denial.objects.filter(pk=self.denial.pk).update(your_state="Atlantis")
        self.post([pdf()], share_for_research="on")
        self.assertEqual(self.shared_copies().get().state, "")

    def test_only_files_that_pass_the_closer_look_are_shared(self):
        binary = SimpleUploadedFile("scan.txt", b"MZ\x90\x00\x00")
        self.post([pdf(), binary], share_for_research="on")
        self.assertEqual(PlanDocuments.objects.filter(denial=self.denial).count(), 2)
        self.assertEqual(self.shared_copies().count(), 1)

    def test_a_copy_that_cant_be_saved_never_stops_the_appeal(self):
        with patch.object(
            shared_plan_documents,
            "save_shared_documents",
            side_effect=RuntimeError("storage went away"),
        ):
            response = self.post([pdf()], share_for_research="on")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "entity_extract.html")
        self.assertEqual(PlanDocuments.objects.filter(denial=self.denial).count(), 1)

    def test_the_copy_outlives_the_appeal_until_a_removal_request(self):
        self.post([pdf()], share_for_research="on")
        Denial.objects.filter(pk=self.denial.pk).delete()
        self.assertEqual(self.shared_copies().count(), 1)
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertFalse(self.shared_copies().exists())


class OptInSharesHaveTheirOwnDailyBudgetTest(_UploadsTestCase):
    def explain_with_the_box_ticked(self):
        return Client().post(
            reverse("understand_policy"),
            {
                "first_name": "Rebecca",
                "last_name": "Crumpler",
                "email": EMAIL,
                "document_type": "summary_of_benefits",
                "plan_category": "unknown",
                "tos_agreement": "on",
                "privacy_policy": "on",
                "policy_document": pdf(),
                "share_for_research": "on",
            },
        )

    def share_on_the_share_page(self):
        return Client().post(
            reverse("share_plan_documents"),
            {
                "plan_documents": [txt()],
                "agree": "on",
                "rendered": shared_plan_documents.render_token(at=time.time() - 60),
            },
        )

    def test_over_budget_the_page_goes_on_and_nothing_is_shared(self):
        with self.settings(SHARED_PLAN_DOCUMENTS_OPT_IN_DAILY_FILES=0):
            response = self.explain_with_the_box_ticked()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 1)
        self.assertFalse(self.shared_copies().exists())

    def test_opt_in_copies_count_against_their_own_budget(self):
        self.assertEqual(
            set(PolicyDocument.SHARED_SOURCES),
            {
                PolicyDocument.SOURCE_SHARED,
                PolicyDocument.SOURCE_SHARED_EXPLAIN,
                PolicyDocument.SOURCE_SHARED_APPEAL,
            },
        )
        self.explain_with_the_box_ticked()
        self.assertEqual(PolicyDocument.objects.count(), 2)
        # The person's own upload isn't shared; only the copy counts.
        for source in (
            PolicyDocument.SOURCE_SHARED_EXPLAIN,
            PolicyDocument.SOURCE_SHARED_APPEAL,
        ):
            with self.subTest(source=source):
                self.assertEqual(shared_plan_documents.shared_today(source), 1)
        self.assertEqual(shared_plan_documents.shared_today(), 0)

    def test_opt_in_copies_leave_the_share_pages_room(self):
        with self.settings(
            SHARED_PLAN_DOCUMENTS_DAILY_FILES=1,
            SHARED_PLAN_DOCUMENTS_OPT_IN_DAILY_FILES=1,
        ):
            self.explain_with_the_box_ticked()
            shared = self.share_on_the_share_page()
        self.assertEqual(shared.status_code, 302)
        self.assertEqual(
            PolicyDocument.objects.filter(source=PolicyDocument.SOURCE_SHARED).count(),
            1,
        )
        self.assertEqual(self.shared_copies().count(), 2)

    def test_a_full_share_page_leaves_room_for_opt_in_copies(self):
        with self.settings(SHARED_PLAN_DOCUMENTS_DAILY_FILES=0):
            self.explain_with_the_box_ticked()
        self.assertEqual(
            self.shared_copies()
            .filter(source=PolicyDocument.SOURCE_SHARED_EXPLAIN)
            .count(),
            1,
        )


class ErrorReportsFromTheOptInPagesCarryNoFormDataTest(TestCase):
    def test_both_pages_are_covered(self):
        from fighthealthinsurance import sentry_filters

        for name in ("understand_policy", "dvc", "share_plan_documents"):
            with self.subTest(page=name):
                event = {
                    "request": {
                        "url": "https://www.fighthealthinsurance.com" + reverse(name),
                        "data": {"email": EMAIL},
                    }
                }
                self.assertTrue(sentry_filters.is_sharing_form_event(event))

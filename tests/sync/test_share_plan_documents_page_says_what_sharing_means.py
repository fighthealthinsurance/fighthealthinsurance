"""The share-your-plan-documents page says plainly what sharing means.

Ticking the box agrees to every point of the sharing terms: the documents
come to us, we combine them with other people's and publish what we learn,
AI models read them (ours and outside services), we keep them as long as we
can, and people should leave out personal details unless they want them in,
while we do our best to take them out either way. The page also explains how
to ask a plan for its full documents, with a letter to copy that works
without JavaScript.
"""

import hashlib
import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import consent
from fighthealthinsurance.chat_forms import SharePlanDocumentsForm

TEMPLATES = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"

# Each point of the terms, as the page words it.
SHARING_POINTS = [
    "By uploading, you agree to share these documents with Fight Health Insurance.",
    "We combine them with documents from other people to learn how plans work, "
    "and we share what we learn publicly.",
    "AI models read them, including ones we run and outside services.",
    "We keep shared documents as long as we can.",
    "Please do your best to leave out your name, member ID and health "
    "information, unless you want to include them.",
    "Either way, we do our best to remove personal details from anything we share.",
]

# The words SHARING_VERSION dates. A change to the words needs a new date.
SHARING_WORDS_SHA256 = {
    "2026-10-23": "6c2b00892bd10f47ca46959e6ffbcbd796010303177b8eb6a7aa14e69bcb887c",
}

EM_DASH = "\u2014"

OFFICIAL_HOSTS = (
    "dol.gov",
    "ecfr.gov",
    "law.cornell.edu",
    "healthcare.gov",
    "opm.gov",
    "medicare.gov",
)


class SharePlanDocumentsPageTest(TestCase):
    def setUp(self):
        self.url = reverse("share_plan_documents")
        self.response = self.client.get(self.url)
        self.soup = BeautifulSoup(self.response.content, "html.parser")

    def test_the_page_renders_the_upload_form(self):
        self.assertEqual(self.response.status_code, 200)
        form = self.soup.find("form", id="share-plan-documents-form")
        self.assertIsNotNone(form)
        self.assertEqual(form["enctype"], "multipart/form-data")
        files = form.find("input", attrs={"name": "plan_documents"})
        self.assertEqual(files["type"], "file")
        self.assertTrue(files.has_attr("multiple"))
        self.assertTrue(files.has_attr("required"))
        agree = form.find("input", attrs={"name": "agree"})
        self.assertEqual(agree["type"], "checkbox")
        self.assertTrue(agree.has_attr("required"))

    def test_the_terms_say_every_point_next_to_the_box(self):
        terms = self.soup.find(id="sharing-terms").get_text(" ", strip=True)
        for point in SHARING_POINTS:
            with self.subTest(point=point):
                self.assertIn(point, terms)
        label = self.soup.find("label", attrs={"for": "id_agree"})
        self.assertEqual(label.get_text(strip=True), consent.SHARING_CHECKBOX)

    def test_the_page_says_plan_wide_documents_usually_carry_no_personal_details(
        self,
    ):
        self.assertContains(self.response, "usually don't include personal details")

    def test_only_the_files_and_the_agreement_are_required(self):
        form = SharePlanDocumentsForm()
        required = {name for name, field in form.fields.items() if field.required}
        self.assertEqual(required, {"plan_documents", "agree"})
        self.assertEqual(form.fields["plan_category"].initial, "unknown")

    def test_the_optional_fields_fold_under_about_the_plan(self):
        about = self.soup.find("details", id="about-the-plan")
        self.assertFalse(about.has_attr("open"))
        names = {field["name"] for field in about.find_all(["input", "select"])}
        self.assertEqual(names, set(SharePlanDocumentsForm.ABOUT_THE_PLAN))

    def test_the_page_asks_for_no_name(self):
        names = {
            field.get("name")
            for field in self.soup.find(id="share-plan-documents-form").find_all(
                ["input", "select", "textarea"]
            )
        }
        self.assertFalse({"first_name", "last_name", "name"} & names)

    def test_the_how_to_section_has_the_facts_and_the_date_checked(self):
        section = self.soup.find("section", id="get-your-plan-documents")
        text = section.get_text(" ", strip=True)
        self.assertIn("How to get your full plan documents", text)
        for fact in (
            "30 days",
            "$110 a day",
            "25 cents a page",
            "Form 5500",
            "7 business days",
            "1-866-444-3272",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, text)
        self.assertIn("Checked October 23, 2026.", text)

    def test_the_letter_is_plain_text_that_is_never_posted(self):
        letter = self.soup.find("textarea", id="request-letter")
        self.assertTrue(letter.has_attr("readonly"))
        self.assertFalse(letter.has_attr("name"))
        self.assertIsNone(letter.find_parent("form"))
        body = letter.get_text()
        self.assertTrue(body.startswith("[Date]"))
        self.assertIn(
            "Re: Request for plan documents under ERISA section 104(b)(4)", body
        )
        self.assertIn("within 30 days", body)

    def test_the_copy_button_only_shows_once_the_script_runs(self):
        button = self.soup.find("button", id="copy-letter")
        self.assertTrue(button.has_attr("hidden"))
        self.assertEqual(button["type"], "button")

    def test_every_source_link_goes_to_an_official_page(self):
        sources = self.soup.find("details", id="plan-documents-sources")
        links = [a["href"] for a in sources.find_all("a")]
        self.assertGreater(len(links), 10)
        for href in links:
            with self.subTest(href=href):
                self.assertTrue(href.startswith("https://"))
                host = re.match(r"https://([^/]+)", href).group(1)
                self.assertTrue(
                    any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)
                )

    def test_the_page_has_no_em_dashes(self):
        source = (TEMPLATES / "share_plan_documents.html").read_text(encoding="utf-8")
        self.assertNotIn(EM_DASH, source)
        main = self.soup.find("main").get_text()
        self.assertNotIn(EM_DASH, main)
        self.assertNotIn(EM_DASH, consent.SHARING_TERMS)

    def test_after_sharing_the_page_thanks_and_says_what_happens_next(self):
        response = self.client.get(self.url + "?shared=1")
        soup = BeautifulSoup(response.content, "html.parser")
        thanks = soup.find(id="share-thanks").get_text(" ", strip=True)
        self.assertIn("Thank you", thanks)
        self.assertIn("encrypted", thanks)
        self.assertIn("AI models will read them", thanks)
        self.assertIn("share what we learn", thanks)
        self.assertIsNone(soup.find("form", id="share-plan-documents-form"))
        self.assertIsNotNone(soup.find("section", id="get-your-plan-documents"))


class SharingTermsVersionTest(TestCase):
    def test_the_words_match_the_version_they_are_dated(self):
        words = consent.SHARING_TERMS + "\n" + consent.SHARING_CHECKBOX
        digest = hashlib.sha256(words.encode()).hexdigest()
        self.assertEqual(
            SHARING_WORDS_SHA256.get(consent.SHARING_VERSION.isoformat()),
            digest,
            "the sharing terms changed: give consent.SHARING_VERSION a new date "
            "and add its digest here",
        )


class SharePlanDocumentsLinkTest(TestCase):
    def test_how_to_help_links_to_the_page(self):
        response = self.client.get(reverse("how-to-help"))
        self.assertContains(response, f'href="{reverse("share_plan_documents")}"')

    def test_assistants_are_sent_to_the_page_rather_than_reading_it(self):
        from fighthealthinsurance import mcp_server

        self.assertIn("share_plan_documents", mcp_server._PERSONAL_START_PAGES)

"""The share-your-plan-documents page says plainly what sharing means.

Ticking the box agrees to the sharing terms. Their core sits beside the box,
always shown: we keep what people share, with what they tell us about the
plan, and use it with other people's documents to publish what we learn;
our team and AI models, ours and outside services, read the files; we keep
it as long as we can. The rest folds under "More about how we use what you
share": encryption, links opened by hand and never fetched, leaving out
personal details, our best effort to take them out, and how removal works.
The page says we ask once a year and take documents anytime. It also
explains how to ask a plan for its full documents, with a letter to copy
that works without JavaScript.
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

# The core terms beside the box, as the page words them.
SHARING_POINTS = [
    "We keep what you share, including what you tell us about the plan, and use "
    "it with other people's documents to publish what we learn about plans.",
    "Our team and AI models, including outside services, read your files.",
    "We keep everything as long as we can.",
]
# The details folded under them.
SHARING_DETAIL_POINTS = [
    "We store what you share encrypted, files and links alike.",
    "Our team opens links by hand. We never fetch them automatically.",
    "Please leave out your name, member ID and health information, unless you "
    "want to include them.",
    "We do our best to remove personal details from anything we publish.",
    "If you gave us your email, you can ask us to delete your data. We do our "
    "best to delete the files and links you shared. What we've learned about "
    "the plan stays, because it describes the plan, not you.",
]

# The words SHARING_VERSION dates: the share page's and the opt-in box's on
# Explain Policy and the plan documents step. A change to any of them needs
# a new date.
SHARING_WORDS_SHA256 = {
    "2026-10-23": "4f7bdb8a89fdaea06cc7aa7fa6a4b9615d13a993e5447582e861d9c143690d91",
    "2026-10-24": "06d9e69f62c2ba0b9470677a0bd286e4744787294cac0fc532a2aa8fc2afb20b",
    "2026-10-27": "976263b9b1f1d3c98d77d4da8237de17ae0defd254da2d843362f3959537f7e9",
    # From here the share page's folded details are part of the words.
    "2026-10-28": "4bd9edc25d22114e3057439d8d3e931f8fb52cd281610589388a8948a726d273",
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
        # Links can stand in for files, so the browser mustn't insist on one.
        self.assertFalse(files.has_attr("required"))
        agree = form.find("input", attrs={"name": "agree"})
        self.assertEqual(agree["type"], "checkbox")
        self.assertTrue(agree.has_attr("required"))

    def test_the_core_terms_show_beside_the_box(self):
        terms = self.soup.find(id="sharing-terms")
        for point in SHARING_POINTS:
            with self.subTest(point=point):
                self.assertIn(point, terms.get_text(" ", strip=True))
        self.assertIsNone(terms.find_parent("details"))
        box = self.soup.find("input", attrs={"name": "agree"})
        self.assertIs(box.find_previous(id="sharing-terms"), terms)
        label = self.soup.find("label", attrs={"for": "id_agree"})
        self.assertEqual(label.get_text(strip=True), consent.SHARING_CHECKBOX)

    def test_the_rest_of_the_terms_fold_under_the_core_before_the_box(self):
        fold = self.soup.find("details", id="what-happens")
        self.assertFalse(fold.has_attr("open"))
        self.assertEqual(
            fold.find("summary").get_text(strip=True),
            "More about how we use what you share",
        )
        self.assertEqual(
            [li.get_text(" ", strip=True) for li in fold.find_all("li")],
            SHARING_DETAIL_POINTS,
        )
        self.assertEqual(list(consent.SHARING_DETAILS), SHARING_DETAIL_POINTS)
        terms = self.soup.find(id="sharing-terms")
        box = self.soup.find("input", attrs={"name": "agree"})
        self.assertIs(terms.find_next("details"), fold)
        self.assertIs(fold.find_next("input", attrs={"name": "agree"}), box)

    def test_no_term_says_an_ai_model_reads_a_link(self):
        words = [consent.SHARING_TERMS, *consent.SHARING_DETAILS]
        for text in words:
            for sentence in re.split(r"(?<=\.) ", text):
                if "link" in sentence.lower():
                    with self.subTest(sentence=sentence):
                        self.assertNotIn("AI", sentence)
        self.assertIn("read your files", consent.SHARING_TERMS)

    def test_the_terms_promise_no_privacy_and_no_deletion(self):
        words = " ".join([consent.SHARING_TERMS, *consent.SHARING_DETAILS]).lower()
        for word in ("private", "anonymous", "confidential", "we delete", "we will delete"):
            with self.subTest(word=word):
                self.assertNotIn(word, words)

    def test_the_page_says_plan_wide_documents_usually_carry_no_personal_details(
        self,
    ):
        self.assertContains(self.response, "usually don't include personal details")

    def test_the_page_says_which_documents_help_most(self):
        items = [
            li.get_text(" ", strip=True)
            for li in self.soup.find(id="most-useful").find_all("li")
        ]
        self.assertEqual(
            items,
            [
                "Rate sheet or enrollment guide What you pay, for yourself and for "
                "family.",
                "Summary of Benefits and Coverage Deductibles, copays and limits, "
                "but not premiums.",
                "Summary Plan Description or Evidence of Coverage If you have it: "
                "what's left out, and how appeals work.",
            ],
        )
        versions = self.soup.find(id="which-versions").get_text(" ", strip=True)
        self.assertIn(
            "This year's and next year's versions help most: matched years show "
            "what changed.",
            versions,
        )
        # Plan materials only: a paystub shows the premium, but with a name on it.
        self.assertIn(
            "Please don't send paystubs, medical records or your own enrollment "
            "confirmation.",
            versions,
        )

    def test_the_upload_comes_first_then_what_to_send_then_the_why(self):
        # The goal, then the upload; what helps most under it; the reasons last.
        files = self.soup.find("input", attrs={"name": "plan_documents"})
        goal = self.soup.find(id="share-goal")
        self.assertEqual(goal.find_next("label")["for"], files["id"])
        cards = self.soup.find(id="most-useful")
        self.assertIs(cards.find_previous("input", attrs={"name": "plan_documents"}), files)
        for part in ("most-useful", "which-versions", "other-plans", "no-documents-yet"):
            with self.subTest(part=part):
                self.assertIsNotNone(files.find_next(id=part))
        form = self.soup.find("form", id="share-plan-documents-form")
        why = self.soup.find(id="about-sharing")
        self.assertEqual(why.find("h2").get_text(strip=True), "Why we're asking")
        self.assertIsNotNone(form.find_next(id="about-sharing"))
        self.assertIsNone(why.find_parent("form"))

    def test_the_top_asks_in_one_help_us_sentence(self):
        text = self.soup.find("main").get_text(" ", strip=True)
        self.assertEqual(text.count("Help us"), 1)

    def test_people_without_documents_find_the_letter_and_the_reminder(self):
        line = self.soup.find(id="no-documents-yet")
        hrefs = [a["href"] for a in line.find_all("a")]
        self.assertEqual(hrefs, ["#request-letter-heading", "#plan-documents-reminder"])
        for href in hrefs:
            with self.subTest(href=href):
                self.assertIsNotNone(self.soup.find(id=href[1:]))
        self.assertIsNotNone(line.find_next("input", attrs={"name": "agree"}))

    def test_other_kinds_of_plans_fold_under_the_cards(self):
        fold = self.soup.find("details", id="other-plans")
        self.assertFalse(fold.has_attr("open"))
        self.assertIsNotNone(fold.find_previous(id="most-useful"))

    def test_no_two_headings_say_the_same_thing(self):
        # A fold's summary counts as a heading; "Name: more" reads as "Name".
        headings = [
            tag.get_text(" ", strip=True).lower().split(":")[0]
            for tag in self.soup.find("main").find_all(["h1", "h2", "h3", "h4", "summary"])
        ]
        self.assertEqual(len(headings), len(set(headings)), headings)

    def test_every_fold_is_a_styled_expansion_with_a_short_summary(self):
        for fold in self.soup.find("main").find_all("details"):
            with self.subTest(fold=fold.get("id")):
                self.assertIn("fhi-accordion-item", fold["class"])
                summary = fold.find("summary")
                self.assertIsNotNone(summary.find(class_="fhi-accordion-question"))
                self.assertLessEqual(len(summary.get_text(strip=True)), 70)

    def test_the_page_says_what_we_will_look_at(self):
        text = self.soup.find(id="what-we-look-at").get_text(" ", strip=True)
        self.assertIn(
            "With plan documents we can show what plans cost for one person and "
            "for a family, what common care costs, how deductibles and "
            "out-of-pocket limits change each year, what plans leave out, and "
            "what needs approval first.",
            text,
        )

    def test_the_page_links_the_data_we_started_with(self):
        have = self.soup.find(id="what-we-have")
        hrefs = [a["href"] for a in have.find_all("a")]
        self.assertEqual(
            hrefs,
            [reverse("denial-watch"), reverse("open-enrollment") + "#coverage-mix"],
        )
        self.assertIn(
            "nearly half of people have coverage only through a job, and Denial "
            "Watch has no denial rates for those plans.",
            have.get_text(" ", strip=True),
        )

    def test_the_page_says_findings_describe_the_plans_people_share(self):
        text = self.soup.find(id="what-it-shows").get_text(" ", strip=True)
        self.assertIn(
            "what people are offered, not which plans they chose or every plan in "
            "the country.",
            text,
        )
        self.assertIn("Each finding says how many plans it comes from.", text)

    def test_the_page_asks_once_a_year_and_is_open_anytime(self):
        ask = self.soup.find(id="yearly-ask")
        self.assertEqual(
            ask.get_text(" ", strip=True),
            "Once a year we ask everyone to request their plan documents and "
            "share them with us. You can share anytime.",
        )

    def test_the_explanation_has_an_anchor_other_pages_link_to(self):
        about = self.soup.find(id="about-sharing")
        self.assertIsNone(about.find_parent("form"))
        self.assertEqual(
            about.find(id="why-short").get_text(strip=True),
            "We publish what we learn from the documents people share.",
        )
        fold = about.find("details", id="more-why")
        self.assertFalse(fold.has_attr("open"))
        self.assertEqual(
            fold.find("summary").get_text(strip=True), "More about why we're asking"
        )
        for part in ("what-we-have", "what-we-look-at", "what-it-shows", "yearly-ask"):
            with self.subTest(part=part):
                self.assertIsNotNone(fold.find(id=part))

    def test_the_plan_year_stays_optional_and_says_it_shows_trends(self):
        field = SharePlanDocumentsForm().fields["plan_year"]
        self.assertFalse(field.required)
        self.assertIn("plans change over time", field.help_text)
        year = self.soup.find("input", attrs={"name": "plan_year"})
        hint = self.soup.find(id=year["aria-describedby"])
        self.assertEqual(hint.get_text(strip=True), field.help_text)
        self.assertIsNotNone(year.find_parent("details", id="about-the-plan"))

    def test_only_the_agreement_is_required(self):
        # A file or a link is asked for by the form's clean(), not a field.
        form = SharePlanDocumentsForm()
        required = {name for name, field in form.fields.items() if field.required}
        self.assertEqual(required, {"agree"})
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

    def test_the_summary_of_benefits_facts_keep_their_limits(self):
        text = self.soup.find("section", id="get-your-plan-documents").get_text(
            " ", strip=True
        )
        # 45 CFR 147.200(a)(2)(i)(J): a group plan's address is for its certificate.
        self.assertIn(
            "certificate of coverage, which is not the full group policy", text
        )
        # HealthCare.gov lists only its own Marketplace plans.
        self.assertIn("Marketplace plans on HealthCare.gov", text)
        self.assertIn(
            "If your state runs its own marketplace, or you bought your plan "
            "straight from an insurer, ask the insurer for it.",
            text,
        )

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
        for words in (consent.SHARING_TERMS, *consent.SHARING_DETAILS):
            self.assertNotIn(EM_DASH, words)
            self.assertNotIn("\u2013", words)

    def test_after_sharing_the_page_thanks_and_says_what_happens_next(self):
        response = self.client.get(self.url + "?shared=1")
        soup = BeautifulSoup(response.content, "html.parser")
        thanks = soup.find(id="share-thanks").get_text(" ", strip=True)
        self.assertIn("Thank you", thanks)
        self.assertIn("publish what we learn", thanks)
        self.assertIsNone(soup.find("form", id="share-plan-documents-form"))
        self.assertIsNotNone(soup.find("section", id="get-your-plan-documents"))


class SharingTermsVersionTest(TestCase):
    def test_the_words_match_the_version_they_are_dated(self):
        words = "\n".join(
            [
                consent.SHARING_TERMS,
                *consent.SHARING_DETAILS,
                consent.SHARING_CHECKBOX,
                consent.SHARING_OPT_IN_TERMS,
                consent.SHARING_OPT_IN_CHECKBOX,
            ]
        )
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

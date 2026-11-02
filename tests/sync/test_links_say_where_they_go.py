"""Links say where they go, and that they open a new tab.

Oct 2026 accessibility audit, items M13, m7 and m8:

- A link that opens a new tab moved the person without warning. On the
  state help page, the send page's free fax services, the consent form, the
  share buttons and the footer, each such link now says so in words: the
  partials/opens_in_new_tab.html mark inside it, or its aria-label, which
  replaces anything inside the link for a screen reader.
- "click here" and a row of identical "Visit Website" buttons said nothing
  out of context, which is how a screen reader's list of links reads them.
- A link in a small hint, and the state help page's contact links, were
  told apart from the text around them by colour alone.
"""

import pathlib
import re

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"
CUSTOM_CSS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "css"
    / "custom.css"
).read_text()

NEW_TAB_WORDS = "(opens in a new tab)"
# The templates the audit named, each read as written.
CHECKED = (
    "state_help.html",
    "appeal.html",
    "base.html",
    "partials/user_consent_form_fields.html",
    "partials/share_buttons.html",
)
_NEW_TAB_LINK = re.compile(r"<a\b[^>]*target=\"_blank\"[^>]*>.*?</a>", re.S)


class EveryNewTabLinkSaysSoTest(SimpleTestCase):
    def test_each_link_that_opens_a_new_tab_says_so(self):
        for name in CHECKED:
            source = (TEMPLATES / name).read_text()
            links = _NEW_TAB_LINK.findall(source)
            self.assertTrue(links, name)
            for link in links:
                with self.subTest(template=name, link=link[:80]):
                    label = re.search(r'aria-label="([^"]*)"', link)
                    if label:
                        self.assertIn(NEW_TAB_WORDS, label.group(1))
                    else:
                        self.assertIn("partials/opens_in_new_tab.html", link)

    def test_the_mark_has_the_words_and_the_arrow(self):
        soup = BeautifulSoup(render_to_string("partials/opens_in_new_tab.html"), "html.parser")
        self.assertEqual(soup.find(class_="fhi-visually-hidden").get_text(), NEW_TAB_WORDS)
        self.assertIsNotNone(soup.find("svg"))

    def test_words_only_leaves_out_the_arrow(self):
        soup = BeautifulSoup(
            render_to_string("partials/opens_in_new_tab.html", {"words_only": True}),
            "html.parser",
        )
        self.assertEqual(soup.find(class_="fhi-visually-hidden").get_text(), NEW_TAB_WORDS)
        self.assertIsNone(soup.find("svg"))


class LinkTextSaysWhereItGoesTest(TestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_the_privacy_policy_pdf_link_names_what_it_downloads(self):
        soup = BeautifulSoup(
            Client().get(reverse("privacy_policy")).content.decode(), "html.parser"
        )
        link = soup.find("a", attrs={"download": True})
        self.assertEqual(link.get_text(strip=True), "download this policy as a PDF")
        self.assertNotIn("click here", soup.get_text(" ").lower())

    def test_each_visit_website_button_names_its_programme(self):
        soup = BeautifulSoup(
            Client().get(reverse("state_help", args=["california"])).content.decode(),
            "html.parser",
        )
        buttons = [
            a for a in soup.find_all("a") if a.get_text(" ", strip=True).startswith("Visit Website")
        ]
        self.assertTrue(buttons, "no Visit Website button on the California page")
        names = []
        for button in buttons:
            hidden = button.find(class_="fhi-visually-hidden")
            self.assertIsNotNone(hidden)
            self.assertTrue(hidden.get_text(strip=True).startswith("of "), hidden)
            names.append(hidden.get_text(strip=True))
        self.assertEqual(len(names), len(set(names)), names)


class LinksInRunningTextUnderlineTest(SimpleTestCase):
    def test_a_link_in_a_small_hint_underlines(self):
        start = CUSTOM_CSS.index("main :where(p, li")
        listed = CUSTOM_CSS[start + len("main :where(") : CUSTOM_CSS.index(")", start)]
        self.assertIn("small", [part.strip() for part in listed.split(",")])

    def test_the_state_help_contact_links_underline_at_rest(self):
        source = (TEMPLATES / "state_help.html").read_text()
        body = re.search(r"\.contact-item a \{([^}]*)\}", source).group(1)
        self.assertIn("text-decoration: underline", body)
        self.assertNotIn(".contact-item a:hover", source)

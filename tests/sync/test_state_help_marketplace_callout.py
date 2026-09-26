"""The state help index tells people moved onto a plan they did not choose
who to call.

Under the national cards, in the same column, a note gives the Marketplace
call center's number and its TTY line, both as links a phone can dial, says
that a state running its own marketplace has its own number, and links
HealthCare.gov's list of state marketplaces and CMS's handout on enrollment
without consent. The numbers come from CMS (checked 2026-09-25), so the
wording is held exactly.
"""

from bs4 import BeautifulSoup
from django.test import Client, TestCase
from django.urls import reverse

SENTENCE = (
    "If your state uses HealthCare.gov, call the Marketplace at 1-800-318-2596 "
    "(TTY 1-855-889-4325) and ask them to investigate. If your state runs its "
    "own marketplace, call that marketplace instead."
)

NEW_TAB_LINKS = {
    "https://www.healthcare.gov/marketplace-in-your-state/": "Find your state's marketplace",
    "https://www.cms.gov/files/document/agent-broker-infographic-2024-final.pdf": "Official guidance",
}


def _text(node) -> str:
    # Text as it reads: the links run inline with the words around them, so
    # nothing is inserted between elements, and whitespace is collapsed.
    return " ".join(node.get_text().split())


class MarketplaceCalloutTest(TestCase):
    def setUp(self):
        body = Client().get(reverse("state_help_index")).content.decode("utf-8")
        self.page = BeautifulSoup(body, "html.parser")
        self.section = self.page.find("section", id="national-resources")
        self.assertIsNotNone(self.section, "the National Resources section is gone")
        self.callout = self.section.find("div", class_="marketplace-callout")
        self.assertIsNotNone(self.callout, "the marketplace callout is not in National Resources")

    def test_the_heading_is_an_h3_under_the_sections_h2(self):
        # The section is an h2 and its cards are h3s, so the callout's
        # heading is an h3 too and the outline skips nothing.
        heading = self.callout.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        self.assertIsNotNone(heading)
        self.assertEqual(heading.name, "h3")
        self.assertEqual(heading.get_text(strip=True), "Enrolled or switched without permission?")
        self.assertEqual(self.section.find("h2").get_text(strip=True), "National Resources")

    def test_it_follows_the_four_cards_in_the_same_column(self):
        column = self.section.find("h2").parent
        self.assertIs(self.callout.parent, column)
        cards = self.callout.find_previous_sibling("div")
        self.assertIsNotNone(cards)
        self.assertEqual(len(cards.find_all("div", class_="national-card")), 4)

    def test_the_sentence_is_exactly_as_written(self):
        self.assertEqual(_text(self.callout.find("p")), SENTENCE)

    def test_both_numbers_can_be_dialled(self):
        paragraph = self.callout.find("p")
        dialled = {a["href"]: a.get_text(strip=True) for a in paragraph.find_all("a", href=True)}
        self.assertEqual(
            dialled,
            {"tel:+18003182596": "1-800-318-2596", "tel:+18558894325": "1-855-889-4325"},
        )

    def test_the_tty_number_is_named_as_tty(self):
        self.assertIn("(TTY 1-855-889-4325)", _text(self.callout.find("p")))

    def test_the_two_links_open_in_a_new_tab_and_say_so(self):
        for href, label in NEW_TAB_LINKS.items():
            with self.subTest(href=href):
                link = self.callout.find("a", href=href)
                self.assertIsNotNone(link, "%s is not linked" % href)
                self.assertEqual(_text(link), "%s (opens in a new tab)" % label)
                self.assertEqual(link.get("target"), "_blank")
                self.assertEqual(set(link.get("rel", [])), {"noopener", "noreferrer"})

    def test_no_em_dash_in_the_callout(self):
        self.assertNotIn("\u2014", self.callout.get_text())

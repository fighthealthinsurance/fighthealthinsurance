"""The number pages and the share page, linked to one another.

Denial Watch, the open enrollment page and what coverage costs are the
number pages; the share page asks for the plan documents behind the next
ones. All four carry one related pages section (partials/related_pages.html)
in the number pages' style, in the same place: just before Sources and
method on a number page, and last on the share page. It links the other
three with one line each, so the share ask shows on every number page. Each
number page's steps ask for plan documents in the same words
(partials/share_plan_documents_step.html).
"""

import pathlib
import re
from datetime import date
from unittest import mock

from bs4 import BeautifulSoup
from django.test import Client, TestCase, override_settings
from django.urls import reverse

IN_SEASON = date(2026, 10, 28)
AFTER_SEASON = date(2027, 1, 16)

# Each page's address name -> the card that links to it, in the section's order.
CARDS = {
    "denial-watch": "related-denial-watch",
    "open-enrollment": "related-open-enrollment",
    "coverage-costs": "related-coverage-costs",
    "share_plan_documents": "related-share",
}
NUMBER_PAGES = ("denial-watch", "open-enrollment", "coverage-costs")
# Each number page's steps section.
STEPS = {"denial-watch": "act", "open-enrollment": "enroll-act", "coverage-costs": "act"}
SHARE_STEP = (
    "Share your plan's documents. Send the rate sheet and Summary of Benefits "
    "and Coverage, nothing with your name on it, so we can show what plans cost."
)


def _soup(name: str, day: date = IN_SEASON) -> BeautifulSoup:
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        response = Client().get(reverse(name))
    assert response.status_code == 200, name
    return BeautifulSoup(response.content, "html.parser")


def _text(node) -> str:
    """Visible text, one space between words, none before a comma or full stop after a link."""
    return re.sub(r" ([,.])", r"\1", " ".join(node.get_text(" ", strip=True).split()))


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class RelatedPagesTest(TestCase):
    def test_each_page_links_the_other_three_and_leaves_itself_out(self):
        for name, own in CARDS.items():
            section = _soup(name).find("section", id="related-pages")
            with self.subTest(page=name):
                self.assertIsNotNone(section)
                cards = section.select(".fhi-card")
                self.assertEqual([c["id"] for c in cards], [c for c in CARDS.values() if c != own])
                for card in cards:
                    target = next(n for n, c in CARDS.items() if c == card["id"])
                    self.assertEqual([a["href"] for a in card.find_all("a")], [reverse(target)])

    def test_each_card_is_a_linked_title_and_one_line(self):
        for name in CARDS:
            for card in _soup(name).find(id="related-pages").select(".fhi-card"):
                with self.subTest(page=name, card=card["id"]):
                    self.assertIsNotNone(card.select_one("h3.fhi-card-title > a"))
                    (line,) = card.find_all("p")
                    self.assertEqual(_text(line).count(". "), 0, _text(line))
                    self.assertTrue(_text(line).endswith("."), _text(line))

    def test_it_is_in_the_number_pages_style(self):
        for name in CARDS:
            soup = _soup(name)
            section = soup.find(id="related-pages")
            with self.subTest(page=name):
                self.assertIsNotNone(section)
                self.assertEqual(section.get("aria-labelledby"), "related-pages-title")
                self.assertEqual(section.find("p").get("class"), ["dw-kicker"])
                self.assertEqual(_text(section.find("h2")), "More on costs, coverage and denials")
                self.assertEqual(section.find("h2").find_next_sibling().get("class"), ["fhi-cards"])
                # The number pages' kicker, from the shared style partial every one of these pages includes.
                self.assertIn(".dw-kicker {", str(soup.find_all("style")))

    def test_it_sits_just_before_sources_and_method_on_a_number_page(self):
        for name in NUMBER_PAGES:
            section = _soup(name).find("section", id="related-pages")
            with self.subTest(page=name):
                self.assertIsNotNone(section)
                after = section.find_next_sibling("section")
                before = section.find_previous_sibling("section")
                self.assertEqual(after.get("id") if after else None, "sources")
                self.assertRegex(before.get("id") if before else "", r"^more-data")

    def test_it_comes_last_on_the_share_page(self):
        section = _soup("share_plan_documents").find("section", id="related-pages")
        self.assertIsNotNone(section)
        self.assertIsNone(section.find_next_sibling())
        self.assertEqual(section.find_previous_sibling("section")["id"], "get-your-plan-documents")
        # Outside the form, so it never posts.
        self.assertIsNone(section.find_parent("form"))

    def test_the_share_ask_is_open_on_every_number_page(self):
        for name in NUMBER_PAGES:
            card = _soup(name).find(id="related-share")
            with self.subTest(page=name):
                self.assertIsNotNone(card)
                self.assertIsNone(card.find_parent("details"))
                self.assertEqual(
                    _text(card),
                    "Share your plan documents Send the documents your plan gives everyone, "
                    "so we can show what plans cost and cover.",
                )

    def test_the_open_enrollment_card_takes_its_pages_title_for_the_day(self):
        for day in (IN_SEASON, AFTER_SEASON):
            with self.subTest(day=day):
                title = _soup("open-enrollment", day).find("h1")
                card = _soup("coverage-costs", day).find(id="related-open-enrollment")
                self.assertEqual(_text(card.find("h3")), _text(title))

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_without_denial_watch_it_links_only_pages_that_serve(self):
        self.assertEqual(
            [c["id"] for c in _soup("coverage-costs").find(id="related-pages").select(".fhi-card")],
            ["related-share"],
        )
        self.assertEqual(
            [c["id"] for c in _soup("share_plan_documents").find(id="related-pages").select(".fhi-card")],
            ["related-coverage-costs"],
        )

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_without_denial_watch_the_share_page_names_its_pages_without_links(self):
        paragraph = _soup("share_plan_documents").find(id="what-we-have")
        self.assertEqual(paragraph.find_all("a"), [])
        self.assertIn("Denial Watch shows", _text(paragraph))
        self.assertIn("Where people get coverage shows", _text(paragraph))

    def test_no_long_dashes(self):
        for name in CARDS:
            text = _text(_soup(name).find(id="related-pages"))
            for dash in ("\u2014", "\u2013"):
                with self.subTest(page=name, dash=dash):
                    self.assertNotIn(dash, text)


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class ShareStepTest(TestCase):
    def test_each_number_pages_steps_ask_for_plan_documents_in_the_same_words(self):
        for name, steps in STEPS.items():
            soup = _soup(name)
            step = soup.find(id="act-share")
            with self.subTest(page=name):
                self.assertIsNotNone(step)
                self.assertIsNotNone(step.find_parent(id=steps))
                self.assertEqual(_text(step), SHARE_STEP)
                self.assertEqual([a["href"] for a in step.find_all("a")], [reverse("share_plan_documents")])

    def test_the_open_enrollment_steps_ask_out_of_season_too(self):
        step = _soup("open-enrollment", AFTER_SEASON).find(id="act-share")
        self.assertIsNotNone(step)
        self.assertEqual(_text(step), SHARE_STEP)


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class DenialWatchMenuTest(TestCase):
    """In the header, Denial Watch is a dropdown like Resources: Denial Watch
    first, then the other number pages and the share page."""

    def _menu(self, day: date = IN_SEASON):
        summary = _soup("about", day).find("summary", id="denialwatchmenu")
        self.assertIsNotNone(summary)
        return summary, summary.find_next_sibling("ul")

    def _links(self, day: date = IN_SEASON) -> list[tuple[str, str]]:
        return [(a.get_text(strip=True), a["href"]) for a in self._menu(day)[1].find_all("a")]

    def test_in_season_it_links_denial_watch_then_the_other_pages(self):
        self.assertEqual(
            self._links(IN_SEASON),
            [
                ("Denial Watch", reverse("denial-watch")),
                ("Open Enrollment", reverse("open-enrollment")),
                ("Coverage Costs for 2027", reverse("coverage-costs")),
                ("Share Your Plan Documents", reverse("share_plan_documents")),
            ],
        )

    def test_out_of_season_it_leaves_out_open_enrollment(self):
        self.assertEqual(
            self._links(AFTER_SEASON),
            [
                ("Denial Watch", reverse("denial-watch")),
                ("Coverage Costs for 2027", reverse("coverage-costs")),
                ("Share Your Plan Documents", reverse("share_plan_documents")),
            ],
        )

    def test_it_is_built_like_the_other_dropdowns(self):
        summary, submenu = self._menu()
        group = summary.parent
        self.assertEqual((group.name, group.get("class"), group.get("name")), ("details", ["fhi-nav-group"], "fhi-nav-group"))
        self.assertEqual(summary.get("class"), ["fhi-nav-link"])
        self.assertEqual(submenu.get("class"), ["fhi-nav-submenu"])
        self.assertEqual(submenu.find("a", id="denialwatchlink")["href"], reverse("denial-watch"))

    def test_it_keeps_the_new_badge_while_denial_watch_is_new(self):
        summary, _ = self._menu(IN_SEASON)
        self.assertEqual(_text(summary), "Denial Watch New")
        self.assertEqual(summary.find("span").get("class"), ["fhi-nav-new"])
        self.assertIsNone(self._menu(AFTER_SEASON)[0].find(class_="fhi-nav-new"))

    def test_coverage_costs_sits_here_not_under_resources(self):
        soup = _soup("about")
        resources = next(s for s in soup.select("summary.fhi-nav-link") if _text(s) == "Resources")
        self.assertIsNone(resources.find_next_sibling("ul").find("a", href=reverse("coverage-costs")))
        self.assertEqual(len(soup.find(id="navbar").find_all("a", href=reverse("coverage-costs"))), 1)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_hidden_when_the_flag_is_off(self):
        self.assertIsNone(_soup("about").find(id="denialwatchmenu"))


class SharePageMatchesTheNumberPagesTest(TestCase):
    """After the form, the share page's sections open as the number pages'
    do, and a tile's source is a note, as on what coverage costs."""

    def test_each_section_after_the_form_opens_with_a_kicker_then_its_heading(self):
        soup = _soup("share_plan_documents")
        kickers = {}
        for section_id in ("about-sharing", "get-your-plan-documents", "related-pages"):
            section = soup.find("section", id=section_id)
            first, second = section.find_all(recursive=False)[:2]
            with self.subTest(section=section_id):
                self.assertEqual((first.name, first.get("class")), ("p", ["dw-kicker"]))
                self.assertEqual((second.name, second.get("id")), ("h2", section["aria-labelledby"]))
            kickers[section_id] = _text(first)
        self.assertEqual(
            kickers,
            {
                "about-sharing": "What the numbers can't show yet",
                "get-your-plan-documents": "No documents yet?",
                "related-pages": "Related pages",
            },
        )

    def test_the_job_share_tile_gives_its_source_as_a_note(self):
        tile = _soup("share_plan_documents").find(id="why-job-share")
        note = tile.find("p", class_="dw-note")
        self.assertIsNotNone(note)
        self.assertEqual(_text(note), "Census Bureau survey, 2026")
        self.assertIs(note.parent, tile)

    def test_kickers_and_notes_keep_their_own_size_in_the_reading_column(self):
        # The reading column sets paragraphs a step larger; the number pages' small type opts out, as .fhi-note does.
        css = (pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "css" / "custom.css").read_text()
        selector = re.search(r"\.fhi-page :where\(p, li\):not\(([^)]*)\)", css).group(1)
        for kept in (".fhi-note", ".dw-kicker", ".dw-note"):
            with self.subTest(kept=kept):
                self.assertIn(kept, [part.strip() for part in selector.split(",")])

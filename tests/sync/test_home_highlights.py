"""The home page's pointers to Denial Watch and the open enrollment page.

One pill in the hero: open enrollment's while the season runs, Denial
Watch's before it. Under How It Works, two cards, each with one number
from Denial Watch's bundle (static/denial_watch.json). All of it hides
behind DENIAL_WATCH_PAGE_ENABLED, like the pages themselves.
"""

from datetime import date
from unittest import mock

from bs4 import BeautifulSoup
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from tests.sync.test_denial_watch import DATA, _between, _millions, _picks, _text

IN_SEASON = date(2026, 10, 25)
BEFORE_SEASON = date(2026, 10, 14)
AFTER_SEASON = date(2027, 1, 16)
# Each side of every date the pill or the season turns on.
EDGES = [
    BEFORE_SEASON,
    date(2026, 10, 15),
    date(2026, 11, 2),
    date(2026, 11, 3),
    date(2026, 12, 7),
    date(2026, 12, 8),
    date(2027, 1, 15),
    AFTER_SEASON,
]


def _get(name: str, day: date) -> str:
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        return Client().get(reverse(name)).content.decode()


def _home(day: date = IN_SEASON) -> str:
    return _get("root", day)


def _pills(html: str) -> list:
    return BeautifulSoup(html, "html.parser").select(".hero-announce")


def _section(html: str):
    return BeautifulSoup(html, "html.parser").find(id="home-highlights")


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class TestHomeSeasonPill(TestCase):
    def test_in_season_the_pill_points_to_open_enrollment(self):
        from fighthealthinsurance.views import denial_watch_enrollment

        (pill,) = _pills(_home(IN_SEASON))
        self.assertEqual(pill["href"], reverse("open-enrollment"))
        year = denial_watch_enrollment(IN_SEASON)["plan_year"]
        self.assertEqual(
            pill.get_text(" ", strip=True),
            f"Open enrollment for {year}: dates, numbers and what to do",
        )

    def test_before_the_season_the_pill_is_denial_watchs(self):
        (pill,) = _pills(_home(BEFORE_SEASON))
        self.assertEqual(pill["href"], reverse("denial-watch"))
        self.assertEqual(pill["id"], "denial-watch-hero")

    def test_after_the_season_there_is_no_pill(self):
        self.assertEqual(_pills(_home(AFTER_SEASON)), [])

    def test_only_ever_one_pill(self):
        for day in EDGES:
            with self.subTest(day=day):
                self.assertLessEqual(len(_pills(_home(day))), 1)

    def test_the_season_pill_needs_no_script(self):
        # Its words are all in the markup, and the counter's script isn't sent.
        html = _home(IN_SEASON)
        self.assertIn("dates, numbers and what to do", _between(html, 'id="season-hero"', "</a>"))
        self.assertNotIn('getElementById("denial-watch-hero")', html)

    def test_the_season_pill_has_a_named_block_to_swap(self):
        from tests.sync.test_denial_watch import REPO

        template = (REPO / "fighthealthinsurance" / "templates" / "index.html").read_text()
        block = _between(template, "{% block season_pill %}", "{% endblock season_pill %}")
        self.assertIn('id="season-hero"', block)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_no_pill_when_the_flag_is_off(self):
        for day in (BEFORE_SEASON, IN_SEASON):
            with self.subTest(day=day):
                self.assertEqual(_pills(_home(day)), [])


@override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
class TestHomeHighlights(TestCase):
    def _card(self, card_id: str, day: date = IN_SEASON):
        return _section(_home(day)).find(id=card_id)

    def test_both_cards_render(self):
        cards = _section(_home()).select(".fhi-card")
        self.assertEqual([c["id"] for c in cards], ["denial-watch-home", "open-enrollment-home"])

    def test_each_card_has_one_number_and_one_link(self):
        for card in _section(_home()).select(".fhi-card"):
            with self.subTest(card=card["id"]):
                self.assertEqual(len(card.select(".home-highlight-num")), 1)
                self.assertEqual(len(card.find_all("a")), 1)

    def test_denial_watch_number_is_the_bundles_latest_rate(self):
        latest = DATA["trend"][-1]
        stat = self._card("denial-watch-home").select_one(".home-highlight-stat")
        self.assertEqual(
            stat.get_text(" ", strip=True),
            f"About 1 in {round(1 / latest['in_rate'])} denied in-network claims on "
            f"HealthCare.gov plans, by insurers in {latest['year']}",
        )

    def test_enrollment_number_is_the_bundles_latest_plan_picks(self):
        picks = _picks()
        year = max(picks)
        stat = self._card("open-enrollment-home").select_one(".home-highlight-stat")
        self.assertEqual(
            stat.select_one(".home-highlight-num").get_text(strip=True),
            f"{_millions(picks[year]).removesuffix(' million')}M marketplace plans",
        )
        self.assertIn(f"picked for {year} in open enrollment", stat.get_text(" ", strip=True))

    def test_enrollment_number_matches_the_open_enrollment_pages_tile(self):
        tile = _between(_get("open-enrollment", IN_SEASON), 'id="enroll-numbers"', "</b>")
        num = self._card("open-enrollment-home").select_one(".home-highlight-num").get_text(strip=True)
        # Same figure, with "million" shortened to M on the card.
        self.assertTrue(num.endswith("M marketplace plans"), num)
        self.assertTrue(tile.endswith(num.removesuffix("M marketplace plans") + " million"), (tile, num))

    def test_cards_link_to_their_pages(self):
        for card_id, name in (("denial-watch-home", "denial-watch"), ("open-enrollment-home", "open-enrollment")):
            with self.subTest(card=card_id):
                self.assertEqual(self._card(card_id).find("a")["href"], reverse(name))

    def test_enrollment_card_takes_the_pages_title_for_the_day(self):
        # In season the page is "Open enrollment for <year>"; out of it, the sign-up page.
        for day in (IN_SEASON, AFTER_SEASON):
            with self.subTest(day=day):
                page_h1 = BeautifulSoup(_get("open-enrollment", day), "html.parser").find("h1")
                title = self._card("open-enrollment-home", day).find("h3")
                self.assertEqual(title.get_text(strip=True), page_h1.get_text(strip=True))
        self.assertNotEqual(
            self._card("open-enrollment-home", IN_SEASON).find("h3").get_text(),
            self._card("open-enrollment-home", AFTER_SEASON).find("h3").get_text(),
        )

    def test_the_section_sits_after_the_appeal_start_and_before_the_rest(self):
        html = _home()
        order = ['id="scanlink"', 'id="how-it-works"', 'id="home-highlights"', 'id="featured"', 'id="start-where-you-are"']
        at = [html.index(marker) for marker in order]
        self.assertEqual(at, sorted(at))

    def test_heading_levels(self):
        soup = BeautifulSoup(_home(), "html.parser")
        self.assertEqual(len(soup.find_all("h1")), 1)
        levels = [h.name for h in _section(_home()).find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]
        self.assertEqual(levels, ["h2", "h3", "h3"])

    def test_no_dashes_in_the_copy(self):
        for day in (IN_SEASON, AFTER_SEASON):
            html = _home(day)
            text = _text(str(_section(html))) + " ".join(p.get_text() for p in _pills(html))
            for dash in ("—", "–", "&mdash;", "&ndash;"):
                self.assertNotIn(dash, text, day)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_hidden_when_the_flag_is_off(self):
        html = _home()
        self.assertIsNone(_section(html))
        self.assertNotIn(reverse("open-enrollment"), html)

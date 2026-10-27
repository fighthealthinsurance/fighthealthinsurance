"""Grids that left most of a row empty, and cards stretched hollow.

The glossary and the state list opened a grid row for every letter, so a
letter with one or two entries left the rest of its row empty. Denial
Watch's seven jump tiles sat in a row of five and a row of two, and the AI
assistants page's seven cards ran three, three and one. On Resources the
government card stretched to the height of the four groups beside it, and on
How to Help the short card stretched to the share card's height.
"""

import pathlib
import re

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.urls import reverse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"
CUSTOM_CSS = REPO_ROOT / "fighthealthinsurance" / "static" / "css" / "custom.css"


def _bodies(css: str, selector: str) -> list[str]:
    """Every body given to exactly ``selector``, in order, comments gone."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        body
        for found, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if found.strip() == selector
    ]


def _declared(body: str, prop: str) -> str:
    found = re.findall(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body)
    return found[-1].strip() if found else ""


def _style(template: str) -> str:
    text = (TEMPLATES / template).read_text()
    return text[text.index("<style>") : text.index("</style>")]


class AnIndexIsOneRunOfEntriesTest(TestCase):
    """Both lettered indexes render one run of entries, every letter a
    marker at the head of its own entries, every jump link landing."""

    def _check(self, url: str, index_class: str) -> None:
        soup = BeautifulSoup(self.client.get(url).content, "html.parser")
        runs = soup.select(f".{index_class}.fhi-index")
        self.assertEqual(len(runs), 1, "the index is not one run")
        run = runs[0]
        self.assertFalse(run.select(".fhi-cards"), "a letter opens a grid of its own")
        sections = run.find_all(id=re.compile(r"^letter-"), recursive=False)
        self.assertTrue(sections)
        for section in sections:
            with self.subTest(letter=section["id"]):
                first, *rest = section.find_all(recursive=False)
                self.assertIn("fhi-index-letter", first["class"])
                self.assertEqual(first.get_text(strip=True), section["id"][-1])
                self.assertTrue(rest)
                for entry in rest:
                    self.assertIn("fhi-index-entry", entry["class"])
        ids = {section["id"] for section in sections}
        jumps = {a["href"][1:] for a in soup.select('a[href^="#letter-"]')}
        self.assertTrue(jumps)
        self.assertEqual(jumps - ids, set(), "a jump link has nowhere to land")

    def test_the_glossary_is_one_run(self):
        self._check(reverse("glossary_index"), "glossary-grid")

    def test_the_state_list_is_one_run(self):
        self._check(reverse("state_help_index"), "state-grid")


class TheIndexStylesTest(TestCase):
    def setUp(self):
        self.css = CUSTOM_CSS.read_text()

    def test_an_index_flows_down_columns(self):
        (body,) = _bodies(self.css, ".fhi-index")
        self.assertIn("var(--fhi-index-min", _declared(body, "columns"))

    def test_a_letter_never_ends_a_column(self):
        (body,) = _bodies(self.css, ".fhi-index-letter")
        self.assertEqual(_declared(body, "break-after"), "avoid")

    def test_an_entry_is_never_split_across_columns(self):
        (body,) = _bodies(self.css, ".fhi-index-entry")
        self.assertEqual(_declared(body, "break-inside"), "avoid")

    def test_the_per_letter_tile_grid_is_gone(self):
        self.assertNotIn(".fhi-cards-tiles", self.css)
        for name in ("glossary_index.html", "state_help_index.html"):
            self.assertNotIn("fhi-cards-tiles", (TEMPLATES / name).read_text())


class JumpTilesAreOneRowOnADesktopTest(TestCase):
    def test_the_tiles_share_one_row_however_many_there_are(self):
        body = _bodies(CUSTOM_CSS.read_text(), ".fhi-jump-links")[0]
        self.assertEqual(_declared(body, "grid-auto-flow"), "column")
        self.assertEqual(_declared(body, "grid-auto-columns"), "minmax(0, 1fr)")
        self.assertEqual(_declared(body, "grid-template-columns"), "")

    def test_below_a_desktop_they_wrap_two_to_a_row_again(self):
        narrow = _bodies(CUSTOM_CSS.read_text(), ".fhi-jump-links")[1]
        self.assertEqual(_declared(narrow, "grid-auto-flow"), "row")
        self.assertEqual(
            _declared(narrow, "grid-template-columns"), "repeat(2, minmax(0, 1fr))"
        )

    def test_denial_watch_has_the_seven_tiles_this_was_sized_for(self):
        text = (TEMPLATES / "denial_watch.html").read_text()
        nav = text[text.index('<nav class="fhi-jump-links"') :]
        self.assertEqual(nav[: nav.index("</nav>")].count("<a "), 7)


@override_settings(MCP_SERVER_ENABLED=True)
class SevenAssistantCardsAreFourAndThreeTest(TestCase):
    def test_seven_cards_take_four_columns_on_a_desktop(self):
        style = _style("ai_assistants.html")
        rule = re.search(
            r"@media \(min-width: 1200px\)\s*\{\s*"
            r"#what-it-does \.fhi-cards:has\(> :nth-child\(7\):last-child\)\s*\{([^}]*)\}",
            style,
        )
        self.assertTrue(rule, "no four-column rule for seven cards")
        self.assertEqual(
            _declared(rule.group(1), "grid-template-columns"),
            "repeat(4, minmax(0, 1fr))",
        )

    @override_settings(MCP_PREPARE_APPEAL_ENABLED=True)
    def test_with_start_an_appeal_on_there_are_seven(self):
        soup = BeautifulSoup(self.client.get("/ai-assistants").content, "html.parser")
        cards = soup.select("#what-it-does .fhi-cards > .fhi-card")
        self.assertEqual(len(cards), 7)

    @override_settings(MCP_PREPARE_APPEAL_ENABLED=False)
    def test_with_it_off_six_keep_three_and_three(self):
        soup = BeautifulSoup(self.client.get("/ai-assistants").content, "html.parser")
        cards = soup.select("#what-it-does .fhi-cards > .fhi-card")
        self.assertEqual(len(cards), 6)


class NoCardIsStretchedHollowTest(TestCase):
    def test_resources_groups_flow_down_two_columns_each_card_whole(self):
        css = CUSTOM_CSS.read_text()
        (flow,) = _bodies(css, ".fhi-cards-flow")
        self.assertEqual(_declared(flow, "columns"), "2 26rem")
        (card,) = _bodies(css, ".fhi-cards-flow > *")
        self.assertEqual(_declared(card, "break-inside"), "avoid")

    @override_settings(MCP_SERVER_ENABLED=True)
    def test_every_resources_group_is_a_card_in_the_flow(self):
        soup = BeautifulSoup(
            self.client.get(reverse("other-resources")).content, "html.parser"
        )
        (flow,) = soup.select(".fhi-cards-flow")
        cards = flow.find_all(recursive=False)
        self.assertEqual(
            [c.h2.get_text(strip=True) for c in cards],
            [
                "Government Resources",
                "Your AI Assistant",
                "Condition-Specific Resources",
                "General Resources",
                "Coping Resources",
            ],
        )
        self.assertTrue(all("fhi-card" in c["class"] for c in cards))

    def test_the_how_to_help_cards_are_as_tall_as_what_they_hold(self):
        style = _style("how_to_help.html")
        (body,) = _bodies(style, ".persona-section > .fhi-cards")
        self.assertEqual(_declared(body, "align-items"), "start")

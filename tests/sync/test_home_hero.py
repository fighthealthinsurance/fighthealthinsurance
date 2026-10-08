"""The home hero, as Melanie chose it (2026-10-02): everything on the
photograph in one band. The headline, Generate an Appeal and four ways in,
the trust chips, Why Fight Health Insurance? beside Pay what you want, and
How It Works. Built without Bootstrap's classes, with a focus ring you can
see on the photograph."""

import pathlib
import re

from django.test import SimpleTestCase

ROOT = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
CUSTOM_CSS = (ROOT / "static" / "css" / "custom.css").read_text()
# Comments stripped first, so a rule left inside an unclosed comment cannot
# satisfy these tests while the browser ignores it.
CSS_WITHOUT_COMMENTS = re.sub(r"/\*.*?(?:\*/|$)", "", CUSTOM_CSS, flags=re.DOTALL)
TEMPLATE = (ROOT / "templates" / "landing_base.html").read_text()
HERO = TEMPLATE[
    TEMPLATE.index("{% block hero_section %}") : TEMPLATE.index(
        "{% endblock hero_section %}"
    )
]


def _body(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS_WITHOUT_COMMENTS)
    assert match, f"no rule for {selector}"
    return match.group(1)


class HomeHeroTest(SimpleTestCase):
    def test_the_hero_offers_four_ways_in_beside_generate_an_appeal(self):
        for link_id in (
            "scanlink",
            "explaindeniallink",
            "chatlink",
            "howtohelp",
            "professional",
        ):
            self.assertIn(f'id="{link_id}"', HERO)

    def test_why_pay_and_how_it_works_sit_in_the_hero(self):
        for block in ("benefits-card", "payment-card-wrapper", 'id="how-it-works"'):
            self.assertIn(block, HERO)

    def test_the_hero_uses_no_bootstrap_classes(self):
        classes = " ".join(re.findall(r'class="([^"]*)"', HERO)).split()
        bootstrap = {"btn", "btn-default", "row", "d-flex", "flex-column"}
        self.assertEqual(sorted(bootstrap.intersection(classes)), [])

    def test_a_focused_control_in_the_home_copy_rings_in_white(self):
        # The site's deep-green ring disappears into the photograph's darker
        # colours; white reads on all of them.
        ring = re.search(
            r"outline-color:\s*(#[0-9a-fA-F]{3,6}|white)\s*;",
            _body("#home .hero-inner :focus-visible"),
        ).group(1).lower()
        self.assertIn(ring, ("#fff", "#ffffff", "white"))

    def test_the_trust_chip_link_is_white_when_pressed_too(self):
        selector = re.search(
            r"([^{}]*\.trust-chip-link:hover[^{}]*)\{", CSS_WITHOUT_COMMENTS
        ).group(1)
        self.assertIn(".trust-chip-link:active", selector)

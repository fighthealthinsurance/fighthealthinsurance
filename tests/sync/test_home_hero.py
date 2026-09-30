"""The home hero: the copy on the photograph, the block under it on the same
photograph, and a focus ring you can see there."""

import pathlib
import re

from django.test import SimpleTestCase

CUSTOM_CSS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "css"
    / "custom.css"
).read_text()
# Comments stripped first, so a rule left inside an unclosed comment cannot
# satisfy these tests while the browser ignores it.
CSS_WITHOUT_COMMENTS = re.sub(r"/\*.*?(?:\*/|$)", "", CUSTOM_CSS, flags=re.DOTALL)


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _body(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS_WITHOUT_COMMENTS)
    assert match, f"no rule for {selector}"
    return match.group(1)


class HomeHeroTest(SimpleTestCase):
    def test_the_copy_sits_on_the_photograph(self):
        # Melanie chose the open look (2026-09-30): no panel of any colour
        # behind the headline and the ways in.
        self.assertNotRegex(_body("#home .hero-inner"), r"background(-color)?\s*:")

    def test_the_block_under_the_hero_sits_on_the_same_photograph(self):
        body = _body(".home-why")
        self.assertIn("crinkledcolors-optimized.jpg", body)
        self.assertNotIn("#2b0f3d", body.lower())

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

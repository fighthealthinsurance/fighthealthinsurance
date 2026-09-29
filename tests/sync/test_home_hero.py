"""The home hero: a flat panel under the copy, and a ring you can see on it."""

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
    def test_the_copy_sits_on_an_opaque_panel(self):
        # Any transparency lets the photograph change the colour behind the
        # words, which is what the panel is there to stop.
        panel = re.search(r"background:\s*#([0-9a-fA-F]{6});", _body("#home .hero-inner"))
        self.assertIsNotNone(panel, "the home copy panel is not a solid colour")

    def test_a_focused_control_on_the_panel_rings_in_a_visible_colour(self):
        panel = re.search(
            r"background:\s*#([0-9a-fA-F]{6});", _body("#home .hero-inner")
        ).group(1)
        ring = re.search(
            r"outline-color:\s*#([0-9a-fA-F]{3,6});",
            _body("#home .hero-inner :focus-visible"),
        ).group(1)
        if len(ring) == 3:
            ring = "".join(c * 2 for c in ring)
        self.assertGreaterEqual(_ratio(ring, panel), 3.0)

    def test_the_trust_chip_link_is_white_when_pressed_too(self):
        selector = re.search(
            r"([^{}]*\.trust-chip-link:hover[^{}]*)\{", CSS_WITHOUT_COMMENTS
        ).group(1)
        self.assertIn(".trust-chip-link:active", selector)

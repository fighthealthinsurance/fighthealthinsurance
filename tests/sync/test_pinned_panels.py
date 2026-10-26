"""Two boxes that sat against the left edge of a wider column, read from source.

The home page's Tell Your Friends panel was held to 680px in a 1108px
column whose other sections are all centred, so it was pinned left with an
empty band beside it. On the AI assistants page the set-up screenshots
stopped at 640px while the step text above each one ran the full column.
"""

import pathlib
import re

from django.test import TestCase, override_settings
from django.urls import reverse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CUSTOM_CSS = REPO_ROOT / "fighthealthinsurance" / "static" / "css" / "custom.css"
AI_PAGE = REPO_ROOT / "fighthealthinsurance" / "templates" / "ai_assistants.html"


def _rule(css: str, selector: str) -> str:
    """The body of the rule whose selector is exactly ``selector``."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for found, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if found.strip() == selector:
            return body
    raise AssertionError(f"no rule for {selector}")


def _declared(body: str, prop: str) -> str:
    found = re.findall(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body)
    return found[-1].strip() if found else ""


class TheHomeSharePanelIsCentredTest(TestCase):
    def test_the_panel_is_centred_in_its_column(self):
        body = _rule(CUSTOM_CSS.read_text(), "#share .share-panel")
        self.assertEqual(_declared(body, "margin-inline"), "auto")
        self.assertEqual(_declared(body, "text-align"), "center")

    def test_the_panel_is_one_row_of_buttons_wide_and_never_past_the_column(self):
        body = _rule(CUSTOM_CSS.read_text(), "#share .share-panel")
        self.assertEqual(_declared(body, "width"), "fit-content")
        self.assertEqual(_declared(body, "max-width"), "100%")

    def test_its_buttons_are_centred_under_the_words(self):
        body = _rule(CUSTOM_CSS.read_text(), "#share .share-buttons")
        self.assertEqual(_declared(body, "justify-content"), "center")

    def test_the_rule_reaches_the_panel_on_the_home_page(self):
        html = self.client.get(reverse("root")).content.decode()
        share = html[html.index('id="share"') :]
        self.assertIn('class="share-panel"', share[: share.index("</section>")])


@override_settings(MCP_SERVER_ENABLED=True)
class TheSetUpStepsAreOneColumnTest(TestCase):
    def _style(self) -> str:
        text = AI_PAGE.read_text()
        return text[text.index("<style>") : text.index("</style>")]

    def test_a_list_of_steps_with_screenshots_is_held_to_the_screenshot_column(self):
        body = _rule(self._style(), ".ai-steps:has(figure)")
        self.assertEqual(_declared(body, "max-width"), "var(--ai-shot-column)")

    def test_a_screenshot_in_a_step_fills_the_step(self):
        body = _rule(self._style(), ".ai-steps:has(figure) img")
        self.assertEqual(_declared(body, "max-width"), "100%")

    def test_a_screenshot_on_its_own_takes_the_same_column(self):
        body = _rule(self._style(), ".ai-steps img,\n  .ai-figure img")
        self.assertIn("var(--ai-shot-column)", _declared(body, "max-width"))

    def test_the_column_is_between_640_and_720_pixels(self):
        body = _rule(self._style(), ":root")
        width = _declared(body, "--ai-shot-column")
        self.assertTrue(width.endswith("rem"), width)
        self.assertTrue(640 <= float(width[:-3]) * 16 <= 720, width)

    def test_both_set_up_lists_carry_screenshots(self):
        """The :has() rule reaches the Claude and ChatGPT steps."""
        html = self.client.get("/ai-assistants").content.decode()
        for section in ("claude", "chatgpt"):
            with self.subTest(section=section):
                start = html.index(f'id="{section}"')
                block = html[start : html.index("</section>", start)]
                steps = block[block.index('class="ai-steps"') :]
                self.assertIn("<figure>", steps[: steps.index("</ol>")])

"""Markup that leaned on a stylesheet the page does not have.

Three things drew wrong because a class they used belonged to a stylesheet
that is not on the page. The questions loading page kept its screen-reader
text in .sr-only, which Bootstrap 5 dropped, so the text printed on the
screen, and its spinner was Font Awesome's, which nothing loads, so there was
no spinner. The chat consent form hid the denial text it carries with
Bootstrap's d-none, so the letter would show on the page the day Bootstrap
goes. And 43 icons across six templates named Bootstrap's icon font, which
no page loads, so each one drew nothing.

These hold each fix, and hold the loading page to classes that something on
it defines.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from tests.sync.test_bootstrap_ratchet import (
    ATTRIBUTE,
    OPENING_TAG,
    TEMPLATE_TAGS,
    _classes_in,
    _files,
    _live_markup,
    bootstrap_classes,
    class_names_in_stylesheet,
)
from tests.sync.test_contrast import load_rules

APP = Path(__file__).resolve().parents[2] / "fighthealthinsurance"
TEMPLATES = APP / "templates"
CSS = APP / "static" / "css"
STATIC = APP / "static"
LOGIN = Path(__file__).resolve().parents[2] / "fhi_users" / "templates" / "login.html"

VISUALLY_HIDDEN = "fhi-visually-hidden"
REDUCED_MOTION_OFF = re.compile(
    r"@media\s*\(\s*prefers-reduced-motion\s*:\s*no-preference\s*\)\s*\{"
)


def _custom_css() -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS / "custom.css").read_text(), flags=re.S)


def _blocks_after(pattern: "re.Pattern[str]", css: str) -> "list[tuple[int, int]]":
    """Where each block that opens with `pattern` starts and ends."""
    spans = []
    for found in pattern.finditer(css):
        depth = 1
        end = found.end()
        while end < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            end += 1
        spans.append((found.end(), end))
    return spans


class TheLoadingPageSaysLoadingOnlyToScreenReadersTest(SimpleTestCase):
    """find_next_steps_loading.html, the page between Details and Questions."""

    PAGE = "find_next_steps_loading.html"

    def _page(self) -> BeautifulSoup:
        return BeautifulSoup(
            render_to_string(self.PAGE, {"current_step": 6}), "html.parser"
        )

    def test_the_status_words_are_hidden_with_our_own_class(self):
        status = self._page().select('[role="status"] .%s' % VISUALLY_HIDDEN)
        self.assertEqual(
            [" ".join(tag.get_text().split()) for tag in status],
            ["Loading questions…"],
        )

    def test_the_spinner_is_ours_and_screen_readers_skip_it(self):
        spinners = self._page().select('[role="status"] .fhi-spinner')
        self.assertEqual(len(spinners), 1)
        self.assertEqual(spinners[0].get("aria-hidden"), "true")

    def test_no_class_on_the_page_is_one_nothing_defines(self):
        """Every class the template writes is styled by something it has.

        That is custom.css, main.css, Bootstrap while base.html still loads
        it, or the template's own <style>. A class from none of them, such
        as sr-only or fa-spin, does nothing at all.
        """
        source = (TEMPLATES / self.PAGE).read_text()
        defined = set(bootstrap_classes())
        for name in ("custom.css", "main.css"):
            defined |= class_names_in_stylesheet((CSS / name).read_text())
        for block in re.findall(r"<style[^>]*>(.*?)</style>", source, re.S):
            defined |= class_names_in_stylesheet(block)
        used = set(_classes_in(_live_markup(source)))
        self.assertTrue(used, "no classes found; the reading has broken")
        self.assertEqual(sorted(used - defined), [])


class OurHidingClassesTest(SimpleTestCase):
    def test_the_visually_hidden_class_takes_the_text_out_of_sight(self):
        rules = [
            rule
            for rule in load_rules()
            if rule.stylesheet == "custom.css"
            and "." + VISUALLY_HIDDEN in rule.selectors
        ]
        self.assertEqual(len(rules), 1, rules)
        declared = {prop: value for prop, value, _ in rules[0].declarations}
        self.assertEqual(declared.get("position"), "absolute")
        self.assertEqual(declared.get("width"), "1px")
        self.assertEqual(declared.get("height"), "1px")
        self.assertEqual(declared.get("overflow"), "hidden")
        self.assertEqual(declared.get("white-space"), "nowrap")
        self.assertIn("clip-path", declared)

    def test_it_is_not_a_bootstrap_name(self):
        self.assertNotIn(VISUALLY_HIDDEN, bootstrap_classes())
        self.assertNotIn("fhi-spinner", bootstrap_classes())
        self.assertNotIn("fhi-icon", bootstrap_classes())

    def test_the_hidden_attribute_hides_without_bootstrap(self):
        """The browser's own [hidden] loses to any rule that sets display."""
        rules = [
            rule
            for rule in load_rules()
            if rule.stylesheet == "custom.css" and "[hidden]" in rule.selectors
        ]
        self.assertEqual(len(rules), 1, rules)
        self.assertIn(("display", "none", True), rules[0].declarations)

    def test_the_spinner_turns_only_for_those_who_have_not_asked_for_less(self):
        css = _custom_css()
        allowed = _blocks_after(REDUCED_MOTION_OFF, css)
        animated = [
            found.start()
            for found in re.finditer(r"\.fhi-spinner\b[^{}]*\{[^}]*\banimation", css)
        ]
        self.assertTrue(animated, "the spinner no longer turns for anyone")
        for at in animated:
            self.assertTrue(
                any(start <= at < end for start, end in allowed),
                "an animation on .fhi-spinner sits outside the "
                "prefers-reduced-motion: no-preference block",
            )
        self.assertRegex(css, r"@keyframes\s+fhi-spin\s*\{")


class TheChatConsentFormHidesTheDenialItCarriesTest(TestCase):
    def test_the_denial_text_is_hidden_by_the_attribute(self):
        session = self.client.session
        session["denial_text_for_explanation"] = "They said it was not necessary."
        session.save()

        response = self.client.get(reverse("chat_consent"))

        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.content.decode(), "html.parser")
        carried = page.select('textarea[name="denial_text"]')
        self.assertEqual(len(carried), 1)
        self.assertTrue(carried[0].has_attr("hidden"))
        self.assertNotIn("d-none", carried[0].get("class") or [])
        self.assertEqual(carried[0].get_text(), "They said it was not necessary.")


def _icon_font_tags(text: str):
    """Each opening tag in the markup that names Bootstrap's icon font.

    An inline <svg> is left alone even with a bi- class on it, the way
    about_us.html's envelopes carry one: it draws itself, so the class is
    only a label. Anything else with such a class, an <i> most often, is
    asking for a font glyph that never arrives.
    """
    for tag in OPENING_TAG.findall(TEMPLATE_TAGS.sub(" ", text)):
        element = re.match(r"<([a-zA-Z][\w-]*)", tag).group(1).lower()
        if element == "svg":
            continue
        for attribute, value in ATTRIBUTE.findall(tag):
            if attribute not in ("class", "className"):
                continue
            names = value.strip("\"'{}`").split()
            if any(name == "bi" or name.startswith("bi-") for name in names):
                yield tag


def _icon_font_uses():
    """Every such tag in both apps' templates and the TypeScript, by file."""
    for key, path in _files():
        text = path.read_text(errors="replace")
        if path.suffix == ".html":
            text = _live_markup(text)
        for tag in _icon_font_tags(text):
            yield "%s: %s" % (key, tag)


class NoIconFontIconsTest(SimpleTestCase):
    def test_no_template_or_script_asks_for_an_icon_font_glyph(self):
        found = list(_icon_font_uses())
        self.assertEqual(
            found,
            [],
            "no page loads Bootstrap's icon font. Where the icon says "
            "something, draw it as an inline SVG, like "
            "partials/opens_in_new_tab.html; where the words beside it say "
            "the same thing, leave it out.",
        )

    def test_the_reading_sees_an_icon_when_there_is_one(self):
        """So a clean run means there are none, not that none were read."""
        self.assertEqual(
            list(_icon_font_tags('<p><i class="bi bi-book"></i> Guide</p>')),
            ['<i class="bi bi-book">'],
        )
        self.assertEqual(
            list(_icon_font_tags('<i className="bi bi-x-circle" />')),
            ['<i className="bi bi-x-circle" />'],
        )
        drawn = '<svg class="bi bi-envelope" viewBox="0 0 16 16"><path d=""/></svg>'
        self.assertEqual(list(_icon_font_tags(drawn)), [])
        self.assertEqual(list(_icon_font_tags('<a class="bingo big">x</a>')), [])


class TheLoginPageNamesOnlyFilesThatExistTest(SimpleTestCase):
    """It loaded Bootstrap 3's script, long after the site moved to 5."""

    def test_every_static_file_it_names_is_on_disk(self):
        source = LOGIN.read_text()
        named = re.findall(r"""{%\s*static\s+['"]([^'"]+)['"]\s*%}""", source)
        self.assertTrue(named)
        for reference in named:
            self.assertTrue(
                (STATIC / reference).exists(),
                "login.html names %s, which is not in the static tree" % reference,
            )

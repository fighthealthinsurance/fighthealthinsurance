"""Every page opens the same way.

Before this, twelve content pages had six treatments between them: an h1 at
the hero size, an h2, an h3, centred, centred over a green rule, boxed, and on
the intake page no heading at all. A person landing on About Us saw a small
left-aligned title with the body starting under it; Delete your Data opened
with a huge centred one; FAQ and Contact drew a green rule under theirs.

There are three ways a page opens now, and each page uses exactly one:

  the image hero, for the entry pages, with two heights that are tokens
  the page title block, partials/page_title.html, for every content page
  the form title, inside the form, for a page that is a form

These tests hold the rendered pages to that, so a new page cannot bring a
seventh treatment back, and hold the sizes to the type scale so the block
cannot drift on its own.
"""

import re
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSS = REPO_ROOT / "fighthealthinsurance" / "static" / "css"
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"

# The content pages: each includes the partial and nothing else on the page is
# an h1. The blog index is not here because its title is rendered by React
# from the same markup, which the test client cannot execute.
CONTENT_PAGES = [
    "about",
    "about-ai",
    "other-resources",
    "how-to-help",
    "faq",
    "media-references",
    "contact",
    "privacy_policy",
    "tos",
    "mhmda",
    "remove_data",
]

# The entry pages: the h1 is the hero headline, and the band is a token.
HERO_PAGES = ["explain_denial", "understand_policy"]

H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S)


def _body(html: str) -> str:
    """The page after the shared header, so a heading in the shell (there is
    none today) could not satisfy or fail an assertion about the page."""
    start = html.index("</header>") if "</header>" in html else 0
    return html[start:]


def _page_title_block(html: str) -> str:
    start = html.index('<header class="fhi-page-title">')
    return html[start : html.index("</header>", start)]


class EveryContentPageOpensWithTheTitleBlockTest(TestCase):
    def test_each_content_page_has_exactly_one_heading_and_it_is_the_title_block(
        self,
    ):
        for name in CONTENT_PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                headings = H1.findall(html)
                self.assertEqual(
                    len(headings),
                    1,
                    "%s announces %d names; a page has one" % (name, len(headings)),
                )
                block = _page_title_block(html)
                self.assertIn("<h1>", block, "%s's h1 is not the title block" % name)
                self.assertNotIn(
                    "section-header",
                    _body(html),
                    "%s still draws the old green-rule header" % name,
                )
                self.assertNotIn(
                    "margin-top: 10vh",
                    html,
                    "%s still positions its title by hand" % name,
                )

    def test_a_lede_is_one_paragraph_under_the_title(self):
        html = self.client.get(reverse("other-resources")).content.decode()
        block = _page_title_block(html)
        self.assertEqual(
            block.count('<p class="fhi-page-lede">'),
            1,
            "the lede is not the one paragraph the block allows",
        )
        self.assertNotIn("fhi-page-lede", _page_title_block(
            self.client.get(reverse("about")).content.decode()
        ), "a page with no lede should render none, not an empty one")


class TheIntakePageHasANameTest(TestCase):
    def test_the_form_carries_the_only_heading(self):
        html = self.client.get(reverse("scan")).content.decode()
        headings = H1.findall(html)
        self.assertEqual(len(headings), 1, "the intake page has %d h1s" % len(headings))
        self.assertIn(
            '<h1 class="fhi-form-title">',
            html,
            "the intake page's heading is not the form title",
        )
        self.assertLess(
            html.index('<div class="main-form">'),
            html.index('<h1 class="fhi-form-title">'),
            "the form title sits outside the form",
        )


class TheHeroPagesReadTheirHeightFromATokenTest(TestCase):
    def test_each_hero_page_has_one_heading_inside_its_hero(self):
        for name in HERO_PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertEqual(len(H1.findall(html)), 1)
                hero = html[html.index('class="slider"') :]
                self.assertIn('class="hero-headline"', hero[: hero.index("</section>")])

    def test_the_short_band_is_the_token_and_not_a_number(self):
        for template in ("explain_denial.html", "understand_policy.html"):
            with self.subTest(template=template):
                text = (TEMPLATES / template).read_text()
                self.assertIn("min-height: var(--fhi-hero-floor-short);", text)
                self.assertNotRegex(
                    text,
                    r"min-height:\s*\d",
                    "%s sets its hero height as a number" % template,
                )

    def test_both_hero_floors_are_declared_once_and_read_by_the_band(self):
        custom = (CSS / "custom.css").read_text()
        main = (CSS / "main.css").read_text()
        self.assertEqual(custom.count("--fhi-hero-floor: 650px;"), 1)
        self.assertEqual(custom.count("--fhi-hero-floor-short: 380px;"), 1)
        band = re.search(r"\.slider \.item \{[^}]*\}", main)
        self.assertIsNotNone(band)
        self.assertIn("min-height: var(--fhi-hero-floor);", band.group(0))


class TheTitleBlockReadsTheScaleTest(TestCase):
    def _rule(self, selector: str) -> str:
        custom = (CSS / "custom.css").read_text()
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", custom)
        self.assertIsNotNone(match, "%s has no rule" % selector)
        return match.group(1)

    def test_the_page_title_is_the_page_size_and_the_form_title_the_section_size(
        self,
    ):
        self.assertIn("font-size: var(--fhi-text-page);", self._rule(".fhi-page-title h1"))
        self.assertIn("font-size: var(--fhi-text-lead);", self._rule(".fhi-page-lede"))
        self.assertIn(
            "font-size: var(--fhi-text-section);", self._rule(".fhi-form-title")
        )

    def test_the_block_spaces_itself_from_the_scale_only(self):
        for selector in (".fhi-page-title", ".fhi-page-title h1", ".fhi-page-lede", ".fhi-form-title"):
            with self.subTest(selector=selector):
                rule = self._rule(selector)
                for prop, value in re.findall(r"(margin|padding)[^:]*:\s*([^;]+);", rule):
                    self.assertNotRegex(
                        value,
                        r"\d(px|rem|em)",
                        "%s spaces itself with %s rather than a token" % (selector, value),
                    )

    def test_printing_a_policy_page_keeps_its_title(self):
        """The policy pages print themselves as the downloadable PDF and hide
        the site furniture to do it. Their print rules used to name the bare
        <header> element, which hid nothing until the title block became the
        site's first <header>; then Save as PDF lost the document's title."""
        for name in ("privacy_policy", "mhmda", "tos"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                for block in re.findall(r"@media print\s*\{(.*?)\n\s*\}\n", html, re.S):
                    for selector_list in re.findall(r"([^{}]+)\{", block):
                        for selector in selector_list.split(","):
                            self.assertNotEqual(
                                selector.strip(),
                                "header",
                                "%s hides every <header> when printing, the title with it" % name,
                            )

    def test_the_partial_is_the_only_place_the_block_is_written(self):
        writers = [
            p.relative_to(TEMPLATES).as_posix()
            for p in TEMPLATES.rglob("*.html")
            if '<header class="fhi-page-title">' in p.read_text()
        ]
        self.assertEqual(writers, ["partials/page_title.html"], writers)

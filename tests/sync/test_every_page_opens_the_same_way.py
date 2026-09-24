"""Every page opens the same way.

Before this, twelve content pages had six treatments between them: an h1 at
the hero size, an h2, an h3, centred, centred over a green rule, boxed, and on
the intake page no heading at all. A person landing on About Us saw a small
left-aligned title with the body starting under it; Delete your Data opened
with a huge centred one; FAQ and Contact drew a green rule under theirs.

There are two ways a page opens now, and each page uses exactly one:

  the image hero, for the entry pages, with two heights and one headline
  size that are tokens (home keeps the poster size)
  the page title block, partials/page_title.html, for every other page,
  centred in the narrow column and left-aligned in the wide one

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
    "scan",
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
    def test_the_title_sits_in_the_narrow_column_above_the_form(self):
        """The intake page had no heading at all; it opens with the same block
        as every other page, inside the narrow column so it centres, and
        above the form rather than inside it."""
        html = self.client.get(reverse("scan")).content.decode()
        narrow = html.index('<div class="container-narrow">')
        title = html.index('<header class="fhi-page-title">')
        form = html.index('<div class="main-form">')
        self.assertLess(narrow, title, "the title is outside the narrow column")
        self.assertLess(title, form, "the title sits inside or below the form")
        # The first heading a screen reader meets is the page's, even when a
        # resume-help or pre-fill notice is showing above the form.
        first_heading = re.search(r"<h[1-6]\b", html[narrow:])
        self.assertIsNotNone(first_heading)
        self.assertEqual(
            narrow + first_heading.start(),
            html.index("<h1>", title),
            "a notice's heading comes before the page title",
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
        for template in (
            "explain_denial.html",
            "understand_policy.html",
            "patient_access.html",
            "microsite.html",
        ):
            with self.subTest(template=template):
                text = (TEMPLATES / template).read_text()
                self.assertIn("min-height: var(--fhi-hero-floor-short);", text)
                self.assertNotRegex(
                    text,
                    r"min-height:\s*\d",
                    "%s sets its hero height as a number" % template,
                )

    def test_every_hero_page_but_home_shares_one_headline_size(self):
        custom = (CSS / "custom.css").read_text()
        self.assertEqual(custom.count("--fhi-text-hero-page:"), 1)
        rules = re.findall(r"(?:^|[,\s])\.hero-headline\s*\{([^}]*)\}", custom, re.M)
        self.assertTrue(
            any("font-size: var(--fhi-text-hero-page)" in r for r in rules),
            "no .hero-headline rule reads --fhi-text-hero-page",
        )
        self.assertFalse(
            any("font-size" in r and "--fhi-text-hero-page" not in r for r in rules),
            "a .hero-headline rule sizes the headline by hand",
        )
        for template in TEMPLATES.rglob("*.html"):
            text = template.read_text()
            self.assertNotRegex(
                text,
                r"\.hero-headline\s*\{[^}]*font-size",
                "%s sizes its hero headline itself" % template.name,
            )

    def test_both_hero_floors_are_declared_once_and_read_by_the_band(self):
        custom = (CSS / "custom.css").read_text()
        main = (CSS / "main.css").read_text()
        self.assertEqual(custom.count("--fhi-hero-floor: 650px;"), 1)
        self.assertEqual(custom.count("--fhi-hero-floor-short: 320px;"), 1)
        band = re.search(r"\.slider \.item \{[^}]*\}", main)
        self.assertIsNotNone(band)
        self.assertIn("min-height: var(--fhi-hero-floor);", band.group(0))


class TheTitleBlockReadsTheScaleTest(TestCase):
    def _rule(self, selector: str) -> str:
        custom = (CSS / "custom.css").read_text()
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", custom)
        self.assertIsNotNone(match, "%s has no rule" % selector)
        return match.group(1)

    def test_the_page_title_is_the_page_size(self):
        self.assertIn("font-size: var(--fhi-text-page);", self._rule(".fhi-page-title h1"))
        self.assertIn("font-size: var(--fhi-text-lead);", self._rule(".fhi-page-lede"))

    def test_alignment_follows_the_column(self):
        """Centred in the narrow column, left in the wide one, decided by the
        wrapper and never by the page."""
        custom = (CSS / "custom.css").read_text()
        centred = re.search(
            r"\.container-narrow \.fhi-page-title,\s*\.fhi-page-centred \.fhi-page-title \{([^}]*)\}",
            custom,
        )
        self.assertIsNotNone(centred, "no rule centres the title in the narrow column")
        self.assertIn("text-align: center", centred.group(1))
        self.assertNotIn("text-align", self._rule(".fhi-page-title"))
        self.assertNotIn("text-align", self._rule(".fhi-page-title h1"))
        for template in TEMPLATES.rglob("*.html"):
            text = template.read_text()
            self.assertNotRegex(
                text,
                r"\.fhi-page-title[^{]*\{[^}]*text-align",
                "%s aligns the title itself" % template.name,
            )

    def test_the_block_spaces_itself_from_the_scale_only(self):
        for selector in (".fhi-page-title", ".fhi-page-title h1", ".fhi-page-lede"):
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

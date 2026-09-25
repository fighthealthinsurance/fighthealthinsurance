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
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSS = REPO_ROOT / "fighthealthinsurance" / "static" / "css"
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"

# A page is its URL name, or its name and the arguments it takes. The slugs
# are real entries in the glossary and in static/state_help.json; California
# has every section the state page can render.
GLOSSARY_TERM = ("glossary_term", {"slug": "external-review"})
STATE_PAGE = ("state_help", {"slug": "california"})

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
    "microsite_directory",
    "share_denial",
    GLOSSARY_TERM,
]

# The entry pages: the h1 is the hero headline, and the band is a token.
HERO_PAGES = [
    "explain_denial",
    "understand_policy",
    "state_help_index",
    "glossary_index",
    STATE_PAGE,
]

# Every page whose content sits on a page column and that a plain GET
# reaches: the content pages that have moved, and the hero pages whose
# bands each hold a column. The rest of the delete flow is reached by a
# token or a POST and carries only its title.
ON_A_COLUMN = [
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
    "microsite_directory",
    "share_denial",
    GLOSSARY_TERM,
    "state_help_index",
    STATE_PAGE,
    "glossary_index",
]

# One feed with one headline, so the Resources page renders the feed name's
# heading instead of leaving it out when no feed answers in the test run.
ONE_FEED = {
    "kff": {
        "name": "KFF Health News",
        "description": "Health policy news.",
        "articles": [
            {
                "url": "https://kffhealthnews.org/example/",
                "title": "A headline",
                "formatted_date": "Sep 24, 2026",
            }
        ],
    }
}

H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S)
HEADING = re.compile(r"^h[1-6]$")


def _url(page) -> str:
    if isinstance(page, tuple):
        name, kwargs = page
        return reverse(name, kwargs=kwargs)
    return reverse(page)


def _outline(html: str) -> "list[tuple[int, str]]":
    """The page's own headings in order, as (level, text).

    Read from <main>, not from _body(): the only <header> on a content page
    is the title block, so everything after the first </header> starts
    below the page's h1. The shell around <main> has no headings today, and
    one added there later belongs to the shell's outline, not the page's.
    """
    main = BeautifulSoup(html, "html.parser").find("main")
    assert main is not None, "the page renders no <main>"
    return [
        (int(tag.name[1]), tag.get_text(" ", strip=True))
        for tag in main.find_all(HEADING)
    ]


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
        for page in CONTENT_PAGES:
            name = _url(page)
            with self.subTest(page=name):
                html = self.client.get(name).content.decode()
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


class EveryPageOnAColumnHasAnOutlineTest(TestCase):
    """A heading's level says where it sits in the page, never how big it is.

    The pages used to pick h4 to h6 for their sections because main.css drew
    an h2 as large as the page's name, so someone moving through a page by
    its headings met the title and then jumped three levels. The page column
    sizes each level now, so the outline can be the real one: one h1, the
    sections under it as h2s, and never a step down of more than one level.
    A hero page may open with its tagline above the h1; going up a level is
    never a skip.
    """

    def test_one_h1_then_an_h2_and_no_level_is_skipped(self):
        with patch(
            "fighthealthinsurance.health_news.get_health_news", return_value=ONE_FEED
        ):
            for page in ON_A_COLUMN:
                url = _url(page)
                with self.subTest(page=url):
                    headings = _outline(self.client.get(url).content.decode())
                    ones = [text for level, text in headings if level == 1]
                    self.assertEqual(
                        len(ones), 1, "%s has %d h1s: %s" % (url, len(ones), ones)
                    )
                    at = [level for level, _ in headings].index(1)
                    if at + 1 < len(headings):
                        level, text = headings[at + 1]
                        self.assertEqual(
                            level,
                            2,
                            "%s: h%d %r follows the h1 %r; the first section is an h2"
                            % (url, level, text, ones[0]),
                        )
                    for (above, above_text), (level, text) in zip(
                        headings, headings[1:]
                    ):
                        self.assertLessEqual(
                            level,
                            above + 1,
                            "%s: h%d %r follows h%d %r, skipping a level"
                            % (url, level, text, above, above_text),
                        )

    def test_the_resources_page_renders_the_feed_heading_it_is_checked_with(self):
        """Without a feed the Resources page has no h3 under its news section,
        and the outline test would pass it on the shorter page."""
        with patch(
            "fighthealthinsurance.health_news.get_health_news", return_value=ONE_FEED
        ):
            html = self.client.get(reverse("other-resources")).content.decode()
        self.assertIn((3, "KFF Health News"), _outline(html))


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
        for page in HERO_PAGES:
            name = _url(page)
            with self.subTest(page=name):
                html = self.client.get(name).content.decode()
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

    def test_a_wide_page_lede_fills_its_column(self):
        """The measure belongs to the reading column. On a wide page the lede
        runs the column like the text below it; Resources' one-sentence lede
        wrapped at 65 characters under 1140px before this."""
        self.assertIn("max-width: none;", self._rule(".fhi-page-wide .fhi-page-lede"))

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

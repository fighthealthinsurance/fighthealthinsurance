"""No page ships a jQuery plugin for a job the page or the browser already does.

Three plugins came in with the original theme and loaded on every page.
Stellar.js, a parallax plugin from 2014, moved the background of each element
marked data-stellar-background-ratio as the page scrolled. None of those
elements had a background image (the hero photo sits on .slider .item), so
nothing on screen ever moved. Owl Carousel was started on .owl-carousel, and
no page had one. They cost every visitor a download and a scroll handler for
nothing.

jquery.sticky did real work: it started itself on .navbar-default at the
bottom of its own file and kept the header at the top of the window. The
browser does that now from position: sticky in main.css, with no script and
no wrapper div frozen at the closed height, so it went too.

These keep all three gone, and keep the header sticking without them.
"""

import os
import re
from pathlib import Path

from django.test import SimpleTestCase

from tests.sync.test_contrast import load_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "fighthealthinsurance" / "static"
TEMPLATE_DIRS = [
    REPO_ROOT / app / "templates"
    for app in ("fighthealthinsurance", "fhi_users", "charts")
]
BASE_HTML = REPO_ROOT / "fighthealthinsurance" / "templates" / "base.html"

# Matched against file names and against what a template or stylesheet
# loads, so a renamed or unminified copy (jquery.stellar.js, owl.carousel.js,
# owl.theme.green.css, jquery-sticky.min.js) or one pulled from a CDN still
# counts.
REMOVED_PLUGIN = re.compile(
    r"stellar|owl[.-]carousel|owl\.theme|jquery[.-]sticky", re.IGNORECASE
)

# Calling a plugin once its file is gone throws, and in custom.js that would
# stop the WOW start that follows it.
PLUGIN_CALL = re.compile(r"\.stellar\s*\(|\.owlCarousel\s*\(|\.sticky\s*\(")

# The classes the plugins put on the page themselves. A rule written against
# one of them styles an element nothing creates any more.
PLUGIN_CLASS = re.compile(r"\.(?:owl-[\w-]+|sticky-wrapper|is-sticky)(?![\w-])")

# What a stylesheet pulls in: an @import, bare or through url(), and any other
# url(). Either one naming a removed plugin is a request for a file that is
# gone.
CSS_REFERENCE = re.compile(
    r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)|url\(\s*["']?([^"')\s]+)"""
)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# Built or vendored trees: not ours, not what a page loads by name, and
# node_modules alone is too big to walk on every run.
NOT_OURS = {"node_modules", "dist"}

# A static tag nests single quotes inside a double-quoted attribute, so each
# quoting style is read up to its own closing quote only.
DOUBLE_QUOTED = re.compile(r'(?:src|href)\s*=\s*"([^"]*)"')
SINGLE_QUOTED = re.compile(r"(?:src|href)\s*=\s*'([^']*)'")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def templates():
    for directory in TEMPLATE_DIRS:
        yield from sorted(directory.rglob("*.html"))


def loaded_by(text: str):
    """Every src and href value in a template, quoted either way."""
    return DOUBLE_QUOTED.findall(text) + SINGLE_QUOTED.findall(text)


def our_static_files():
    for directory, subdirectories, files in os.walk(STATIC):
        subdirectories[:] = sorted(d for d in subdirectories if d not in NOT_OURS)
        for name in sorted(files):
            yield Path(directory, name)


def our_stylesheets():
    return [path for path in our_static_files() if path.suffix == ".css"]


class NoPluginLeftWithNothingToDoTest(SimpleTestCase):
    def test_no_template_marks_an_element_for_parallax(self):
        marked = [
            f"{path.relative_to(REPO_ROOT)}: {match.group(0)}"
            for path in templates()
            for match in re.finditer(r'data-stellar[\w-]*(?:="[^"]*")?', read(path))
        ]
        self.assertEqual(
            marked,
            [],
            f"these still carry a parallax attribute that nothing reads: {marked}",
        )

    def test_base_html_loads_none_of_them(self):
        seen = loaded_by(read(BASE_HTML))
        # The reader has to be seeing the shell's scripts at all, or finding
        # no plugin among them proves nothing.
        self.assertTrue(
            any("js/jquery.js" in reference for reference in seen),
            f"read no jquery.js reference out of base.html, only {seen}",
        )
        plugins = [ref for ref in seen if REMOVED_PLUGIN.search(ref)]
        self.assertEqual(plugins, [], f"base.html loads {plugins} again")

    def test_no_other_template_loads_one(self):
        loaded = [
            f"{path.relative_to(REPO_ROOT)}: {reference}"
            for path in templates()
            for reference in loaded_by(read(path))
            if REMOVED_PLUGIN.search(reference)
        ]
        self.assertEqual(loaded, [], f"a template loads a removed plugin: {loaded}")

    def test_nothing_starts_a_plugin_that_is_no_longer_loaded(self):
        scripts = [
            path for path in our_static_files() if path.suffix in {".js", ".ts", ".tsx"}
        ]
        calls = [
            f"{path.relative_to(REPO_ROOT)}: {match.group(0)}"
            for path in scripts + list(templates())
            for match in PLUGIN_CALL.finditer(read(path))
        ]
        # custom.js is where Stellar and Owl were started; if the walk never
        # reaches it, an empty list here means nothing.
        self.assertIn(STATIC / "js" / "custom.js", scripts)
        self.assertEqual(
            calls,
            [],
            f"these call a plugin whose file is gone, and would throw: {calls}",
        )

    def test_no_stylesheet_imports_a_removed_plugin(self):
        """The template checks read src and href only. An @import of
        owl.carousel.css in custom.css would pull the file back in on every
        page, or fail the compress step once the file is gone, and none of
        them would see it."""
        stylesheets = our_stylesheets()
        references = [
            (path, match.group(1) or match.group(2))
            for path in stylesheets
            for match in CSS_REFERENCE.finditer(CSS_COMMENT.sub("", read(path)))
        ]
        # main.css loads its hero images through url(); if the reader finds
        # none of those, finding no plugin proves nothing.
        self.assertTrue(
            any(
                path.name == "main.css" and "images/" in reference
                for path, reference in references
            ),
            f"read no image url() out of main.css, only {references}",
        )
        imported = [
            f"{path.relative_to(REPO_ROOT)}: {reference}"
            for path, reference in references
            if REMOVED_PLUGIN.search(reference)
        ]
        self.assertEqual(
            imported, [], f"a stylesheet pulls in a removed plugin: {imported}"
        )

    def test_no_stylesheet_styles_what_a_removed_plugin_made(self):
        """Owl's dots, the sticky wrapper and its is-sticky class only ever
        existed because a plugin put them on the page. A rule for one of them
        is dead on arrival, and a sign that someone expects the plugin back."""
        stylesheets = our_stylesheets()
        self.assertIn(STATIC / "css" / "main.css", stylesheets)
        self.assertIn(STATIC / "css" / "custom.css", stylesheets)
        styled = [
            f"{path.relative_to(REPO_ROOT)}: {match.group(0)}"
            for path in stylesheets
            for match in PLUGIN_CLASS.finditer(CSS_COMMENT.sub("", read(path)))
        ]
        self.assertEqual(
            styled, [], f"these style what only a removed plugin made: {styled}"
        )

    def test_the_plugin_files_are_gone_from_the_static_tree(self):
        left = [
            str(path.relative_to(STATIC))
            for path in our_static_files()
            if REMOVED_PLUGIN.search(path.name)
        ]
        self.assertEqual(left, [], f"still in fighthealthinsurance/static: {left}")

    def test_the_header_sticks_without_a_script(self):
        """With jquery.sticky gone, these two declarations are the whole of
        what keeps the header at the top of the window. The browser tests
        scroll a page and measure it; this catches the rule going missing
        without a browser. The last rule for the header wins, so that is the
        one read."""
        header = [rule for rule in load_rules() if ".navbar-default" in rule.selectors]
        declared = {}
        for rule in header:
            for prop, value, _ in rule.declarations:
                declared[prop] = value
        self.assertEqual(
            declared.get("position"),
            "sticky",
            f".navbar-default is position: {declared.get('position')}, "
            "so the header scrolls away with the page",
        )
        self.assertIn(
            declared.get("top"),
            {"0", "0px"},
            f".navbar-default has top: {declared.get('top')}; sticky needs "
            "top: 0 to hold the header at the top of the window",
        )

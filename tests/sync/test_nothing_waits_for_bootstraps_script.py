"""Nothing in our templates waits for Bootstrap's script any more.

bootstrap.bundle.min.js came from a CDN at the foot of every page. By the
end all it did for our own markup was open the accordions on four pages:
the FAQ on every microsite and on the Denial Language Library, and the
lists on Preparing for 2026 and Turning 26. A blocked or slow script left
every one of those answers shut, and nothing on the page said why.

Each question is a <details> now, the way the header's dropdowns already
were, and the script tag is gone from base.html. These hold both: no
template or script reaches for Bootstrap's JavaScript, and each of the four
groups is built so the browser opens it by itself, one answer at a time,
with the question still a heading.

Pages from an installed package can render inside base.html as well, and
django-mfa2's do. Two of its templates still carry data-bs attributes.
Neither can do anything today, for the reasons PACKAGE_PAGES_WAITING gives,
and a test fails the day either reason stops holding.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.conf import settings
from django.template import engines
from django.template.utils import get_app_template_dirs
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from fighthealthinsurance.microsites import get_microsite
from tests.sync.test_bootstrap_ratchet import (
    _data_bs_in,
    _files,
    _live_markup,
    _script_data_bs,
)
from tests.sync.test_contrast import load_rules
from tests.sync.test_nothing_leans_on_a_missing_stylesheet import (
    REDUCED_MOTION_OFF,
    _blocks_after,
    _custom_css,
)

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "fighthealthinsurance" / "templates" / "base.html"
SCRIPTS = REPO / "fighthealthinsurance" / "static" / "js"

#: A <script> that loads any file of Bootstrap's: the bundle, the plain
#: build, or one plugin on its own.
BOOTSTRAP_SCRIPT = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*["'][^"']*bootstrap[^"'/]*\.js""", re.I
)

#: Bootstrap's JavaScript called by name: its constructors, or the jQuery
#: methods it adds when it finds jQuery on the page. ".alert(" and ".tab("
#: are left out, because window.alert and many a tab widget share them.
BOOTSTRAP_API = re.compile(
    r"\bbootstrap\s*\.\s*(?:Alert|Button|Carousel|Collapse|Dropdown|Modal"
    r"|Offcanvas|Popover|ScrollSpy|Tab|Toast|Tooltip)\b"
    r"|\.(?:modal|collapse|dropdown|tooltip|popover|offcanvas|toast|scrollspy"
    r"|carousel)\s*\("
)

MICROSITE = "mri-denial"

# Pages from installed packages that render inside our base.html and still
# carry a data-bs attribute, and why each does nothing today. Both are
# django-mfa2's.
PACKAGE_PAGES_WAITING = {
    # The list of two-factor methods. Bootstrap's script opened its Add
    # Method dropdown. Its view stops before the page renders, because
    # settings does not set MFA_UNALLOWED_METHODS.
    "MFA.html": ["data-bs-toggle"],
    # The pop-up its pages include. Only the package's own scripts open
    # it, with Bootstrap's modal(), and they sit in a {% block head %}
    # that base.html does not have, so they never load.
    "modal.html": ["data-bs-dismiss", "data-bs-dismiss"],
}

EXTENDS = re.compile(r"""{%\s*extends\s+["']([^"']+)["']""")
INCLUDE = re.compile(r"""{%\s*include\s+["']([^"']+)["']""")
HEAD_BLOCK = re.compile(r"{%\s*block\s+head\s*%}")

# Each page with questions that open in place: the name its group shares
# and how many questions it holds.
GROUPS = {
    "preparing-2026": ("areas-to-watch", 5),
    "turning-26": ("coverage-options", 5),
    "denial-language-library": ("library-faq", 4),
    "microsite": ("microsite-faq", None),
}


def _our_scripts():
    """Every script file of ours and of the vendored libraries we serve."""
    for path in sorted(SCRIPTS.rglob("*")):
        if not path.is_file() or path.suffix not in (".js", ".ts", ".tsx"):
            continue
        if {"node_modules", "dist"} & set(path.relative_to(SCRIPTS).parts):
            continue
        yield path


def _template_file(name: str) -> "Path | None":
    """The file Django loads for a template name, without compiling it."""
    for loader in engines["django"].engine.template_loaders:
        for origin in loader.get_template_sources(name):
            if Path(origin.name).is_file():
                return Path(origin.name).resolve()
    return None


def _inside_our_base(path: Path) -> bool:
    """Whether a template extends, at any remove, our base.html."""
    seen = set()
    while path not in seen:
        seen.add(path)
        parent = EXTENDS.search(_live_markup(path.read_text(errors="replace")))
        found = _template_file(parent.group(1)) if parent else None
        if found is None:
            return False
        if found == BASE.resolve():
            return True
        path = found
    return False


def _package_data_bs():
    """The data-bs attributes on package templates rendered inside base.html.

    A package template that extends "base.html" gets ours, because Django
    looks in our apps first, and with it a page that loads no Bootstrap
    script. Read are those pages and every template of a package they
    include, by name, where Django would load that name from the package
    rather than from us.
    """
    ours = {path.resolve() for _, path in _files()}
    found = {}
    seen: set = set()

    def read(name: str, path: Path) -> None:
        if path in seen or path in ours:
            return
        seen.add(path)
        text = _live_markup(path.read_text(errors="replace"))
        attributes = list(_data_bs_in(text))
        if attributes:
            found[name] = attributes
        for included in INCLUDE.findall(text):
            target = _template_file(included)
            if target is not None:
                read(included, target)

    for folder in get_app_template_dirs("templates"):
        for path in sorted(Path(folder).rglob("*.html")):
            name = path.relative_to(folder).as_posix()
            if _template_file(name) == path.resolve() and _inside_our_base(path):
                read(name, path.resolve())
    return found


class NothingReachesForBootstrapsScriptTest(TestCase):
    def test_no_template_or_script_carries_a_data_bs_attribute(self):
        """data-bs-* is how markup asks Bootstrap's script to open things.

        Without the script such an attribute does nothing at all, so a
        button carrying one is a button that silently never works.
        """
        found = []
        for key, path in _files():
            if path.suffix == ".html":
                text = _live_markup(path.read_text(errors="replace"))
                found.extend("%s: %s" % (key, name) for name in _data_bs_in(text))
        # Every script, the plain .js ones as well as the TypeScript.
        for path in _our_scripts():
            found.extend(
                "static/js/%s: %s" % (path.relative_to(SCRIPTS), name)
                for name in _script_data_bs(path.read_text(errors="replace"))
            )
        self.assertEqual(
            found,
            [],
            "nothing loads Bootstrap's script. Use a <details>, the way the "
            "header and the FAQs do, a <dialog>, or a few lines of script.",
        )

    def test_base_html_loads_no_bootstrap_script(self):
        source = _live_markup(BASE.read_text())
        self.assertEqual(BOOTSTRAP_SCRIPT.findall(source), [])
        # Its stylesheet is still on the page, which is a separate job.
        self.assertIn("bootstrap@5.2.3/dist/css/bootstrap.min.css", source)

    def test_no_rendered_page_loads_one_either(self):
        """The source check misses a script that arrives by an include."""
        pages = [
            reverse("root"),
            reverse("preparing-2026"),
            reverse("microsite", kwargs={"slug": MICROSITE}),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertEqual(BOOTSTRAP_SCRIPT.findall(html), [])

    def test_no_template_or_script_calls_bootstraps_javascript(self):
        found = []
        for key, path in _files():
            if path.suffix == ".html":
                text = _live_markup(path.read_text(errors="replace"))
                found.extend(
                    "%s: %s" % (key, m.group(0)) for m in BOOTSTRAP_API.finditer(text)
                )
        for path in _our_scripts():
            text = path.read_text(errors="replace")
            found.extend(
                "static/js/%s: %s" % (path.relative_to(SCRIPTS), m.group(0))
                for m in BOOTSTRAP_API.finditer(text)
            )
        self.assertEqual(found, [])

    def test_the_readings_see_what_they_look_for(self):
        """So a clean run means there are none, not that none were read."""
        self.assertEqual(
            len(
                BOOTSTRAP_SCRIPT.findall(
                    '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.2.3'
                    '/dist/js/bootstrap.bundle.min.js"></script>'
                )
            ),
            1,
        )
        self.assertEqual(
            BOOTSTRAP_SCRIPT.findall('<link href="/css/bootstrap.min.css">'), []
        )
        self.assertTrue(BOOTSTRAP_API.search("new bootstrap.Modal(el).show()"))
        self.assertTrue(BOOTSTRAP_API.search("$('#popUpModal').modal('show')"))
        self.assertIsNone(BOOTSTRAP_API.search("window.alert('saved')"))
        self.assertIsNone(BOOTSTRAP_API.search("$('.owl-carousel').owlCarousel({})"))
        self.assertTrue(list(_files()), "no templates or scripts were found")


class PackagePagesInsideOursTest(SimpleTestCase):
    """A page from an installed package can wait on Bootstrap's script too.

    Reading only our own templates missed that django-mfa2's pages render
    inside base.html, and that two of them carry data-bs attributes.
    """

    def test_every_package_page_that_carries_one_is_known(self):
        self.assertEqual(
            _package_data_bs(),
            PACKAGE_PAGES_WAITING,
            "a package's template renders inside base.html, which loads no "
            "Bootstrap script, so a data-bs attribute on it opens nothing. "
            "Give what it opens another way to open, and list it in "
            "PACKAGE_PAGES_WAITING only when it cannot be reached.",
        )

    def test_the_mfa_method_list_still_cannot_render(self):
        """Its Add Method dropdown has nothing to open it."""
        self.assertFalse(
            hasattr(settings, "MFA_UNALLOWED_METHODS"),
            "django-mfa2's list of methods renders now, and its Add Method "
            "dropdown waits on Bootstrap's script, which no page loads. Give "
            "it another way to open before turning two-factor on.",
        )

    def test_the_mfa_scripts_still_do_not_load(self):
        """They open the pop-up with Bootstrap's modal(), which is gone."""
        self.assertIsNone(
            HEAD_BLOCK.search(_live_markup(BASE.read_text())),
            "base.html has a head block, so django-mfa2's scripts load on its "
            "pages now, and they call Bootstrap's modal(), which no page "
            "loads. Give the pop-up another way to open.",
        )


class EachQuestionOpensByItselfTest(TestCase):
    """The four groups that were Bootstrap accordions."""

    def _page(self, url_name: str) -> BeautifulSoup:
        kwargs = {"slug": MICROSITE} if url_name == "microsite" else {}
        response = self.client.get(reverse(url_name, kwargs=kwargs))
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.content.decode(), "html.parser")

    def _expected_count(self, count):
        if count is None:
            return len(get_microsite(MICROSITE).faq)
        return count

    def test_each_question_is_a_details_with_its_heading_in_the_summary(self):
        for url_name, (name, count) in GROUPS.items():
            with self.subTest(page=url_name):
                page = self._page(url_name)
                groups = page.select(".fhi-accordion")
                self.assertEqual(len(groups), 1)
                items = groups[0].find_all(recursive=False)
                self.assertEqual(len(items), self._expected_count(count), items)
                for item in items:
                    self.assertEqual(item.name, "details")
                    self.assertEqual(item.get("class"), ["fhi-accordion-item"])
                    summary = item.find(recursive=False)
                    self.assertEqual(summary.name, "summary", "summary comes first")
                    headings = summary.find_all(["h2", "h3", "h4", "h5", "h6"])
                    self.assertEqual(len(headings), 1, summary)
                    self.assertEqual(headings[0].name, "h3")
                    self.assertTrue(headings[0].get_text(strip=True))
                    self.assertEqual(
                        headings[0].get_text(strip=True),
                        summary.get_text(strip=True),
                        "the summary says the question and nothing else",
                    )
                    answer = item.select(":scope > .fhi-accordion-body")
                    self.assertEqual(len(answer), 1)
                    self.assertTrue(answer[0].get_text(strip=True))

    def test_the_questions_sit_under_their_sections_heading(self):
        """The outline is what it was: an h2 for the section, then the
        questions at h3 beneath it, with nothing in between."""
        for url_name in GROUPS:
            with self.subTest(page=url_name):
                page = self._page(url_name)
                group = page.select_one(".fhi-accordion")
                above = group.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
                self.assertEqual(above[0].name, "h2", above[0])
                self.assertIs(
                    above[0].find_parent("section"), group.find_parent("section")
                )

    def test_a_group_shares_one_name_and_opens_on_its_first_answer(self):
        for url_name, (name, _) in GROUPS.items():
            with self.subTest(page=url_name):
                items = self._page(url_name).select(".fhi-accordion > details")
                self.assertEqual({item.get("name") for item in items}, {name})
                self.assertEqual(
                    [item.has_attr("open") for item in items],
                    [True] + [False] * (len(items) - 1),
                    "the first answer shows, as it did, and only the first",
                )

    def test_no_bootstrap_accordion_is_left_on_the_page(self):
        for url_name in GROUPS:
            with self.subTest(page=url_name):
                page = self._page(url_name)
                left = page.select(
                    ".accordion, .accordion-item, .accordion-button, "
                    ".accordion-collapse, .collapse"
                )
                self.assertEqual(left, [])

    def test_the_one_at_a_time_fallback_runs_after_the_content(self):
        """Browsers before late 2023 ignore name=. The script that closes a
        group's other answers by hand has to run once every group on the
        page exists, so it sits after </main>, and it reads every group by
        its name rather than knowing the header's."""
        html = self.client.get(reverse("preparing-2026")).content.decode()
        fallback = html.index("document.querySelectorAll('details[name]')")
        self.assertGreater(fallback, html.rindex("</main>"))
        self.assertGreater(fallback, html.rindex('name="areas-to-watch"'))
        self.assertIn("other.open = false", html[fallback:])


def _rules(selector: str):
    return [
        rule
        for rule in load_rules()
        if rule.stylesheet == "custom.css" and selector in rule.selectors
    ]


def _declared(selector: str) -> "dict[str, str]":
    declared: "dict[str, str]" = {}
    for rule in _rules(selector):
        declared.update({prop: value for prop, value, _ in rule.declarations})
    return declared


class TheQuestionsLookAndBehaveTest(SimpleTestCase):
    SUMMARY = ".fhi-accordion-item > summary"

    def test_the_whole_row_is_a_target_a_thumb_finds(self):
        declared = _declared(self.SUMMARY)
        height = re.fullmatch(r"(\d+)px", declared.get("min-height", ""))
        self.assertIsNotNone(height, declared)
        self.assertGreaterEqual(int(height.group(1)), 44)
        self.assertEqual(declared.get("cursor"), "pointer")

    def test_the_browsers_triangle_gives_way_to_the_chevron(self):
        self.assertEqual(_declared(self.SUMMARY).get("list-style"), "none")
        self.assertEqual(
            _declared(self.SUMMARY + "::-webkit-details-marker").get("display"),
            "none",
        )

    def test_the_chevron_turns_when_the_answer_opens(self):
        closed = _declared(self.SUMMARY + "::after").get("transform")
        opened = _declared(".fhi-accordion-item[open] > summary::after").get(
            "transform"
        )
        self.assertIn("rotate(", closed or "")
        self.assertIn("rotate(", opened or "")
        self.assertNotEqual(closed, opened)

    def test_a_focused_question_shows_a_ring(self):
        ring = _declared(self.SUMMARY + ":focus-visible")
        self.assertRegex(ring.get("outline", ""), r"^\d+px solid var\(--fhi-")

    def test_nothing_moves_for_anyone_who_asked_for_less_motion(self):
        css = _custom_css()
        allowed = _blocks_after(REDUCED_MOTION_OFF, css)
        moving = [
            found.start()
            for found in re.finditer(
                r"[^{}]*\.fhi-accordion[^{}]*\{[^}]*\b(?:transition|animation)", css
            )
        ]
        self.assertTrue(moving, "the chevron no longer turns for anyone")
        for at in moving:
            self.assertTrue(
                any(start <= at < end for start, end in allowed),
                "a transition or animation on the questions sits outside the "
                "prefers-reduced-motion: no-preference block",
            )

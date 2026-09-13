"""The shared shell every page extends: base.html and the two stylesheets.

These are inverted tests where they can be. Rather than asserting that one
known-bad string is gone, they derive the bad shape from the files as they
are (an overlay is any rule that makes a fixed full-viewport layer; an input
is any class that a template actually puts on an input) and then assert that
nothing in the shell matches it. A regression that nobody remembered to add
to a list still fails the build.
"""

import pathlib
import re
from html.parser import HTMLParser

from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import Client, TestCase
from django.urls import reverse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
TEMPLATES = APP / "templates"
STATIC = APP / "static"
BASE_HTML = TEMPLATES / "base.html"
MAIN_CSS = STATIC / "css" / "main.css"
CUSTOM_CSS = STATIC / "css" / "custom.css"
CUSTOM_JS = STATIC / "js" / "custom.js"

# Elements that never carry a closing tag, so an HTML nesting check must not
# expect one.
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def read(path: pathlib.Path) -> str:
    # main.css is stored with CRLF line endings. read_text normalizes them to
    # "\n", which is what the regexes below expect, and nothing is written
    # back, so the file on disk keeps its own endings.
    return path.read_text(encoding="utf-8", errors="replace")


def css_rules(path: pathlib.Path):
    """Every ``selector -> declarations`` pair in a stylesheet.

    Comments are stripped first. ``@media`` wrappers fall out for free: the
    regex only matches innermost blocks, so a rule nested in one is returned
    with its own selector and the wrapper is skipped. That is what we want --
    a declaration inside a media query still counts.
    """
    text = re.sub(r"/\*.*?\*/", "", read(path), flags=re.DOTALL)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        selector = " ".join(match.group(1).split())
        if selector.startswith("@"):
            continue
        yield selector, match.group(2)


def declared(declarations: str, prop: str):
    """The last declared value of ``prop``, lowercased, or None."""
    found = re.findall(
        rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", declarations, re.IGNORECASE
    )
    return found[-1].strip().lower() if found else None


def to_px(value: str):
    """A CSS length in px, or None if it is not a plain length."""
    match = re.match(r"^([0-9.]+)\s*(px|rem|em)\b", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    return number if match.group(2).lower() == "px" else number * 16.0


def css_rules_with_media(path: pathlib.Path):
    """``(media conditions, selector, declarations)`` for every rule.

    css_rules() above throws the ``@media`` wrapper away, which is fine when
    the question is "does any rule say this" and wrong when the question is
    "does it still say it on a phone". A declaration written for every width
    can be flatly contradicted by the same selector inside a max-width block
    further down the file, and read without the wrapper the two are
    indistinguishable.
    """
    text = re.sub(r"/\*.*?\*/", "", read(path), flags=re.DOTALL)
    found = []

    def walk(chunk: str, media: tuple):
        index = 0
        while True:
            match = re.search(r"([^{}]+)\{", chunk[index:])
            if not match:
                return
            prelude = " ".join(match.group(1).split())
            start = index + match.end()
            depth, end = 1, start
            while end < len(chunk) and depth:
                if chunk[end] == "{":
                    depth += 1
                elif chunk[end] == "}":
                    depth -= 1
                end += 1
            body = chunk[start : end - 1]
            if prelude.startswith("@media"):
                walk(body, media + (prelude,))
            elif not prelude.startswith("@"):
                found.append((media, prelude, body))
            index = end

    walk(text, ())
    return found


def media_matches(conditions, viewport_px: float) -> bool:
    """Whether every wrapper around a rule holds at this viewport width."""
    for condition in conditions:
        if "print" in condition:
            return False
        for bound in re.findall(r"max-width\s*:\s*([0-9.]+)px", condition):
            if viewport_px > float(bound):
                return False
        for bound in re.findall(r"min-width\s*:\s*([0-9.]+)px", condition):
            if viewport_px < float(bound):
                return False
    return True


def effective(path: pathlib.Path, selector: str, viewport_px: float, prop: str):
    """What ``prop`` resolves to for ``selector`` at a given viewport width.

    Every rule written against that exact selector, in source order, with the
    ones whose ``@media`` wrapper does not hold skipped, then the cascade:
    ``!important`` wins, otherwise the last one does. Returns None when no
    rule that applies at this width declares the property at all, which is
    the answer that matters for ``display`` on an element whose own default
    is ``inline``.
    """
    value = None
    value_is_important = False
    for conditions, found, body in css_rules_with_media(path):
        if found != selector or not media_matches(conditions, viewport_px):
            continue
        for raw in re.findall(
            rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body, re.IGNORECASE
        ):
            raw = raw.strip()
            important = bool(re.search(r"!\s*important$", raw, re.IGNORECASE))
            cleaned = re.sub(r"!\s*important$", "", raw, flags=re.IGNORECASE)
            if important or not value_is_important:
                value, value_is_important = cleaned.strip().lower(), important
    return value


# Box types a min-height actually applies to. A non-replaced inline box
# ignores min-height outright, so an anchor left at the default display is
# not a 44px target however tall the rule says it is.
MIN_HEIGHT_APPLIES_TO = frozenset(
    {
        "block",
        "inline-block",
        "flex",
        "inline-flex",
        "grid",
        "inline-grid",
        "table-cell",
    }
)


def nearest_div_before(text: str, index: int) -> str:
    """The last ``<div ...>`` start tag opened before ``index``."""
    start = text.rfind("<div", 0, index)
    if start == -1:
        return ""
    end = text.find(">", start)
    return text[start : len(text) if end == -1 else end + 1]


def shell_templates():
    """``(path, source)`` for every template that extends base.html.

    Only these have the shell's stylesheets on the page, so only these can
    rely on a utility class the shell defines.
    """
    for template in sorted(TEMPLATES.rglob("*.html")):
        text = template.read_text(encoding="utf-8", errors="replace")
        if re.search(r"{%\s*extends\s*['\"]base\.html['\"]\s*%}", text):
            yield template, text


def classes_in(html: str) -> set:
    """Every class name on every element in a rendered document."""
    names = set()
    for attr in re.findall(r'\bclass\s*=\s*"([^"]*)"', html):
        names.update(attr.split())
    for attr in re.findall(r"\bclass\s*=\s*'([^']*)'", html):
        names.update(attr.split())
    return names


def full_viewport_overlay_classes():
    """Class names that CSS alone turns into a fixed full-viewport cover.

    Fixed position, the whole width, the whole height. Such an element hides
    the page underneath it from the moment the stylesheet lands, and nothing
    but script can take it away again, so the shell must not render one.
    """
    overlays = set()
    for path in (MAIN_CSS, CUSTOM_CSS):
        for selector, body in css_rules(path):
            if declared(body, "position") != "fixed":
                continue
            if declared(body, "display") == "none":
                continue
            width = declared(body, "width") or ""
            height = declared(body, "height") or ""
            covers_width = width in {"100%", "100vw"} or declared(body, "inset") == "0"
            covers_height = height in {"100%", "100vh"} or declared(body, "inset") == "0"
            if covers_width and covers_height:
                overlays.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))
    return overlays


def template_ids() -> set:
    """Every id attribute our templates emit, literal ones only."""
    ids = set()
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r'\bid\s*=\s*"([^"{}]*)"', text):
            ids.add(value.strip())
    return ids


def input_classes() -> set:
    """Class names our templates actually put on a text entry field."""
    names = set()
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"<(?:input|textarea|select)\b([^>]*)>", text, re.I):
            attrs = re.search(r'class\s*=\s*"([^"]*)"', match.group(1))
            if attrs:
                names.update(c for c in attrs.group(1).split() if "{" not in c)
    return names


def shell_of(html: str) -> str:
    """A rendered document with the content block cut out.

    base.html wraps ``{% block content %}`` in ``<main id="main-content">``,
    so everything outside that element is the shared shell and everything
    inside it belongs to the page.
    """
    start = html.index("<main")
    end = html.index("</main>") + len("</main>")
    return html[:start] + html[end:]


class UnbalancedTags(HTMLParser):
    """Records close tags that do not match the element currently open."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.unexpected = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.unexpected.append(tag)


class SharedShellMarkupTest(TestCase):
    """base.html has to parse as written: 60 templates extend it."""

    def setUp(self):
        self.client = Client()
        self.home = self.client.get(reverse("root")).content.decode()

    def test_rendered_footer_opens_and_closes_the_same_number_of_divs(self):
        footer = self.home[self.home.index("<footer") : self.home.index("</footer>")]
        # A commented-out column still sits in the footer; it is not markup.
        footer = re.sub(r"<!--.*?-->", "", footer, flags=re.DOTALL)
        self.assertEqual(
            len(re.findall(r"<div[\s>]", footer)),
            len(re.findall(r"</div>", footer)),
            "the footer closes a different number of divs than it opens",
        )

    def test_the_shell_parses_with_no_unexpected_close_tag(self):
        """What base.html itself emits, on every page that extends it.

        Scoped to the shell rather than the whole document on purpose: the
        home page and the upload page each carry one unbalanced tag of their
        own inside the content block (an extra </div> in the home hero and a
        stray </form> on the upload page). Those belong to the PRs that
        rebuild those two pages; this one owns the shell, and an unbalanced
        tag here reparents all 60 templates that extend it.
        """
        for name in ("root", "scan"):
            html = self.client.get(reverse(name)).content.decode()
            parser = UnbalancedTags()
            parser.feed(shell_of(html))
            self.assertEqual(
                parser.unexpected,
                [],
                f"{name}: the shell closes {parser.unexpected} with nothing open",
            )

    def test_no_tooplate_attribution_remains_in_the_shell(self):
        self.assertNotIn("tooplate", BASE_HTML.read_text(encoding="utf-8").lower())
        self.assertNotIn("tooplate", self.home.lower())


class NothingCoversThePageTest(TestCase):
    """The white screen, at the level the browser checks cannot reach.

    Three browser checks stand behind this one (scripting off, the bundle
    404ing, a tracker host stalled rather than refused), and all three pass
    for the same reason: no element the shell renders is turned into a
    full-viewport cover by CSS alone, so there is nothing whose removal a
    script has to be alive to perform.
    """

    def setUp(self):
        self.client = Client()

    def test_the_shell_renders_no_element_that_css_turns_into_a_cover(self):
        overlays = full_viewport_overlay_classes()
        for name in ("root", "scan"):
            html = self.client.get(reverse(name)).content.decode()
            covering = overlays & classes_in(html)
            self.assertEqual(
                covering,
                set(),
                f"{name} renders {covering}, which CSS alone lays over the whole "
                "viewport; only script could ever take it away",
            )

    def test_no_script_hides_a_covering_layer_on_load(self):
        # Moving such a handler to a different event does not help: the page
        # is covered until script runs at all. There must be no handler.
        source = read(CUSTOM_JS)
        for name in full_viewport_overlay_classes() | {"preloader"}:
            self.assertNotIn(
                name,
                source,
                f"custom.js still reaches for .{name}; the page's visible state "
                "must not depend on the bundle arriving",
            )

    def test_the_page_body_is_readable_before_the_webfont_arrives(self):
        # A webfont with no fallback stack means no text at all until the
        # font lands. 'sans-serif' on its own is the browser's default, not
        # a fallback anyone chose.
        body = dict(css_rules(MAIN_CSS))["body"]
        stack = [part.strip() for part in (declared(body, "font-family") or "").split(",")]
        self.assertGreater(
            len(stack), 2, f"body has no real fallback stack behind Poppins: {stack}"
        )


class ShellAssetWeightTest(TestCase):
    """What a patient's browser downloads before it can show anything."""

    def setUp(self):
        self.client = Client()

    def _static_bytes(self, url: str) -> int:
        relative = url.split("/static/", 1)[-1].lstrip("/")
        path = STATIC / relative
        self.assertTrue(path.exists(), f"{relative} is not in the static tree")
        return path.stat().st_size

    def test_favicon_route_serves_an_icon_under_five_kilobytes(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 302)
        size = self._static_bytes(response["Location"])
        self.assertLess(
            size, 5 * 1024, f"/favicon.ico redirects to a {size} byte file"
        )

    def test_navbar_logo_is_small_and_requested_at_its_display_size(self):
        home = self.client.get(reverse("root")).content.decode()
        tag = re.search(r"<img[^>]*class=\"[^\"]*\blogo\b[^\"]*\"[^>]*>", home)
        self.assertIsNotNone(tag, "the navbar logo img is gone")
        markup = tag.group(0)
        source = re.search(r'src="([^"]+)"', markup).group(1)
        size = self._static_bytes(source)
        self.assertLess(size, 20 * 1024, f"the navbar logo is {size} bytes")
        width = int(re.search(r'width="(\d+)"', markup).group(1))
        # .logo renders the mark in a 75px box; twice that covers a 2x screen
        # and anything beyond it is downloaded for nothing.
        box = to_px(declared(dict(css_rules(CUSTOM_CSS))[".logo"], "width"))
        self.assertLessEqual(
            width, box * 2, f"a {width}px image in a {box:.0f}px box"
        )


class ShellTypographyTest(TestCase):
    """The weights that are asked for, and the sizes that trigger a zoom."""

    def test_the_font_comes_from_a_link_in_the_head_not_an_import(self):
        source = BASE_HTML.read_text(encoding="utf-8")
        head = source[source.index("<head") : source.index("</head>")]
        link = re.search(
            r'<link[^>]*href="(https://fonts\.googleapis\.com/[^"]+)"', head, re.DOTALL
        )
        self.assertIsNotNone(link, "the webfont is not requested from the head")
        # An @import cannot begin downloading until the stylesheet holding it
        # has itself arrived and parsed, which is why it moved.
        self.assertNotIn("@import", read(MAIN_CSS))

    def test_the_bold_weight_asked_for_is_the_weight_requested(self):
        requested = set()
        source = BASE_HTML.read_text(encoding="utf-8")
        for href in re.findall(r'href="(https://fonts\.googleapis\.com/[^"]+)"', source):
            family = re.search(r"family=Poppins:([0-9,]+)", href)
            if family:
                requested.update(family.group(1).split(","))
        used = set()
        for path in (MAIN_CSS, CUSTOM_CSS):
            for _selector, body in css_rules(path):
                weight = declared(body, "font-weight")
                if weight and weight.isdigit():
                    used.add(weight)
        self.assertTrue(
            used, "no numeric font-weight in either stylesheet, check the parser"
        )
        self.assertEqual(
            used - requested - {"400"},
            set(),
            "these weights are asked for by a rule and never downloaded, so the "
            "browser synthesizes them",
        )

    def test_no_input_declares_a_font_size_below_one_rem(self):
        # Under 16px, iOS zooms the viewport when the field takes focus and
        # does not zoom back out.
        targets = input_classes()
        known_ids = template_ids()
        offenders = []
        for path in (MAIN_CSS, CUSTOM_CSS):
            for selector, body in css_rules(path):
                size = declared(body, "font-size")
                if size is None:
                    continue
                px = to_px(size)
                if px is None or px >= 16:
                    continue
                for part in selector.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    # A rule hung off an id no template emits is dead code.
                    ids = re.findall(r"#([A-Za-z0-9_-]+)", part)
                    if any(i not in known_ids for i in ids):
                        continue
                    last = part.split()[-1]
                    is_field = re.fullmatch(
                        r"(input|textarea|select)(\[[^\]]*\])?", last, re.IGNORECASE
                    )
                    named = set(re.findall(r"\.([A-Za-z0-9_-]+)", last)) & targets
                    if is_field or named:
                        offenders.append(f"{path.name}: {part} -> {size}")
        self.assertEqual(offenders, [], f"inputs smaller than 1rem: {offenders}")


class ShellTapTargetTest(TestCase):
    """Nine stacked nav rows, one of which is Remove Your Data."""

    def _rule(self, path, selector):
        for found, body in css_rules(path):
            if found == selector:
                return body
        self.fail(f"{selector} is gone from {path.name}")

    def test_nav_rows_are_at_least_44px_tall(self):
        body = self._rule(MAIN_CSS, ".navbar-default .navbar-nav li a")
        self.assertGreaterEqual(to_px(declared(body, "min-height") or "0px"), 44)
        self.assertGreaterEqual(to_px(declared(body, "font-size") or "0px"), 15)

    def test_nav_rows_are_still_44px_on_a_phone_and_on_a_desktop(self):
        """The tap target has to survive the media queries below it.

        min-height is what makes the row 44px, and it only applies to a box
        type that has a height at all: a non-replaced inline box ignores it.
        Two things make that a live question here rather than a pedantic one.
        main.css re-declares this selector inside @media (max-width: 767px)
        with display:inline-block !important, so a display chosen for desktop
        is simply absent on a phone. And li.appointment-btn a, the Generate
        Appeal button, carries no Bootstrap class of its own, so with no
        display from this rule it is a plain inline anchor and the 44px never
        lands on the biggest call to action in the navbar.

        _rule() above cannot see any of this -- it returns the first selector
        match and never the override -- so this one resolves the cascade at a
        phone width and a desktop width instead.
        """
        selector = ".navbar-default .navbar-nav li a"
        for width in (390, 1280):
            min_height = effective(MAIN_CSS, selector, width, "min-height")
            self.assertIsNotNone(
                min_height, f"{width}px: nav links declare no min-height"
            )
            self.assertGreaterEqual(
                to_px(min_height),
                44,
                f"{width}px: nav rows are only {min_height} tall",
            )
            display = effective(MAIN_CSS, selector, width, "display")
            self.assertIn(
                display,
                MIN_HEIGHT_APPLIES_TO,
                f"{width}px: nav links resolve to display:{display}, which a "
                "min-height does not apply to, so the 44px row never lands",
            )

    def test_a_nav_link_is_the_same_kind_of_box_at_every_width(self):
        """So that a fix checked on one screen is true on the other.

        This is the trap the rule above walked into once already. A display
        written into the base rule and then contradicted by
        display:inline-block !important inside @media (max-width: 767px)
        reads, in the file, as though it applied everywhere, while on a
        phone it applies nowhere. Keeping one box type across the
        breakpoints is what makes an eyeball check on a laptop worth
        anything on a phone.
        """
        selector = ".navbar-default .navbar-nav li a"
        phone = effective(MAIN_CSS, selector, 390, "display")
        desktop = effective(MAIN_CSS, selector, 1280, "display")
        self.assertEqual(
            phone,
            desktop,
            f"nav links are display:{desktop} on a desktop and display:{phone} "
            "on a phone, so whichever one was reasoned about is wrong on the "
            "other",
        )

    def test_footer_rows_are_at_least_44px_tall(self):
        body = self._rule(MAIN_CSS, ".footer-link a")
        self.assertGreaterEqual(to_px(declared(body, "min-height") or "0px"), 44)
        self.assertGreaterEqual(to_px(declared(body, "font-size") or "0px"), 14)


class NothingClippedOutOfReachTest(TestCase):
    """Wide things should scroll, not be cut off at the viewport edge."""

    def test_no_stylesheet_clips_the_body_sideways(self):
        # overflow-x:hidden on body does not make the page fit. It makes the
        # part that does not fit unreachable, and hides the bug in dev.
        for path in (MAIN_CSS, CUSTOM_CSS):
            for selector, decls in css_rules(path):
                if "body" not in selector.split():
                    continue
                self.assertNotEqual(
                    declared(decls, "overflow-x"),
                    "hidden",
                    f"{path.name} clips the body sideways at {selector}",
                )

    def test_a_horizontal_scroll_utility_exists(self):
        body = dict(css_rules(MAIN_CSS)).get(".scroll-x")
        self.assertIsNotNone(body, ".scroll-x is the replacement for the clip")
        self.assertEqual(declared(body, "overflow-x"), "auto")

    def test_the_wide_tables_sit_inside_a_horizontal_scroller(self):
        for name, table_class in (
            ("categorize.html", "post-infered-table"),
            ("outside_help.html", "outside-help-options-table"),
        ):
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            index = text.index(table_class)
            before = text[:index]
            self.assertIn(
                "scroll-x",
                before[before.rindex("<div") :] if "<div" in before else "",
                f"{name}: {table_class} is not wrapped in a .scroll-x",
            )

    def test_every_form_table_on_a_shell_page_scrolls_sideways(self):
        """Django's as_table is the shape most likely to run past the edge.

        as_table has no width cap of its own: the table is as wide as its
        widest label and field, and on a 390px screen several of ours are
        wider than that. body{overflow-x:hidden} used to hide the problem by
        making the overflow unreachable rather than absent, so taking the
        clip out is what makes each of these need a .scroll-x of its own.

        Derived from the templates rather than listed, so the next page that
        renders a form this way is covered without anyone remembering to add
        it here. Scoped to templates that extend base.html, because .scroll-x
        is defined in the shell's stylesheet and a fragment rendered without
        the shell would not have it.
        """
        unwrapped = []
        for template, source in shell_templates():
            for match in re.finditer(r"{{\s*[\w.]+\.as_table\s*}}", source):
                table = source.rfind("<table", 0, match.start())
                if table == -1:
                    continue
                if "scroll-x" not in nearest_div_before(source, table):
                    line = source.count("\n", 0, table) + 1
                    unwrapped.append(f"{template.name}:{line}")
        self.assertEqual(
            unwrapped,
            [],
            f"form tables with no horizontal scroller around them: {unwrapped}",
        )

    def test_container_narrow_caps_its_width_instead_of_giving_a_fifth_away(self):
        body = dict(css_rules(CUSTOM_CSS))[".container-narrow"]
        self.assertEqual(
            declared(body, "width"),
            "100%",
            "width:80% throws away a fifth of a 390px screen",
        )
        self.assertIsNotNone(
            to_px(declared(body, "max-width") or ""),
            ".container-narrow needs a real max-width for the desktop line length",
        )

    def test_stacked_form_rows_fill_the_column_on_a_phone(self):
        # Centre alignment is why the name, street and zip inputs sat at
        # label width instead of filling the column.
        rules = [
            (selector, decls)
            for selector, decls in css_rules(CUSTOM_CSS)
            if selector in (".together-form-group", ".together-form-group > div")
        ]
        group = [d for s, d in rules if s == ".together-form-group"][-1]
        children = [d for s, d in rules if s == ".together-form-group > div"][-1]
        self.assertEqual(declared(group, "align-items"), "stretch")
        self.assertEqual(declared(children, "width"), "100%")

    def test_the_tiktok_mark_is_square(self):
        text = (TEMPLATES / "how_to_help.html").read_text(encoding="utf-8")
        tag = re.search(r'<img[^>]*alt="TikTok"[^>]*>', text).group(0)
        style = re.search(r'style="([^"]*)"', tag).group(1)
        width = to_px(re.search(r"width:\s*([^;]+)", style).group(1))
        height = to_px(re.search(r"height:\s*([^;]+)", style).group(1))
        self.assertEqual(
            width,
            height,
            "a 280px mark in a 28px row is what opens a sideways scroll here",
        )


class ShellStaticFilesExistTest(TestCase):
    """Every static file base.html names has to be on disk."""

    def test_the_shell_references_only_files_that_exist(self):
        source = BASE_HTML.read_text(encoding="utf-8")
        for reference in re.findall(r"{%\s*static\s*'([^']+)'\s*%}", source):
            relative = reference.lstrip("/")
            self.assertTrue(
                (STATIC / relative).exists(),
                f"base.html names {reference}, which is not in the static tree",
            )
            self.assertTrue(staticfiles_storage.url(relative))

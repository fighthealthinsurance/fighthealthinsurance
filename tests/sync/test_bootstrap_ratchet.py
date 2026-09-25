"""Bootstrap can only shrink from here.

The site is built on Bootstrap 5 and the owner wants it gone, which is a long
job: on 2026-09-24 there were 2,994 uses of 163 of its classes across 69
templates and 5 scripts, 483 of them the grid alone, and its script opened
the accordions on four pages. A long job needs a mechanism rather than a
resolution, so this is the mechanism. It counts what is there today and
fails when a count goes up.

It counts all of it. Every class name in Bootstrap 5.2.3's own stylesheet is
listed in bootstrap_5_2_3_classes.txt next to this file, and each one belongs
to a family: the grid, spacing, display and flex, text, and each component
on its own, so a failure says which part of Bootstrap a page reached for and
what replaces it. The count covers the templates of both apps, the class
names the TypeScript writes into the page, and the data-bs-* attributes
Bootstrap's script reads to open and close things.

Each file is held to each name it uses, in bootstrap_uses_by_file.txt next
to this file, so a page cannot trade one Bootstrap class for another, d-grid
for d-none, while its family's total stands still. The families add those
up into the site's totals below.

Nothing here asks anyone to remove Bootstrap. It asks that a page being worked
on does not reach for more of it, and that when a batch of uses goes, the
number recorded here comes down with it so the headroom cannot grow back.
Same shape as the spacing ratchet next door, for the same reason.

The first component owned outright is the input box: ``.fhi-field`` and
``.fhi-check`` in custom.css, stamped onto Django-rendered forms by
``StyledWidgetsMixin`` and written by hand in the flow templates. Forms were
picked first because the site uses a thin slice of Bootstrap's form layer, so
it is about twenty lines of CSS against four hundred uses of the grid.
"""

import functools
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"
USER_TEMPLATES = REPO_ROOT / "fhi_users" / "templates"
SCRIPTS = REPO_ROOT / "fighthealthinsurance" / "static" / "js"
CLASS_LIST = Path(__file__).resolve().with_name("bootstrap_5_2_3_classes.txt")

CLASS_LIST_SOURCE = (
    "https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/css/bootstrap.css"
)
CLASS_LIST_HEADER = (
    "# Every class name Bootstrap 5.2.3 styles, one per line, sorted: the class\n"
    "# selectors of its stylesheet, the version base.html loads. Read by\n"
    "# test_bootstrap_ratchet.py.\n"
    "# Our own classes that only look like Bootstrap's, such as btn-default and\n"
    "# no-gutters in custom.css, are not in it, because 5.2.3 does not have them.\n"
    "# To regenerate it, from the top of the repository:\n"
    "#   curl -sL %s \\\n"
    "#     | python tests/sync/test_bootstrap_ratchet.py \\\n"
    "#     > tests/sync/bootstrap_5_2_3_classes.txt\n" % CLASS_LIST_SOURCE
)

# The family each Bootstrap class belongs to, by its name. Every class in the
# list belongs to exactly one; a test holds that, so a regenerated list cannot
# leave a class uncounted or count one twice. The family names have
# spaces or are plurals Bootstrap does not use, so none of them is a class.
FAMILIES = (
    # Components, one family each.
    ("accordions", r"accordion.*"),
    ("alerts", r"alert(-.*)?"),
    ("badges", r"badge"),
    ("breadcrumbs", r"breadcrumb.*"),
    ("buttons", r"btn(-.*)?"),
    ("cards", r"card(-.*)?"),
    ("carousels", r"carousel.*|pointer-event"),
    ("collapse panels", r"collapse.*|collapsing|collapsed"),
    ("dropdowns", r"dropdown.*|dropup.*|dropend|dropstart"),
    (
        "forms",
        r"form-.*|col-form-label.*|input-group.*|is-(in)?valid|was-validated"
        r"|(in)?valid-(feedback|tooltip)|has-validation",
    ),
    ("images and figures", r"img-.*|figure.*"),
    ("list groups", r"list-group.*"),
    ("modals", r"modal.*"),
    ("navs and the navbar", r"nav|nav-.*|navbar.*|tab-content|tab-pane"),
    ("offcanvas panels", r"offcanvas.*"),
    ("page links", r"pagination.*|page-(item|link)"),
    ("placeholders", r"placeholder.*"),
    ("popovers", r"popover.*|bs-popover-.*"),
    ("progress bars", r"progress.*"),
    ("spinners", r"spinner-.*"),
    ("tables", r"table.*|caption-top"),
    ("toasts", r"toast.*"),
    ("tooltips", r"tooltip.*|bs-tooltip-.*"),
    # The state classes several components share, and read together with them.
    ("shared state", r"active|show|showing|hiding|fade|disabled"),
    # Layout and the utilities.
    (
        "grid",
        r"container.*|row|row-cols-.*|col|col-(?!form-label).*|offset-.*|g[xy]?-.*",
    ),
    (
        "spacing",
        r"m[tbsexy]?-((sm|md|lg|xl|xxl)-)?(auto|\d)"
        r"|p[tbsexy]?-((sm|md|lg|xl|xxl)-)?\d|gap-.*",
    ),
    (
        "display and flex",
        r"d-.*|flex-.*|justify-content-.*|align-(items|content|self)-.*|order-.*"
        r"|[hv]stack",
    ),
    (
        "text",
        r"text-(?!bg-).*|fw-.*|fst-.*|fs-.*|lh-.*|font-monospace|lead|small|mark"
        r"|initialism|display-\d|h[1-6]|list-(unstyled|inline.*)|blockquote.*"
        r"|align-(baseline|top|middle|bottom|text-top|text-bottom)",
    ),
    ("colours and backgrounds", r"bg-.*|text-bg-.*|link-.*"),
    ("borders and shadows", r"border.*|rounded.*|shadow.*"),
    ("sizing", r"[wh]-.*|m[wh]-.*|v[wh]-.*|min-v[wh]-.*|ratio.*"),
    (
        "position, float and overflow",
        r"position-.*|(top|bottom|start|end)-.*|translate-middle.*"
        r"|fixed-(top|bottom)|sticky-.*|float-.*|clearfix|overflow-.*",
    ),
    (
        "visibility and interaction",
        r"visible|invisible|visually-hidden.*|opacity-.*|pe-(none|auto)"
        r"|user-select-.*|stretched-link|vr",
    ),
)
_FAMILY_PATTERNS = tuple((family, re.compile(p)) for family, p in FAMILIES)

# The nine class names the first version of this ratchet watched, still
# given site totals of their own beside their family's, as that version
# gave them.
# "col-" is every Bootstrap class that starts with it. A name of ours that
# starts the same way is not Bootstrap's, and neither is Bootstrap 3's
# col-md-offset-1, which 5.2.3 does not have and nothing styles.
WATCHED = (
    "form-control",
    "form-check-input",
    "card",
    "btn",
    "row",
    "col-",
    "container",
    "alert",
    "d-flex",
)

#: Attributes Bootstrap's script reads, counted together under this name.
DATA_BS = "data-bs attributes"

# Recounted 2026-09-24, when this widened from nine class names in one app's
# templates to all of Bootstrap in both apps' templates and the TypeScript.
# The nine carried over from the counts of 2026-09-18 and 2026-09-19, which
# the wider reading moved in three ways: login.html in fhi_users is counted,
# so are the scripts, and the pages in NO_BOOTSTRAP_HERE are not, which took
# the seventeen ".stat-card.alert" uses in admin_status.html out of "alert"
# (they are "stat-alert" now).
# Lower these as uses go. A family or class not listed is at zero and stays
# there. The only thing that raises one is a page arriving that was written
# before this existed, and then by exactly what that page brings, with the
# page's own lines in bootstrap_uses_by_file.txt, so no page can grow under
# cover of a total. 2026-09-19: the two glossary pages, written before the
# ratchet landed.
# 2026-09-24: the accordions on four pages became <details>, and with them
# went every accordion and collapse class, the "show" on each first answer,
# and all 45 data-bs attributes. Bootstrap's script left base.html in the
# same change, so none of those can come back and do anything.
# 2026-09-25: "col-" stopped counting names that only start like Bootstrap's,
# which took three col-md-offset-1 out of it, one each on share_denial,
# stripe_finish_error and unsubscribed.
# 2026-09-25: each file is held to each class, in bootstrap_uses_by_file.txt,
# in place of a count per family per file.
BASELINE: "dict[str, int]" = {
    "form-control": 6,
    "form-check-input": 5,
    "card": 105,
    "btn": 136,
    "row": 141,
    "col-": 200,
    "container": 120,
    "alert": 20,
    "d-flex": 53,
    # The families.
    "alerts": 60,
    "badges": 18,
    "borders and shadows": 41,
    "breadcrumbs": 7,
    "buttons": 192,
    "cards": 411,
    "colours and backgrounds": 64,
    "display and flex": 177,
    "forms": 107,
    "grid": 483,
    "images and figures": 49,
    "list groups": 23,
    "navs and the navbar": 21,
    "shared state": 3,
    "sizing": 83,
    "spacing": 709,
    "spinners": 3,
    "text": 385,
    "visibility and interaction": 8,
}

# Where each one should end up instead, for whoever reads a failure.
INSTEAD = {
    "form-control": "fhi-field",
    "form-check-input": "fhi-check",
    "card": "a component class of ours, built from the tokens",
    "btn": "the button tokens #1025 shipped",
    "row": "flexbox or grid with gap",
    "col-": "flexbox or grid with gap",
    "container": "one shared content wrapper",
    "alert": "a component class of ours",
    "d-flex": "a component class of ours, or plain CSS on the element",
    # The families.
    "accordions": "a native <details>, the way the header opens its groups",
    "alerts": "a component class of ours",
    "badges": "a component class of ours",
    "breadcrumbs": "a plain list with a class of ours",
    "buttons": "the button tokens #1025 shipped",
    "cards": "a component class of ours, built from the tokens",
    "carousels": "a component class of ours",
    "collapse panels": "a native <details>",
    "dropdowns": "a native <details>, the way the header opens its groups",
    "forms": "fhi-field and fhi-check",
    "images and figures": "max-width on a class of ours",
    "list groups": "a plain list with a class of ours",
    "modals": "a native <dialog>",
    "navs and the navbar": "the header's own fhi-nav classes",
    "offcanvas panels": "a native <dialog>",
    "page links": "a plain list of links with a class of ours",
    "placeholders": "a component class of ours",
    "popovers": "a component class of ours",
    "progress bars": "a native <progress>",
    "spinners": "a component class of ours",
    "tables": "a table class of ours",
    "toasts": "a component class of ours",
    "tooltips": "a title attribute, or a component class of ours",
    "shared state": "whatever replaces the component that reads it",
    "grid": "flexbox or grid with gap, or the fhi-page columns",
    "spacing": "margin, padding or gap from the --fhi-space-* scale",
    "display and flex": "a component class of ours, or plain CSS on the element",
    "text": "the --fhi-text-* sizes and --fhi-muted, on a class of ours",
    "colours and backgrounds": "the colour tokens, such as --fhi-surface",
    "borders and shadows": "--fhi-line, --fhi-radius and --fhi-shadow",
    "sizing": "width, height or aspect-ratio on a class of ours",
    "position, float and overflow": "plain CSS on a class of ours",
    "visibility and interaction": "plain CSS on a class of ours",
    DATA_BS: "a native <details> or <dialog>, or a few lines of our own script",
}


# Pages that load no Bootstrap at all: no stylesheet, no script, nothing of
# ours extended, and included by nothing that loads it. A class on them that
# shares a name with Bootstrap's is theirs, styled by their own <style>
# block, so it is not a use and is not counted. A test holds each of them to
# that, so a page that starts loading Bootstrap has to come off this list.
#
# The staff pages that used to be here, admin_status, admin_model_query and
# the two Pro Connector pages, gave their own badge, alert and card classes
# names of their own on 2026-09-24, so nothing on them shares a name with
# Bootstrap's any more. They are counted like any other page, at zero, and
# a Bootstrap class put on one of them fails.
NO_BOOTSTRAP_HERE = (
    # Django's admin, which brings its own stylesheet. Its "active" and
    # "tab-content" are the admin's.
    "admin/fighthealthinsurance/ongoingchat/chat_editor.html",
    # A standalone page styled inline. Its container, mt-4 and mb-5 style
    # nothing, because nothing there loads Bootstrap.
    "brb.html",
)

# Class names of ours that share a name with Bootstrap's, where it matters.
# Bootstrap's ".active" only does anything beside one of its own components,
# and ".visible" on our error message is our own toggle. A test fails when an
# entry here stops being used, so the list cannot hide anything by going
# stale.
OUR_OWN_NAMES = {
    # .flow-progress-step.active, defined in the partial's own <style>.
    "partials/flow_progress.html": ("active",),
    # .hidden-error-message.visible, defined in custom.css.
    "static/js/scrub_client_side_form.ts": ("visible",),
}

# What each file carries, one class or data-bs attribute to a line.
USES_BY_FILE = Path(__file__).resolve().with_name("bootstrap_uses_by_file.txt")
USES_BY_FILE_HEADER = (
    "# Each Bootstrap class and data-bs attribute each file uses, and how\n"
    "# many times: the file, the name and the count, one to a line, sorted.\n"
    "# Read by test_bootstrap_ratchet.py, which fails when a use grows and\n"
    "# when a number here is above what the file really has. A template is\n"
    "# named by its path under fighthealthinsurance/templates, one of\n"
    "# fhi_users' by fhi_users/ and its path, and a script by static/js/ and\n"
    "# its name. A name a file does not list is at zero there.\n"
    "# After taking uses out, bring these down, from the top of the repository:\n"
    "#   python tests/sync/test_bootstrap_ratchet.py --lower\n"
    "# That only lowers a number or drops a line. A use that has to grow is\n"
    "# raised here by hand, in the same commit, which says why.\n"
)


@functools.lru_cache(maxsize=None)
def bootstrap_classes() -> "frozenset[str]":
    """The class names in the list next to this file."""
    return frozenset(
        line
        for line in CLASS_LIST.read_text().splitlines()
        if line and not line.startswith("#")
    )


@functools.lru_cache(maxsize=None)
def family_of(name: str) -> "str | None":
    """The family a Bootstrap class belongs to, or None for any other name."""
    if name not in bootstrap_classes():
        return None
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.fullmatch(name):
            return family
    return None


def _keys_of(name: str) -> "list[str]":
    """The counts one use adds to: its family, and its own if it is watched."""
    if name.startswith("data-bs-"):
        return [DATA_BS]
    keys = []
    family = family_of(name)
    if family:
        keys.append(family)
    watched = "col-" if family and name.startswith("col-") else name
    if watched in WATCHED:
        keys.append(watched)
    return keys


def class_names_in_stylesheet(css: str) -> "set[str]":
    """Every class a stylesheet's selectors name.

    Reads the text before each "{" that does not start an at-rule, so a
    class-like word inside a declaration's value, such as a url(), is not
    taken for one, and a "{" inside a quoted string does not start a rule.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    names = set()
    prelude = ""
    for piece in re.findall(
        r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[{};]|[^{};"']+""", css
    ):
        if piece == "{":
            if not prelude.lstrip().startswith("@"):
                selector = re.sub(r"\[[^\]]*\]", "", prelude)
                names.update(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", selector))
            prelude = ""
        elif piece in "};":
            prelude = ""
        else:
            prelude += piece
    return names


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def _scripts():
    """The TypeScript sources, not the packages or the bundles built from them."""
    return sorted(
        path
        for path in SCRIPTS.rglob("*.ts*")
        if path.suffix in (".ts", ".tsx")
        and not {"node_modules", "dist"} & set(path.relative_to(SCRIPTS).parts)
    )


def _files():
    """Every file counted, under the name the baselines use for it."""
    for path in _templates():
        yield str(path.relative_to(TEMPLATES)), path
    for path in sorted(USER_TEMPLATES.rglob("*.html")):
        yield "fhi_users/%s" % path.relative_to(USER_TEMPLATES), path
    for path in _scripts():
        yield "static/js/%s" % path.relative_to(SCRIPTS), path


#: A class attribute, anchored on the whitespace that must precede any
#: attribute name. Matching "class=" wherever it appears also matched the
#: tail of another attribute, so "x.class=" counted as one.
#: An opening tag, and inside it one attribute. Read as two steps rather
#: than one, because a `class="..."` inside another attribute's value, such
#: as <div data-label='use class="btn"'>, is not a class the browser puts on
#: anything, and a single pattern over the whole file counts it as one.
#:
#: The tag pattern skips over quoted values rather than stopping at the
#: first ">", because a ">" inside an earlier attribute would otherwise end
#: the tag there and hide every attribute after it, the class included.
OPENING_TAG = re.compile(r"""<[a-zA-Z][^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>""")
#: The element's name at the front of a tag, taken off before the
#: attributes are read so that it is not read as one of them.
TAG_NAME = re.compile(r"^<[a-zA-Z][-\w:.]*")
#: One attribute, with its value or without one. <button data-bs-toggle>
#: carries the attribute as surely as <button data-bs-toggle="collapse">
#: does, and a pattern that insisted on "=" never saw it. The value comes
#: back empty when there is none.
ATTRIBUTE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))?"""
)


#: Markup that never reaches a browser. Django's comment tag takes an
#: optional note, {% comment "why this is here" %}, and a pattern that
#: insisted on nothing between the word and the closing brace left those
#: blocks counted.
#: ``{% ... %}`` and ``{{ ... }}``, replaced by a space rather than removed
#: so two names either side of one do not become a single word.
TEMPLATE_TAGS = re.compile(r"{%.*?%}|{{.*?}}", re.S)

COMMENTS = re.compile(
    r"<!--.*?-->|{#.*?#}|{%\s*comment\b[^%]*%}.*?{%\s*endcomment\s*%}", re.S
)


def _live_markup(text: str) -> str:
    """The template with its commented-out markup removed.

    A commented-out button counted as a use, which is wrong in both
    directions: it inflated the baseline, it blocked tidying the comment
    away, and uncommenting it changed no count at all.
    """
    return COMMENTS.sub("", text)


def _attributes_in(text: str):
    """Every attribute the markup puts on an element, as (name, value).

    Template tags are taken out first, each replaced by a space, so an
    attribute behind a condition is one the page can render.
    """
    for tag in OPENING_TAG.findall(TEMPLATE_TAGS.sub(" ", text)):
        yield from ATTRIBUTE.findall(TAG_NAME.sub("", tag))


def _classes_in(text: str):
    """Every class name the markup actually puts on an element.

    Reading class attributes rather than the file's raw text, because the
    raw text also matches a selector in a page's own stylesheet, a class
    named inside a comment, and a word in prose. Counting those made the
    numbers wrong in both directions: three of the form-control matches were
    CSS selectors, and a commented-out button counted as a live one.

    Template tags are taken out first, each replaced by a space. A class
    behind a condition is a class the page can render, so
    ``class="{% if x %}form-control{% endif %}"`` has to count as one; left
    in, the tag's own words were counted instead and the class was not, and
    a quote inside the tag ended the attribute early and lost the rest of
    it.
    """
    for attribute, value in _attributes_in(text):
        if attribute.lower() != "class":
            continue
        for name in value.strip("\"'").split():
            yield name


def _data_bs_in(text: str):
    """The data-bs-* attributes the markup puts on elements."""
    for attribute, _ in _attributes_in(text):
        if attribute.lower().startswith("data-bs-"):
            yield attribute.lower()


#: A string literal in a script: double quoted, single quoted, or a template.
SCRIPT_STRING = re.compile(
    r""""(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`"""
)
#: A comment in a script, found alongside the strings so that "//" inside a
#: string, a URL say, is not taken for one.
SCRIPT_COMMENT = re.compile(r"(%s)|//[^\n]*|/\*.*?\*/" % SCRIPT_STRING.pattern, re.S)

#: Where a script names classes. After a JSX className= comes one value, a
#: string or a {...} group. After the DOM's className, a className key, or
#: classList and setAttribute("class", ...), the value runs to the end of
#: the expression. Only the string literals in the value are read, so an
#: error message that says "container" or "not active" is not a use.
CLASS_VALUE = re.compile(r"(?<![\w.$])className\s*=(?!=)\s*")
CLASS_EXPRESSION = re.compile(
    r"\.className\s*\+?=(?!=)\s*"
    r"|(?<![\w.$])className\s*:\s*"
    r"|\bclassList\s*\.\s*(?:add|remove|toggle|contains|replace)\s*(?=\()"
    r"""|\bsetAttribute\s*(?=\(\s*["']class["'])"""
)
#: A data-bs-* attribute written with its value into a script's markup,
#: JSX or a string.
SCRIPT_DATA_BS = re.compile(r"(?<![\w-])data-bs-[\w-]+(?=\s*=)")
#: One set by name: el.setAttribute("data-bs-toggle", ...), toggleAttribute,
#: or jQuery's $(el).attr(...).
SCRIPT_DATA_BS_BY_NAME = re.compile(
    r"""\b(?:setAttribute|toggleAttribute|attr)\s*\(\s*(["'`])(data-bs-[\w-]+)\1"""
)
#: One set through the element's dataset, el.dataset.bsToggle = ... or
#: el.dataset["bsToggle"] = ..., which the browser writes as data-bs-toggle.
SCRIPT_DATA_BS_DATASET = re.compile(
    r"""\.dataset\s*(?:\.\s*(bs[A-Z]\w*)|\[\s*(["'`])(bs[A-Z]\w*)\2\s*\])\s*=(?!=)"""
)


def _literals_after(text: str, start: int, whole_expression: bool):
    """The string literals in the value that starts at `start`.

    One value is a single string or a single bracketed group. A whole
    expression runs to a ";" or "," outside any bracket, a bracket that
    closes one opened before it, or the end of a line that the next line
    does not carry on with an operator.
    """
    depth = 0
    i = start
    while i < len(text):
        literal = SCRIPT_STRING.match(text, i)
        if literal:
            yield literal.group(0)
            i = literal.end()
            if depth == 0 and not whole_expression:
                return
            continue
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0 or (depth == 0 and not whole_expression):
                return
        elif depth == 0 and not whole_expression:
            return
        elif depth == 0 and ch in ";,":
            return
        elif depth == 0 and ch == "\n":
            if text[i:].lstrip()[:1] not in ("?", ":", "+", "|", "&"):
                return
        i += 1


def _words_in_literal(literal: str):
    """The class names in one string literal.

    A template literal's text is read as class names and its ${...} parts
    as script, whose own string literals are read in turn. A name glued to
    a ${...}, btn-${size}, is built at run time and is not read.
    """
    body = literal[1:-1]
    if literal[0] != "`":
        yield from body.split()
        return
    text = []
    i = 0
    while i < len(body):
        if body.startswith("${", i):
            depth = 0
            j = i + 1
            while j < len(body):
                depth += {"{": 1, "}": -1}.get(body[j], 0)
                if depth == 0:
                    break
                j += 1
            for inner in SCRIPT_STRING.findall(body[i + 2 : j]):
                yield from _words_in_literal(inner)
            text.append("\0")
            i = j + 1
        else:
            text.append(body[i])
            i += 1
    for word in "".join(text).split():
        if "\0" not in word:
            yield word


def _script_data_bs(text: str):
    """Every data-bs-* attribute a script puts on an element.

    Reading only "data-bs-...=" missed the other ways a script sets one:
    by name, through the dataset, or with no value in a string of markup.
    Reading an attribute, getAttribute or a dataset comparison, puts
    nothing on the page and is not counted.
    """
    code = SCRIPT_COMMENT.sub(lambda m: m.group(1) or " ", text)
    yield from SCRIPT_DATA_BS.findall(code)
    for attribute, value in _attributes_in(code):
        if attribute.lower().startswith("data-bs-") and not value:
            yield attribute.lower()
    for found in SCRIPT_DATA_BS_BY_NAME.finditer(code):
        yield found.group(2)
    for found in SCRIPT_DATA_BS_DATASET.finditer(code):
        key = found.group(1) or found.group(3)
        yield "data-" + re.sub(r"[A-Z]", lambda m: "-" + m.group(0).lower(), key)


def _script_classes(text: str):
    """Every class name a script writes, literally, where it names classes.

    Known limits: a class held in a variable before it is assigned, or built
    from pieces at run time, is not seen.
    """
    text = SCRIPT_COMMENT.sub(lambda m: m.group(1) or " ", text)
    for anchor, whole_expression in ((CLASS_VALUE, False), (CLASS_EXPRESSION, True)):
        for found in anchor.finditer(text):
            for literal in _literals_after(text, found.end(), whole_expression):
                yield from _words_in_literal(literal)
    # Markup built as a string, for innerHTML. JSX says className, so this
    # only ever reads the strings.
    yield from _classes_in(text)


def bootstrap_uses() -> "dict[str, Counter]":
    """Every Bootstrap class and data-bs-* attribute, by name, per file."""
    uses: dict = {}
    for key, path in _files():
        if key in NO_BOOTSTRAP_HERE:
            continue
        text = path.read_text(errors="replace")
        if path.suffix in (".ts", ".tsx"):
            names = list(_script_classes(text))
            attributes = list(_script_data_bs(text))
        else:
            live = _live_markup(text)
            names = list(_classes_in(live))
            attributes = list(_data_bs_in(live))
        ours = OUR_OWN_NAMES.get(key, ())
        here = Counter(
            name for name in names + attributes if name not in ours and _keys_of(name)
        )
        if here:
            uses[key] = here
    return uses


def recorded_uses() -> "dict[str, Counter]":
    """What bootstrap_uses_by_file.txt allows each file, by name."""
    recorded: dict = {}
    for line in USES_BY_FILE.read_text().splitlines():
        if line and not line.startswith("#"):
            key, name, count = line.split(" ")
            recorded.setdefault(key, Counter())[name] = int(count)
    return recorded


def _uses_list(uses: "dict[str, Counter]") -> str:
    """The text of bootstrap_uses_by_file.txt for these uses."""
    return USES_BY_FILE_HEADER + "".join(
        "%s %s %d\n" % (key, name, count)
        for key in sorted(uses)
        for name, count in sorted(uses[key].items())
        if count
    )


def _lowered(recorded: "dict[str, Counter]", now: "dict[str, Counter]") -> dict:
    """Each recorded number brought down to what the file has now.

    Never up, and never a name the list does not already hold for that
    file, so running it cannot let a use grow.
    """
    lowered: dict = {}
    for key, allowed in recorded.items():
        for name, count in allowed.items():
            left = min(count, now.get(key, Counter())[name])
            if left:
                lowered.setdefault(key, Counter())[name] = left
    return lowered


# Known limits, so nobody reads more into a green run than is there. A class
# whose name arrives in a variable is not seen, and neither is one a script
# builds from pieces. This counts what is written literally in the markup,
# inside a condition or not, and what the scripts write where they name
# classes, which is where Bootstrap actually sits in this codebase.


def bootstrap_counts() -> "dict[str, Counter]":
    """How often each family and watched class is used, per file.

    Per file, not per site, so removing a use on one page cannot pay for
    adding one on another.
    """
    counts: dict = {}
    for key, here in bootstrap_uses().items():
        tally: Counter = Counter()
        for name, found in here.items():
            for counted in _keys_of(name):
                tally[counted] += found
        counts[key] = tally
    return counts


def totals() -> Counter:
    total: Counter = Counter()
    for here in bootstrap_counts().values():
        total.update(here)
    return total


def test_the_templates_are_found_at_all() -> None:
    """A path that stops matching would make every count zero and pass."""
    found = _templates()
    assert len(found) > 100, "only found %d templates under %s" % (
        len(found),
        TEMPLATES,
    )
    assert (USER_TEMPLATES / "login.html").exists(), USER_TEMPLATES
    assert len(_scripts()) > 10, "only found %d scripts under %s" % (
        len(_scripts()),
        SCRIPTS,
    )


def test_no_page_reaches_for_more_bootstrap() -> None:
    counts = totals()
    grown = [
        "%s: %d now, %d allowed -- use %s instead"
        % (name, counts[name], BASELINE.get(name, 0), INSTEAD[name])
        for name in sorted(set(counts) | set(BASELINE))
        if counts[name] > BASELINE.get(name, 0)
    ]
    assert not grown, (
        "Bootstrap is on the way out and these went up:\n  %s\n"
        "If the use is genuinely needed, raise the number here in the same "
        "commit and say why." % "\n  ".join(grown)
    )


def test_no_single_page_reaches_for_more_bootstrap() -> None:
    """The site total can hold steady while one page gets worse.

    Removing a use from one file must not buy the right to add one to
    another, and removing one class from a page must not buy the right to
    put another there. Swapping chat consent's d-grid for d-none would
    leave every family's count where it was and hide its Continue button.
    So each file is held to each name it has today.
    """
    recorded = recorded_uses()
    grown = [
        "%s: %s went from %d to %d (%s) -- use %s instead"
        % (
            path,
            name,
            recorded.get(path, Counter())[name],
            found,
            _keys_of(name)[0],
            INSTEAD[_keys_of(name)[0]],
        )
        for path, here in sorted(bootstrap_uses().items())
        for name, found in sorted(here.items())
        if found > recorded.get(path, Counter())[name]
    ]
    assert not grown, (
        "these files reached for more Bootstrap:\n  %s\n"
        "Use the site's own classes, or raise the number in "
        "bootstrap_uses_by_file.txt in the same commit and say why."
        % "\n  ".join(grown)
    )


def test_the_baseline_has_no_stale_numbers() -> None:
    """A number above what is really there stops holding anything down."""
    counts = totals()
    stale = [
        "%s: allowed %d, only %d left" % (name, BASELINE[name], counts[name])
        for name in sorted(BASELINE)
        if counts[name] < BASELINE[name]
    ]
    assert not stale, (
        "lower these to what the templates actually have, so the backlog "
        "cannot quietly grow back into the headroom:\n  %s" % "\n  ".join(stale)
    )


def test_the_list_by_file_has_no_stale_numbers() -> None:
    uses = bootstrap_uses()
    stale = [
        "%s: %s allowed %d, only %d left"
        % (path, name, allowed, uses.get(path, Counter())[name])
        for path, here in sorted(recorded_uses().items())
        for name, allowed in sorted(here.items())
        if uses.get(path, Counter())[name] < allowed
    ]
    assert not stale, (
        "lower these to what the files actually have, with\n"
        "  python tests/sync/test_bootstrap_ratchet.py --lower\n  %s"
        % "\n  ".join(stale)
    )


def test_the_list_by_file_is_in_its_own_order() -> None:
    """The list is exactly what --lower would write.

    Sorted, one line per file and name, every count above zero, and the
    header on top, so the diff --lower makes is only the numbers that moved.
    """
    assert USES_BY_FILE.read_text() == _uses_list(recorded_uses())


def test_the_list_by_file_names_only_bootstrap() -> None:
    """A misspelt class in the list would hold nothing down."""
    unknown = sorted(
        "%s: %s" % (path, name)
        for path, here in recorded_uses().items()
        for name in here
        if not (family_of(name) or name.startswith("data-bs-"))
    )
    assert not unknown, "not a Bootstrap class or data-bs attribute: %s" % unknown


def test_lowering_the_list_never_raises_or_adds_a_use() -> None:
    recorded = {
        "a.html": Counter({"btn": 3, "mt-3": 1}),
        "gone.html": Counter({"row": 2}),
    }
    now = {"a.html": Counter({"btn": 2, "mt-3": 4, "d-none": 1})}
    assert _lowered(recorded, now) == {"a.html": Counter({"btn": 2, "mt-3": 1})}


def test_every_count_has_a_name_it_can_mean() -> None:
    """A misspelt key in a baseline would hold nothing down and say nothing."""
    unknown = sorted(name for name in BASELINE if name not in INSTEAD)
    assert not unknown, "not a family or watched class: %s" % unknown
    missing = sorted(
        name
        for name in [family for family, _ in FAMILIES] + list(WATCHED) + [DATA_BS]
        if name not in INSTEAD
    )
    assert not missing, "INSTEAD has nothing to suggest for %s" % missing


def test_the_class_list_is_bootstrap_5_2_3() -> None:
    """The list is Bootstrap's own, read from its stylesheet, and only that.

    Our own classes that only look like Bootstrap's are not in it. The
    site's stylesheets define all four of these: btn-default and
    navbar-default are Bootstrap 3's names, no-gutters is Bootstrap 4's, and
    text-align-center was never Bootstrap's.
    """
    text = CLASS_LIST.read_text()
    assert text.startswith(CLASS_LIST_HEADER), "the header says how to regenerate"
    names = [line for line in text.splitlines() if not line.startswith("#")]
    assert names == sorted(set(names)), "one name per line, sorted, no repeats"
    classes = bootstrap_classes()
    assert len(classes) > 1500, len(classes)
    for name in ("btn", "col-md-6", "d-flex", "mb-3", "text-muted", "row"):
        assert name in classes, name
    for name in ("btn-default", "no-gutters", "text-align-center", "navbar-default"):
        assert name not in classes, name


def test_only_bootstraps_own_col_classes_count_as_col() -> None:
    """A class of ours may start with "col-" and still not be Bootstrap's."""
    assert _keys_of("col-md-6") == ["grid", "col-"]
    assert _keys_of("col-form-label") == ["forms", "col-"]
    assert _keys_of("col-our-layout") == []
    assert _keys_of("col-md-offset-1") == []


def test_every_bootstrap_class_has_exactly_one_family() -> None:
    """A class in no family would be counted nowhere; in two, twice."""
    wrong = {}
    for name in sorted(bootstrap_classes()):
        families = [
            family for family, pattern in _FAMILY_PATTERNS if pattern.fullmatch(name)
        ]
        if len(families) != 1:
            wrong[name] = families
    assert not wrong, wrong
    names = [family for family, _ in FAMILIES] + list(WATCHED) + [DATA_BS]
    assert len(names) == len(set(names)), "a family shares a name with a class"
    assert not bootstrap_classes() & {family for family, _ in FAMILIES}


def test_the_stylesheet_is_read_by_its_selectors() -> None:
    css = """
    /* .not-this */
    @media (min-width: 576px) { .col-sm-6, .a > .b:not(.c) { width: 50%; } }
    .x[data-y=".z"] { background: url("data:image/svg+xml,.nope{"); }
    @keyframes spin { from { opacity: 0.5 } }
    """
    assert class_names_in_stylesheet(css) == {"col-sm-6", "a", "b", "c", "x"}


def test_a_script_counts_classes_only_where_it_names_them() -> None:
    """A word in an error message is not a class, and a className is."""

    def read(script: str) -> "list[str]":
        return list(_script_classes(script))

    assert read('el.className = "btn btn-primary";') == ["btn", "btn-primary"]
    assert read('<a className="btn mt-auto" role="alert">x</a>') == ["btn", "mt-auto"]
    assert read('<a className={`card ${open ? "show" : ""}`}>x</a>') == [
        "show",
        "card",
    ]
    assert read('el.classList.add("visually-hidden", "d-none");') == [
        "visually-hidden",
        "d-none",
    ]
    assert read("b.className = on\n  ? 'btn btn-green'\n  : 'btn';") == [
        "btn",
        "btn-green",
        "btn",
    ]
    assert read("return { char: '', className: 'row' };") == ["row"]
    assert read("el.innerHTML = '<div class=\"alert alert-info\">x</div>';") == [
        "alert",
        "alert-info",
    ]
    assert read('throw new Error("container not found");') == []
    assert read('// el.className = "btn";\nx = 1;') == []
    assert read('if (e.classList.contains("show")) {\n  go("row");\n}') == ["show"]


def test_a_data_bs_attribute_counts_where_it_is_one() -> None:
    markup = '<button data-bs-toggle="collapse" data-bs-target="#a">x</button>'
    assert list(_data_bs_in(markup)) == ["data-bs-toggle", "data-bs-target"]
    assert list(_data_bs_in("<p>prose about data-bs-toggle</p>")) == []
    assert SCRIPT_DATA_BS.findall('<b data-bs-toggle="tooltip">x</b>') == [
        "data-bs-toggle"
    ]


def test_a_data_bs_attribute_without_a_value_still_counts() -> None:
    """The browser puts it on the element all the same."""
    assert list(_data_bs_in("<button data-bs-toggle>x</button>")) == ["data-bs-toggle"]
    spread = '<button\n  data-bs-dismiss\n  class="btn">x</button>'
    assert list(_data_bs_in(spread)) == ["data-bs-dismiss"]
    # Reading them that way leaves the classes where they were.
    assert list(_classes_in('<input required class="btn" disabled>')) == ["btn"]
    assert list(_classes_in("<button data-bs-toggle>x</button>")) == []


def test_a_script_counts_every_way_it_sets_a_data_bs_attribute() -> None:
    def read(script: str) -> "list[str]":
        return list(_script_data_bs(script))

    assert read('el.setAttribute("data-bs-toggle", "collapse");') == ["data-bs-toggle"]
    assert read("el.toggleAttribute('data-bs-dismiss');") == ["data-bs-dismiss"]
    assert read('$(el).attr("data-bs-target", "#a");') == ["data-bs-target"]
    assert read('el.dataset.bsToggle = "collapse";') == ["data-bs-toggle"]
    assert read('el.dataset["bsTarget"] = "#a";') == ["data-bs-target"]
    assert read("const b = '<button data-bs-toggle>x</button>';") == ["data-bs-toggle"]
    assert read('<b data-bs-toggle="tooltip">x</b>') == ["data-bs-toggle"]
    # Reading one, comparing one, a comment and prose put nothing on a page.
    assert read('el.getAttribute("data-bs-toggle");') == []
    assert read('if (el.dataset.bsToggle === "collapse") {}') == []
    assert read('// el.setAttribute("data-bs-toggle", "collapse");') == []
    assert read('throw new Error("no data-bs-toggle here");') == []


def test_the_pages_left_out_load_no_bootstrap() -> None:
    """A page on NO_BOOTSTRAP_HERE is counted as soon as it could load it."""
    ours = {key for key, _ in _files()}
    everything = [path.read_text(errors="replace") for _, path in _files()]
    problems = []
    for page in NO_BOOTSTRAP_HERE:
        path = TEMPLATES / page
        if not path.exists():
            problems.append("%s: no longer exists" % page)
            continue
        live = _live_markup(path.read_text(errors="replace"))
        if "bootstrap" in live.lower():
            problems.append("%s: names Bootstrap" % page)
        if re.search(r"<link\b[^>]*stylesheet|<script\b[^>]*\bsrc=", live, re.I):
            problems.append("%s: loads a stylesheet or script" % page)
        for parent in re.findall(r"""{%\s*extends\s+["']([^"']+)""", live):
            if parent in ours:
                problems.append("%s: extends %s" % (page, parent))
        pulled_in = re.compile(
            r"""{%%\s*(?:include|extends)\s+["']%s["']""" % re.escape(page)
        )
        if any(pulled_in.search(text) for text in everything):
            problems.append("%s: another template pulls it in" % page)
    assert not problems, (
        "count these pages again, by taking them off NO_BOOTSTRAP_HERE:\n  %s"
        % "\n  ".join(problems)
    )


def test_our_own_names_are_still_used_where_they_are_listed() -> None:
    files = dict(_files())
    stale = []
    for key, names in sorted(OUR_OWN_NAMES.items()):
        text = files[key].read_text(errors="replace")
        found = (
            set(_script_classes(text))
            if key.startswith("static/js/")
            else set(_classes_in(_live_markup(text)))
        )
        stale.extend("%s: %s" % (key, name) for name in names if name not in found)
    assert not stale, "take these off OUR_OWN_NAMES:\n  %s" % "\n  ".join(stale)


def test_only_a_real_class_attribute_counts() -> None:
    """The tail of another attribute is not a class attribute.

    "class=" matched wherever it appeared, so markup like x.class="btn"
    counted a use that no browser would apply.
    """
    assert list(_classes_in('<b class="btn row">')) == ["btn", "row"]
    assert list(_classes_in('<b x.class="btn">')) == []
    assert list(_classes_in('<div\n  class="row">')) == ["row"]


def test_a_class_inside_another_attribute_is_not_a_class() -> None:
    """Reading the whole file counted text nobody sees as markup.

    An attribute value can contain the word, and so can prose and a
    stylesheet. None of those put a class on an element.
    """
    assert list(_classes_in("""<div data-label='use class="btn"'>x</div>""")) == []
    assert list(_classes_in('prose mentioning class="btn" in it')) == []
    assert list(_classes_in("<style>.btn { color: red }</style>")) == []


def test_a_greater_than_in_an_earlier_value_does_not_end_the_tag() -> None:
    """A ">" is ordinary inside an attribute value.

    Stopping the tag at the first ">" hid every attribute after it,
    including the class, so a page could add Bootstrap behind one and the
    ratchet would never see it.
    """
    assert list(_classes_in('<div data-tip="a > b" class="btn">x</div>')) == ["btn"]
    assert list(_classes_in("<a title='5 > 4' class='card'>x</a>")) == ["card"]


def test_a_class_behind_a_condition_counts() -> None:
    """The page can render it, so it is a use.

    Left as raw text, the tag's own words were counted and the class was
    not, and a quote inside the tag ended the attribute early.
    """
    assert list(_classes_in('<input class="{% if x %}form-control{% endif %}">')) == [
        "form-control"
    ]
    assert list(
        _classes_in("""<a class="{% if x %}btn{% else %}card{% endif %}">x</a>""")
    ) == ["btn", "card"]


def test_a_commented_block_with_a_note_is_still_ignored() -> None:
    """Django's comment tag takes an optional note.

    A pattern that allowed nothing between the word and the brace left
    {% comment "why this is here" %} blocks counted.
    """
    plain = '{% comment %}<a class="btn">x</a>{% endcomment %}'
    noted = '{% comment "kept for reference" %}<a class="btn">x</a>{% endcomment %}'

    assert list(_classes_in(_live_markup(plain))) == []
    assert list(_classes_in(_live_markup(noted))) == []
    assert list(_classes_in(_live_markup('<a class="btn">x</a>'))) == ["btn"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--lower"]:
        # Brings bootstrap_uses_by_file.txt down to what the files have now.
        USES_BY_FILE.write_text(_uses_list(_lowered(recorded_uses(), bootstrap_uses())))
    else:
        # Regenerates the class list from Bootstrap's stylesheet on stdin;
        # the list's own header says how.
        sys.stdout.write(
            CLASS_LIST_HEADER
            + "".join(
                "%s\n" % name
                for name in sorted(class_names_in_stylesheet(sys.stdin.read()))
            )
        )

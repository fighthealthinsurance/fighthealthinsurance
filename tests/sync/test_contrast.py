"""Text contrast gate for the site's own stylesheets.

The point of this file is that it is not a list of buttons. It reads every rule
in custom.css and main.css, works out what colour the words are and what colour
they sit on, and fails any pair under the WCAG AA ratio of 4.5:1. A hand
written list of selectors is what let white-on-lime survive on the only button
on the delete-your-data page, so the gate reads the source instead of trusting
anybody's memory of it.

Working out what the words sit on needs more than the stylesheets, because a
selector like `.pwyw-thanks` says nothing about where the element renders. So
the gate builds the pages too: it resolves each template's `{% extends %}` and
`{% include %}`, walks the elements, and asks what the real ancestors paint.
That is how it can tell that the home page's payment confirmation renders on a
dark purple panel over the hero photograph rather than on a white page.

Three things are then true of every rule that writes words, and the gate says
which one out loud rather than reporting green by default:

* Resolved. Some opaque fill was found, or the ground is a translucent stack
  thin enough that the whole range it can take still lands on one side of
  4.5:1. The pair is measured and passes or fails.
* Unresolved. The ground bottoms out in the hero photograph and the veil over
  it does not narrow the range enough to decide. Reported, and named in
  UNRESOLVED with a reason. An unresolvable ground scored as a pass is the same
  hole as a hand written selector list: nobody has checked it, and green says
  somebody has.
* Unreached. The selector matches no element on any page the templates build,
  so the rule paints nothing. Reported, and named in UNREACHED with a reason.

Each of the three lists can only shrink: an entry whose rule leaves the
stylesheet fails, and so does one whose rule starts being measurable or
starts passing.

What the gate still cannot see: colours a script writes at runtime (the drafts
page phase labels, named in UNREACHED), and anything Bootstrap contributes,
which is why the focus ring is checked against Bootstrap's known weight rather
than against Bootstrap's actual text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = REPO_ROOT / "fighthealthinsurance" / "static" / "css"
TEMPLATE_DIR = REPO_ROOT / "fighthealthinsurance" / "templates"

# In base.html order, which is also cascade order: later wins a tie.
STYLESHEETS = ("custom.css", "main.css")

MINIMUM_RATIO = 4.5


# ---------------------------------------------------------------- css parsing


@dataclass(frozen=True)
class Rule:
    """One `selector { ... }` block, with the selector exactly as written."""

    stylesheet: str
    line: int
    selector: str
    declarations: tuple[tuple[str, str, bool], ...]  # property, value, !important

    @property
    def selectors(self) -> list[str]:
        return [s.strip() for s in self.selector.split(",") if s.strip()]


def _strip_comments(css: str) -> str:
    return re.sub(
        r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S
    )


def _split_declarations(body: str) -> tuple[tuple[str, str, bool], ...]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == ";" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    out: list[tuple[str, str, bool]] = []
    for part in parts:
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        prop = prop.strip().lower()
        stripped = re.sub(r"!\s*important\s*$", "", value.strip()).strip()
        important = stripped != value.strip()
        if prop and stripped and not prop.startswith("//"):
            out.append((prop, stripped, important))
    return tuple(out)


_NESTING_AT_RULES = re.compile(r"@(media|supports|layer|container|scope)\b")


def parse_stylesheet(text: str, stylesheet: str) -> list[Rule]:
    """Flatten a stylesheet into rules, descending into @media and friends."""
    rules: list[Rule] = []

    def walk(source: str, first_line: int) -> None:
        index = 0
        line = first_line
        start_line = first_line
        buffer = ""
        length = len(source)
        while index < length:
            char = source[index]
            if char == "{":
                selector = " ".join(buffer.split())
                buffer = ""
                depth = 1
                end = index + 1
                while end < length and depth:
                    if source[end] == "{":
                        depth += 1
                    elif source[end] == "}":
                        depth -= 1
                    end += 1
                inner = source[index + 1 : end - 1]
                if selector.startswith("@"):
                    if _NESTING_AT_RULES.match(selector):
                        walk(inner, line)
                else:
                    rules.append(
                        Rule(
                            stylesheet, start_line, selector, _split_declarations(inner)
                        )
                    )
                line += source[index:end].count("\n")
                index = end
                start_line = line
                continue
            if char in "};":
                # `}` ends a block; `;` ends a statement at-rule such as the
                # @import at the top of main.css, whose text would otherwise
                # run into the next selector and hide the rule after it.
                buffer = ""
                start_line = line
            else:
                if char == "\n":
                    line += 1
                    if not buffer.strip():
                        start_line = line
                buffer += char
            index += 1

    walk(_strip_comments(text), 1)
    return rules


def load_rules() -> list[Rule]:
    rules: list[Rule] = []
    for name in STYLESHEETS:
        rules.extend(parse_stylesheet((CSS_DIR / name).read_text(), name))
    return rules


def custom_properties(rules: Iterable[Rule]) -> dict[str, str]:
    """Every --token declared on :root, so var() can be resolved."""
    values: dict[str, str] = {}
    for rule in rules:
        if ":root" not in rule.selector:
            continue
        for prop, value, _ in rule.declarations:
            if prop.startswith("--"):
                values[prop] = value
    return values


def resolve_vars(value: str, variables: dict[str, str]) -> str:
    for _ in range(8):
        if "var(" not in value:
            break

        def substitute(match: re.Match[str]) -> str:
            name, _, fallback = match.group(1).partition(",")
            return variables.get(name.strip(), fallback.strip())

        value = re.sub(r"var\(([^()]*)\)", substitute, value)
    return value


# --------------------------------------------------------------------- colour

RGBA = tuple[int, int, int, float]

NAMED_COLOURS: dict[str, RGBA] = {
    "transparent": (0, 0, 0, 0.0),
    "white": (255, 255, 255, 1.0),
    "black": (0, 0, 0, 1.0),
    "red": (255, 0, 0, 1.0),
    "darkred": (139, 0, 0, 1.0),
    "crimson": (220, 20, 60, 1.0),
    "orange": (255, 165, 0, 1.0),
    "gold": (255, 215, 0, 1.0),
    "yellow": (255, 255, 0, 1.0),
    "green": (0, 128, 0, 1.0),
    "teal": (0, 128, 128, 1.0),
    "blue": (0, 0, 255, 1.0),
    "navy": (0, 0, 128, 1.0),
    "maroon": (128, 0, 0, 1.0),
    "silver": (192, 192, 192, 1.0),
    "gray": (128, 128, 128, 1.0),
    "grey": (128, 128, 128, 1.0),
    "darkgray": (169, 169, 169, 1.0),
    "darkgrey": (169, 169, 169, 1.0),
    "lightgray": (211, 211, 211, 1.0),
    "lightgrey": (211, 211, 211, 1.0),
    "whitesmoke": (245, 245, 245, 1.0),
}

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}")
_RGB_FUNC = re.compile(r"\brgba?\(([^()]*)\)")


def parse_colour(token: str) -> Optional[RGBA]:
    token = token.strip()
    lowered = token.lower()
    if lowered in NAMED_COLOURS:
        return NAMED_COLOURS[lowered]
    if _HEX.fullmatch(token):
        digits = token[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        if len(digits) in (6, 8):
            red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
            alpha = int(digits[6:8], 16) / 255.0 if len(digits) == 8 else 1.0
            return (red, green, blue, alpha)
        return None
    match = _RGB_FUNC.fullmatch(token)
    if match:
        pieces = [p for p in re.split(r"[,/\s]+", match.group(1).strip()) if p]
        try:
            channels = [
                float(p[:-1]) * 255 / 100 if p.endswith("%") else float(p)
                for p in pieces[:3]
            ]
            alpha = 1.0
            if len(pieces) > 3:
                raw = pieces[3]
                alpha = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
        except (ValueError, IndexError):
            return None
        if len(channels) != 3:
            return None
        return (
            int(round(channels[0])),
            int(round(channels[1])),
            int(round(channels[2])),
            alpha,
        )
    return None


def colours_in(value: str) -> list[RGBA]:
    """Every colour in a declaration value, in source order.

    A gradient yields one entry per stop, because the words sit on all of
    them and the worst stop is the one that decides whether they can be read.
    """
    found: list[tuple[int, RGBA]] = []
    for match in _RGB_FUNC.finditer(value):
        colour = parse_colour(match.group(0))
        if colour:
            found.append((match.start(), colour))
    masked = _RGB_FUNC.sub(lambda m: " " * len(m.group(0)), value)
    for match in _HEX.finditer(masked):
        colour = parse_colour(match.group(0))
        if colour:
            found.append((match.start(), colour))
    for match in re.finditer(r"[a-zA-Z]+", masked):
        word = match.group(0).lower()
        if word in NAMED_COLOURS and word != "transparent":
            found.append((match.start(), NAMED_COLOURS[word]))
    return [colour for _, colour in sorted(found)]


def relative_luminance(rgb: Sequence[int]) -> float:
    def channel(value: int) -> float:
        scaled = value / 255.0
        if scaled <= 0.03928:
            return scaled / 12.92
        return ((scaled + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
    )


def contrast_ratio(one: Sequence[int], other: Sequence[int]) -> float:
    first, second = relative_luminance(one), relative_luminance(other)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def flatten(top: RGBA, backdrop: tuple[int, int, int]) -> tuple[int, int, int]:
    red, green, blue, alpha = top
    if alpha >= 1.0:
        return (red, green, blue)
    return (
        int(round(red * alpha + backdrop[0] * (1 - alpha))),
        int(round(green * alpha + backdrop[1] * (1 - alpha))),
        int(round(blue * alpha + backdrop[2] * (1 - alpha))),
    )


# ------------------------------------------------------------- css selectors

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
BACKGROUND_PROPERTIES = ("background", "background-color", "background-image")

_SINGLE_COLON_ELEMENTS = frozenset(
    (":before", ":after", ":first-line", ":first-letter")
)
# Pseudo-classes that describe a moment rather than the page at rest. A rule
# behind one of these paints only while it is true, so it neither replaces the
# element's resting colours nor hides them.
_STATE_PSEUDO_CLASSES = frozenset(
    (
        "hover",
        "focus",
        "focus-visible",
        "focus-within",
        "active",
        "visited",
        "target",
        "checked",
        "disabled",
        "enabled",
        "indeterminate",
        "placeholder-shown",
    )
)


@dataclass(frozen=True)
class Compound:
    """One `div#id.cls[attr]:hover` step of a selector."""

    tag: str
    ident: str
    classes: frozenset[str]
    attributes: tuple[tuple[str, str, str], ...]
    negations: tuple["Compound", ...]
    pseudo_classes: int
    pseudo_elements: int
    states: frozenset[str] = frozenset()

    @property
    def anchored(self) -> bool:
        """Does this step name anything a document can actually carry?"""
        return bool(self.tag or self.ident or self.classes or self.attributes)


_COMPOUND_PART = re.compile(
    r"(?P<fn>::?[a-zA-Z-]+\([^()]*\))"
    r"|(?P<pseudo>::?[a-zA-Z-]+)"
    r"|(?P<attr>\[[^\]]*\])"
    r"|(?P<ident>\#[\w-]+)"
    r"|(?P<cls>\.[\w-]+)"
    r"|(?P<tag>\*|[a-zA-Z][\w-]*)"
)
_ATTRIBUTE = re.compile(
    r"\[\s*([\w-]+)\s*(?:([~^$*|]?=)\s*[\"']?([^\]\"']*)[\"']?)?\s*\]"
)


def parse_compound(text: str) -> Compound:
    tag = ident = ""
    classes: set[str] = set()
    attributes: list[tuple[str, str, str]] = []
    negations: list[Compound] = []
    states: set[str] = set()
    pseudo_classes = pseudo_elements = 0
    for match in _COMPOUND_PART.finditer(text):
        if match.group("fn"):
            body = match.group("fn")
            name = body[: body.index("(")].lstrip(":").lower()
            argument = body[body.index("(") + 1 : -1]
            if name == "not":
                negations.extend(
                    parse_compound(piece)
                    for piece in argument.split(",")
                    if piece.strip()
                )
            else:
                if name in _STATE_PSEUDO_CLASSES:
                    states.add(name)
                pseudo_classes += 1
        elif match.group("pseudo"):
            word = match.group("pseudo").lower()
            if word.startswith("::") or word in _SINGLE_COLON_ELEMENTS:
                pseudo_elements += 1
            else:
                if word.lstrip(":") in _STATE_PSEUDO_CLASSES:
                    states.add(word.lstrip(":"))
                pseudo_classes += 1
        elif match.group("attr"):
            found = _ATTRIBUTE.match(match.group("attr"))
            if found:
                attributes.append(
                    (
                        found.group(1).lower(),
                        found.group(2) or "",
                        (found.group(3) or "").strip(),
                    )
                )
            pseudo_classes += 1
        elif match.group("ident"):
            ident = match.group("ident")[1:]
        elif match.group("cls"):
            classes.add(match.group("cls")[1:])
        elif match.group("tag") and match.group("tag") != "*":
            tag = match.group("tag").lower()
    return Compound(
        tag,
        ident,
        frozenset(classes),
        tuple(attributes),
        tuple(negations),
        pseudo_classes,
        pseudo_elements,
        frozenset(states),
    )


Step = tuple[str, Compound]


def split_selector(selector: str) -> list[Step]:
    """`a > b c` into steps, each carrying the combinator on its left."""
    steps: list[Step] = []
    pending = ""
    buffer = ""
    depth = 0
    for char in " ".join(selector.split()):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and char in " >+~":
            if buffer:
                steps.append((pending, parse_compound(buffer)))
                buffer = ""
                pending = " "
            if char in ">+~":
                pending = char
            continue
        buffer += char
    if buffer:
        steps.append((pending, parse_compound(buffer)))
    return steps


def specificity(selector: str) -> tuple[int, int, int]:
    ids = classes = elements = 0
    for _, compound in split_selector(selector):
        for part in (compound,) + compound.negations:
            ids += 1 if part.ident else 0
            classes += len(part.classes) + part.pseudo_classes
            elements += (1 if part.tag else 0) + part.pseudo_elements
    return (ids, classes, elements)


# -------------------------------------------------------- the templates' DOM

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_DJANGO = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
_INCLUDE = re.compile(r"\{%\s*include\s+[\"']([^\"']+)[\"'][^%]*%\}")
_EXTENDS = re.compile(r"\{%\s*extends\s+[\"']([^\"']+)[\"']\s*%\}")
_BLOCK_OPEN = re.compile(r"\{%\s*block\s+([\w.-]+)\s*%\}")
_BLOCK_CLOSE = re.compile(r"\{%\s*endblock[^%]*%\}")
_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w-]*)([^>]*?)(/?)>", re.S)
_ATTR_IN_TAG = re.compile(r"([\w:@-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)")
_CLASS_NAME = re.compile(r"-?[A-Za-z_][\w-]*")
_BRANCH = re.compile(
    r"\{%\s*if\b.*?%\}(.*?)\{%\s*endif\s*%\}", re.S
)
_BRANCH_SPLIT = re.compile(r"\{%\s*(?:elif\b.*?|else)\s*%\}", re.S)


def _class_variants(raw: str) -> tuple[frozenset[str], ...]:
    """The class sets an element can really carry.

    `class="btn {% if x %}section-btn{% else %}btn-outline-green{% endif %}"`
    is two different buttons, never one wearing both. Merging the branches
    invents an element that never renders and the pairs measured off it are
    fiction, so each branch is kept as its own set.
    """
    variants = [""]
    rest = raw
    while True:
        found = _BRANCH.search(rest)
        if found is None:
            variants = [variant + " " + rest for variant in variants]
            break
        head, tail = rest[: found.start()], rest[found.end() :]
        branches = _BRANCH_SPLIT.split(found.group(1)) or [""]
        if len(branches) == 1:
            branches = branches + [""]  # an if with no else can also be absent
        variants = [
            variant + " " + head + " " + branch
            for variant in variants
            for branch in branches
        ]
        rest = tail
        if len(variants) > 16:
            return (
                frozenset(
                    word
                    for word in _DJANGO.sub(" ", raw).split()
                    if _CLASS_NAME.fullmatch(word)
                ),
            )
    return tuple(
        frozenset(
            word
            for word in _DJANGO.sub(" ", variant).split()
            if _CLASS_NAME.fullmatch(word)
        )
        for variant in variants
    )


def _block_bodies(text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    stack: list[tuple[str, int]] = []
    index = 0
    while index < len(text):
        opened = _BLOCK_OPEN.search(text, index)
        closed = _BLOCK_CLOSE.search(text, index)
        if opened and (closed is None or opened.start() < closed.start()):
            stack.append((opened.group(1), opened.end()))
            index = opened.end()
        elif closed is not None:
            if stack:
                name, start = stack.pop()
                bodies.setdefault(name, text[start : closed.start()])
            index = closed.end()
        else:
            break
    return bodies


def _fill_blocks(parent: str, bodies: dict[str, str]) -> str:
    out: list[str] = []
    index = 0
    while True:
        opened = _BLOCK_OPEN.search(parent, index)
        if opened is None:
            out.append(parent[index:])
            return "".join(out)
        out.append(parent[index : opened.start()])
        depth = 1
        cursor = opened.end()
        body_end = len(parent)
        while depth:
            next_open = _BLOCK_OPEN.search(parent, cursor)
            next_close = _BLOCK_CLOSE.search(parent, cursor)
            if next_close is not None and (
                next_open is None or next_close.start() < next_open.start()
            ):
                depth -= 1
                body_end = next_close.start()
                cursor = next_close.end()
            elif next_open is not None:
                depth += 1
                cursor = next_open.end()
            else:
                cursor = len(parent)
                break
        out.append(bodies.get(opened.group(1), parent[opened.end() : body_end]))
        index = cursor


def template_markup(name: str, stack: tuple[str, ...] = ()) -> str:
    """One template with its parent and its includes resolved into it.

    This is the only way the gate can know that the payment confirmation in
    partials/pwyw_panel.html renders inside the hero's dark panel: the
    relationship is in the templates, never in the selector.
    """
    path = TEMPLATE_DIR / name
    if name in stack or not path.is_file():
        return ""
    text = _HTML_COMMENT.sub(" ", path.read_text(errors="replace"))
    parent = _EXTENDS.search(text)
    if parent is not None:
        text = _fill_blocks(
            template_markup(parent.group(1), stack + (name,)), _block_bodies(text)
        )

    def inline(match: re.Match[str]) -> str:
        return template_markup(match.group(1), stack + (name,))

    for _ in range(8):
        if not _INCLUDE.search(text):
            break
        text = _INCLUDE.sub(inline, text)
    return text


@dataclass
class Node:
    """One element found in a template, with the ancestors it really has."""

    tag: str
    ident: str
    classes: frozenset[str]  # every class any branch can put here
    class_sets: tuple[frozenset[str], ...]  # one set per branch
    attributes: dict[str, str]
    template: str
    parent: Optional["Node"] = None
    earlier_siblings: tuple["Node", ...] = ()

    @property
    def inline_style(self) -> str:
        return self.attributes.get("style", "")


def _nodes_in(markup: str, template: str) -> list[Node]:
    nodes: list[Node] = []
    stack: list[Node] = []
    brothers: dict[int, list[Node]] = {}
    for match in _TAG.finditer(markup):
        closing, tag, raw, self_closing = (
            match.group(1),
            match.group(2).lower(),
            match.group(3),
            match.group(4),
        )
        if closing:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index].tag == tag:
                    del stack[index:]
                    break
            continue
        attributes: dict[str, str] = {}
        raw_class = ""
        for found in _ATTR_IN_TAG.finditer(raw):
            name = found.group(1).lower()
            value = found.group(2).strip("\"'")
            if name == "class":
                raw_class = value
            attributes[name] = _DJANGO.sub(" ", value)
        variants = _class_variants(raw_class) if raw_class else (frozenset(),)
        parent = stack[-1] if stack else None
        siblings = brothers.setdefault(id(parent), [])
        node = Node(
            tag=tag,
            ident=attributes.get("id", "").strip(),
            classes=frozenset().union(*variants) if variants else frozenset(),
            class_sets=variants,
            attributes=attributes,
            template=template,
            parent=parent,
            earlier_siblings=tuple(siblings),
        )
        siblings.append(node)
        nodes.append(node)
        if tag not in VOID_TAGS and not self_closing:
            stack.append(node)
    return nodes


def _compound_matches(
    node: Node, compound: Compound, variant: Optional[frozenset[str]] = None
) -> bool:
    """`variant` pins which branch of a `{% if %}` put the classes there."""
    if compound.tag and compound.tag != node.tag:
        return False
    if compound.ident and compound.ident != node.ident:
        return False
    carried_sets = (variant,) if variant is not None else node.class_sets
    if compound.classes and not any(
        compound.classes <= carried for carried in carried_sets
    ):
        return False
    for name, operator, value in compound.attributes:
        if name not in node.attributes:
            return False
        actual = node.attributes[name]
        if operator == "=" and actual != value:
            return False
        if operator == "~=" and value not in actual.split():
            return False
        if operator == "^=" and not actual.startswith(value):
            return False
        if operator == "$=" and not actual.endswith(value):
            return False
        if operator == "*=" and value not in actual:
            return False
    for other in compound.negations:
        # `:not(:last-child)` says nothing a flattened template can be held
        # to, so it is left alone rather than treated as excluding every
        # element, which would quietly drop the rule from the gate.
        if other.anchored and _compound_matches(node, other, variant):
            return False
    return True


def _matches_at(
    steps: Sequence[Step],
    index: int,
    node: Node,
    variant: Optional[frozenset[str]] = None,
) -> bool:
    combinator, compound = steps[index]
    if not _compound_matches(node, compound, variant):
        return False
    if index == 0:
        return True
    if combinator == ">":
        return node.parent is not None and _matches_at(steps, index - 1, node.parent)
    if combinator == "+":
        return bool(node.earlier_siblings) and _matches_at(
            steps, index - 1, node.earlier_siblings[-1]
        )
    if combinator == "~":
        return any(
            _matches_at(steps, index - 1, sibling) for sibling in node.earlier_siblings
        )
    ancestor = node.parent
    while ancestor is not None:
        if _matches_at(steps, index - 1, ancestor):
            return True
        ancestor = ancestor.parent
    return False


def selector_matches(
    node: Node, steps: Sequence[Step], variant: Optional[frozenset[str]] = None
) -> bool:
    return bool(steps) and _matches_at(steps, len(steps) - 1, node, variant)


def selector_states(steps: Sequence[Step]) -> frozenset[str]:
    """The states the reader has to be in for this selector to paint."""
    return frozenset().union(*(compound.states for _, compound in steps)) if steps else frozenset()


class TemplateDom:
    """Every element on every page the site can serve.

    A template is indexed only once its `{% extends %}` chain has reached
    base.html, so an element's ancestors run all the way up to <body> and the
    ground under it is the page's, not a fragment's.
    """

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self.pages: list[str] = []
        for path in sorted(TEMPLATE_DIR.rglob("*.html")):
            name = path.relative_to(TEMPLATE_DIR).as_posix()
            markup = template_markup(name)
            if "<body" not in markup:
                continue  # a partial or a mail fragment, reached through a page
            self.pages.append(name)
            self.nodes.extend(_nodes_in(markup, name))
        self.by_class: dict[str, list[Node]] = {}
        self.by_ident: dict[str, list[Node]] = {}
        self.by_tag: dict[str, list[Node]] = {}
        for node in self.nodes:
            for name in node.classes:
                self.by_class.setdefault(name, []).append(node)
            if node.ident:
                self.by_ident.setdefault(node.ident, []).append(node)
            self.by_tag.setdefault(node.tag, []).append(node)

    def candidates(self, subject: Compound) -> list[Node]:
        buckets: list[list[Node]] = []
        if subject.ident:
            buckets.append(self.by_ident.get(subject.ident, []))
        for name in subject.classes:
            buckets.append(self.by_class.get(name, []))
        if not buckets and subject.tag:
            buckets.append(self.by_tag.get(subject.tag, []))
        if not buckets:
            return self.nodes
        return min(buckets, key=len)

    def matching(self, steps: Sequence[Step]) -> list[Node]:
        if not steps:
            return []
        return [
            node
            for node in self.candidates(steps[-1][1])
            if selector_matches(node, steps)
        ]


_DOM: Optional[TemplateDom] = None


def template_dom() -> TemplateDom:
    global _DOM
    if _DOM is None:
        _DOM = TemplateDom()
    return _DOM


# ----------------------------------------------------------- background stack


@dataclass(frozen=True)
class Ground:
    """What a box is painted with, as a band the stylesheets can defend.

    `low` and `high` are the same colour when an opaque fill was found. When
    the stack bottoms out in the hero photograph they are that stack composited
    over black and over white, which is the whole range the real pixel can be.
    """

    low: tuple[int, int, int]
    high: tuple[int, int, int]

    @property
    def certain(self) -> bool:
        return self.low == self.high

    def describe(self) -> str:
        if self.certain:
            return "#%02x%02x%02x" % self.low
        return "#%02x%02x%02x..#%02x%02x%02x" % (self.low + self.high)


UNKNOWN_GROUND = Ground(BLACK, WHITE)


def _known(colour: RGBA) -> Ground:
    flat = flatten(colour, WHITE)
    return Ground(flat, flat)


def _composite(stop: RGBA, under: Ground) -> Ground:
    return Ground(flatten(stop, under.low), flatten(stop, under.high))


@dataclass(frozen=True)
class Layer:
    """One background declaration, with the weight the cascade gives it."""

    kind: str  # "colour" or "image"
    stops: tuple[RGBA, ...]
    weight: tuple[int, int, int, int]  # !important, then specificity


def _layers_from(
    declarations: Sequence[tuple[str, str, bool]],
    variables: dict[str, str],
    weight: tuple[int, int, int],
) -> list[Layer]:
    layers: list[Layer] = []
    for prop, raw, important in declarations:
        if prop not in BACKGROUND_PROPERTIES:
            continue
        value = resolve_vars(raw, variables)
        full = (int(important),) + weight
        if "url(" in value:
            layers.append(Layer("image", (), full))
            continue
        found = colours_in(value)
        if found:
            layers.append(Layer("colour", tuple(found), full))
    return layers


class Painter:
    """Resolves what any element on any page is painted on.

    Three things decide that, and all three come from outside the selector:
    which branch of a `{% if %}` put the classes there, which state the reader
    has the element in, and what the real ancestors paint. Every background
    declaration that wins is kept, not one of them: .section-btn is declared in
    both stylesheets at the same specificity and which one wins depends on the
    <link> order in base.html, so measuring both means a reorder cannot turn a
    readable button back into an unreadable one.
    """

    def __init__(self, rules: Sequence[Rule], dom: TemplateDom) -> None:
        self.variables = custom_properties(rules)
        self.dom = dom
        self.backgrounds: list[tuple[list[Step], frozenset[str], list[Layer]]] = []
        self.foregrounds: list[
            tuple[list[Step], frozenset[str], tuple[int, int, int, int], bool]
        ] = []
        for rule in rules:
            important_colour = [
                important
                for prop, _, important in rule.declarations
                if prop == "color"
            ]
            for selector in rule.selectors:
                steps = split_selector(selector)
                weight = specificity(selector)
                states = selector_states(steps)
                if steps and not steps[-1][1].pseudo_elements:
                    # A ::before box paints itself, not the element it hangs
                    # off, so its fill is not what the element's words sit on.
                    layers = _layers_from(rule.declarations, self.variables, weight)
                    if layers:
                        self.backgrounds.append((steps, states, layers))
                pseudo = bool(steps and steps[-1][1].pseudo_elements)
                for important in important_colour:
                    self.foregrounds.append(
                        (steps, states, (int(important),) + weight, pseudo)
                    )
        self._cache: dict[tuple[int, Optional[frozenset[str]], frozenset[str]], list[Ground]] = {}

    # -- the states a reader can put an element into ----------------------

    def states_on(self, node: Node, variant: Optional[frozenset[str]]) -> list[
        frozenset[str]
    ]:
        """Resting, plus every state the stylesheets repaint this element in."""
        found = {frozenset()}
        for steps, states, _ in self.backgrounds:
            if states and selector_matches(node, steps, variant):
                found.add(states)
        for steps, states, _, _ in self.foregrounds:
            if states and selector_matches(node, steps, variant):
                found.add(states)
        return sorted(found, key=lambda entry: (len(entry), sorted(entry)))

    # -- what a node's own box paints -------------------------------------

    def _own_layers(
        self, node: Node, variant: Optional[frozenset[str]], active: frozenset[str]
    ) -> list[Layer]:
        layers: list[Layer] = []
        if node.inline_style and not active:
            layers.extend(
                _layers_from(
                    _split_declarations(node.inline_style),
                    self.variables,
                    (1, 0, 0),
                )
            )
        for steps, states, rule_layers in self.backgrounds:
            if states <= active and selector_matches(node, steps, variant):
                layers.extend(rule_layers)
        if not layers:
            return []
        winning = max(layer.weight for layer in layers)
        return [layer for layer in layers if layer.weight == winning]

    def grounds(
        self,
        node: Optional[Node],
        variant: Optional[frozenset[str]] = None,
        active: frozenset[str] = frozenset(),
        depth: int = 0,
    ) -> list[Ground]:
        if node is None or depth > 24:
            return [UNKNOWN_GROUND]
        key = (id(node), variant, active)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        self._cache[key] = [UNKNOWN_GROUND]
        layers = self._own_layers(node, variant, active)
        if not layers:
            result = self.grounds(node.parent, None, active, depth + 1)
        else:
            result = []
            under: Optional[list[Ground]] = None
            for layer in layers:
                if layer.kind == "image":
                    result.append(UNKNOWN_GROUND)
                    continue
                if all(stop[3] >= 1.0 for stop in layer.stops):
                    result.extend(_known(stop) for stop in layer.stops)
                    continue
                if under is None:
                    under = self.grounds(node.parent, None, active, depth + 1)
                for base in under:
                    result.extend(_composite(stop, base) for stop in layer.stops)
        self._cache[key] = result
        return result

    # -- whether this rule's colour is the one the reader sees -------------

    def wins_colour_on(
        self,
        node: Node,
        variant: Optional[frozenset[str]],
        active: frozenset[str],
        weight: tuple[int, int, int, int],
        pseudo: bool,
    ) -> bool:
        """False when a strictly stronger rule recolours this element.

        Only strictly stronger: an equal-specificity rule in the other
        stylesheet is still measured, so the <link> order cannot hide a pair.
        A ::before rule and the rule on the element it hangs off colour
        different boxes, so neither overrides the other.
        """
        if not pseudo and not active and "color" in _split_property_names(
            node.inline_style
        ):
            return False
        for steps, states, other, other_pseudo in self.foregrounds:
            if other_pseudo != pseudo or not states <= active:
                continue
            if other > weight and selector_matches(node, steps, variant):
                return False
        return True

    def grounds_under(self, base: Sequence[Ground], own: Sequence[Layer]) -> list[Ground]:
        """The grounds for a box that paints its own fill over the element."""
        if not own:
            return list(base)
        result: list[Ground] = []
        for layer in own:
            if layer.kind == "image":
                result.append(UNKNOWN_GROUND)
                continue
            if all(stop[3] >= 1.0 for stop in layer.stops):
                result.extend(_known(stop) for stop in layer.stops)
                continue
            for under in base:
                result.extend(_composite(stop, under) for stop in layer.stops)
        return result


def _split_property_names(style: str) -> set[str]:
    return {prop for prop, _, _ in _split_declarations(style)}


# ------------------------------------------------------------------ the pairs

_GROUND_SAMPLES = 17


def _ratio_band(
    foreground: RGBA, ground: Ground
) -> tuple[float, float, tuple[int, int, int]]:
    """Worst and best contrast this foreground can have on this ground.

    An uncertain ground is sampled along the ramp between its two ends, which
    is exactly the set of colours the stack produces over a grey backdrop. A
    colour photograph lies off that ramp, which is why a band that straddles
    the threshold is reported as unresolved rather than rounded either way.
    """
    if ground.certain:
        painted = flatten(foreground, ground.low)
        value = contrast_ratio(painted, ground.low)
        return value, value, ground.low
    worst = (float("inf"), ground.low)
    best = 0.0
    for step in range(_GROUND_SAMPLES):
        share = step / (_GROUND_SAMPLES - 1)
        base = tuple(
            int(round(ground.low[i] + (ground.high[i] - ground.low[i]) * share))
            for i in range(3)
        )
        value = contrast_ratio(flatten(foreground, base), base)
        if value < worst[0]:
            worst = (value, base)  # type: ignore[assignment]
        best = max(best, value)
    return worst[0], best, worst[1]


@dataclass(frozen=True)
class Pair:
    rule: Rule
    selector: str
    foreground: tuple[int, int, int]
    background: tuple[int, int, int]
    ratio: float  # the worst the stylesheets allow
    best: float  # the best they allow; equal to ratio when the ground is known
    where: str  # the template the worst case was found on

    @property
    def verdict(self) -> str:
        if self.best < MINIMUM_RATIO:
            return "fail"
        if self.ratio >= MINIMUM_RATIO:
            return "pass"
        return "unresolved"

    def describe(self) -> str:
        ground = (
            "#%02x%02x%02x" % self.background
            if self.verdict != "unresolved"
            else "a ground that runs to #%02x%02x%02x" % self.background
        )
        return "%s:%d  %s  #%02x%02x%02x on %s is %.2f:1 (%s)" % (
            (self.rule.stylesheet, self.rule.line, self.rule.selector)
            + self.foreground
            + (ground, self.ratio, self.where)
        )


def all_pairs(rules: Sequence[Rule]) -> list[Pair]:
    """Every rule that writes words, measured where the templates put it.

    A rule is measured on each element it reaches, under each branch of a
    template conditional that can put its classes there, and in each state the
    reader can hold that element in. The worst of those is the pair, because
    the worst is the one a patient can actually be looking at.
    """
    dom = template_dom()
    painter = Painter(rules, dom)
    pairs: list[Pair] = []
    for rule in rules:
        declared = None
        important = False
        for prop, raw, flag in rule.declarations:
            if prop == "color":
                declared = resolve_vars(raw, painter.variables)
                important = flag
        if declared is None:
            continue
        foregrounds = colours_in(declared)
        if not foregrounds:
            continue
        foreground = foregrounds[-1]
        for selector in rule.selectors:
            steps = split_selector(selector)
            own_specificity = specificity(selector)
            weight = (int(important),) + own_specificity
            needs = selector_states(steps)
            pseudo = bool(steps and steps[-1][1].pseudo_elements)
            own = (
                _layers_from(rule.declarations, painter.variables, own_specificity)
                if pseudo
                else []
            )
            worst: Optional[tuple[float, float, tuple[int, int, int], str]] = None
            for node in dom.matching(steps):
                for variant in node.class_sets:
                    if not selector_matches(node, steps, variant):
                        continue
                    for active in painter.states_on(node, variant):
                        if not needs <= active:
                            continue
                        if not painter.wins_colour_on(
                            node, variant, active, weight, pseudo
                        ):
                            continue
                        base = painter.grounds(node, variant, active)
                        for ground in painter.grounds_under(base, own):
                            low, high, seen = _ratio_band(foreground, ground)
                            if worst is None or (low, high) < (worst[0], worst[1]):
                                worst = (low, high, seen, node.template)
            if worst is None:
                continue
            pairs.append(
                Pair(
                    rule=rule,
                    selector=selector,
                    foreground=flatten(foreground, worst[2]),
                    background=worst[2],
                    ratio=worst[0],
                    best=worst[1],
                    where=worst[3],
                )
            )
    return pairs


def failing_pairs(rules: Sequence[Rule]) -> list[Pair]:
    return [pair for pair in all_pairs(rules) if pair.verdict == "fail"]


def unresolved_pairs(rules: Sequence[Rule]) -> list[Pair]:
    return [pair for pair in all_pairs(rules) if pair.verdict == "unresolved"]


# -------------------------------------------------------------- the exceptions


@dataclass(frozen=True)
class Exempt:
    """One rule left under 4.5:1, and why.

    `selector` is the selector exactly as the stylesheet writes it, so editing
    the rule forces this line to be looked at again.
    """

    stylesheet: str
    selector: str
    reason: str
    fixed_by: str = ""


BRAND_PINK = (
    "Brand pink #f02eaa on hover, 3.4:1 on the tinted press panel. Moving the "
    "brand pink is a brand decision, not a side effect of the button sweep."
)
FACEBOOK_BLUE = (
    "Facebook's own blue #1877F2 at 4.23:1. A share button has to wear the "
    "platform's colour to be recognised, and the rule that keeps every share "
    "button's label white on focus inherits the same measurement."
)
WHITE_ON_BRAND_LIME = (
    "White label on the brand lime sweep: 1.57:1 at #b8dd00, 2.78:1 at "
    "#8aa618, against the 4.5:1 this gate asks for. Product owner decision of "
    "2026-09-13, made on rendered options with the measurement in hand: the "
    "lime stays and the label stays white, because it matches the white of "
    "the hero title. Pending a swap of --fhi-btn-ink to a dark ink, which is "
    "one value in custom.css: hero plum #2b0f3d at 10.80:1 to 6.10:1, or "
    "near-black #1a1a1a at 11.09:1 to 6.26:1. Excused, not unmeasured: these "
    "rules still come out of the gate as failures, and the day the ink moves "
    "this list gets shorter."
)

EXCEPTIONS: tuple[Exempt, ...] = (
    Exempt(
        "custom.css",
        ".hidden-error-message",
        "Form error text in plain red, 4.00:1. Recolouring error states "
        "belongs with the pass that reworks form errors.",
    ),
    Exempt(
        "custom.css",
        ".testimonial-quote::before",
        "Decorative quotation mark, carries no words.",
    ),
    Exempt(
        "custom.css",
        ".testimonial-card::before",
        "Decorative star row above a testimonial, carries no words.",
    ),
    Exempt("custom.css", ".share-btn-facebook", FACEBOOK_BLUE),
    Exempt(
        "custom.css",
        ".share-btn:hover, .share-btn:focus, .share-btn:active",
        FACEBOOK_BLUE,
    ),
    Exempt(
        "custom.css",
        ".featured-title-link:hover, .featured-title-link:focus",
        BRAND_PINK,
    ),
    Exempt(
        "custom.css",
        ".featured-see-all-link:hover, .featured-see-all-link:focus",
        BRAND_PINK,
    ),
    Exempt(
        "custom.css",
        ".media-reference-headline a:hover, .media-reference-headline a:focus",
        BRAND_PINK,
    ),
    Exempt(
        "custom.css",
        ".media-reference-link:hover, .media-reference-link:focus",
        BRAND_PINK,
    ),
    Exempt("custom.css", ".btn-green, .btn-green:focus", WHITE_ON_BRAND_LIME),
    Exempt("custom.css", ".btn-green:hover", WHITE_ON_BRAND_LIME),
    Exempt(
        "custom.css",
        ".btn-green:hover, .btn-green:focus, .btn-green:active, "
        ".section-btn:hover, .section-btn:focus, .section-btn:active",
        WHITE_ON_BRAND_LIME,
    ),
    Exempt("custom.css", ".btn-delete, .pro-submit-btn", WHITE_ON_BRAND_LIME),
    Exempt(
        "custom.css", ".btn-delete:hover, .pro-submit-btn:hover", WHITE_ON_BRAND_LIME
    ),
    Exempt(
        "custom.css",
        ".section-btn, .section-btn.btn.btn-default.smoothScroll",
        WHITE_ON_BRAND_LIME,
    ),
    Exempt(
        "custom.css",
        ".section-btn:hover, .section-btn.btn.btn-default.smoothScroll:hover",
        WHITE_ON_BRAND_LIME,
    ),
    Exempt("main.css", ".section-btn", WHITE_ON_BRAND_LIME),
)


# ------------------------------------------------------------ the unresolved

HERO_PHOTOGRAPH = (
    "Words written straight onto the hero photograph "
    "(static/images/crinkledcolors-optimized.jpg). No stylesheet says what "
    "colour a photograph is, so this belongs to a rendered check, not here."
)
HERO_VEIL = (
    "A translucent panel over the hero photograph. The veil is thin enough "
    "that the ratio still depends on the picture underneath, so the source "
    "cannot settle it either way."
)
EVERY_LINK = (
    "Applies to every anchor on the site, including the ones the hero draws "
    "over the photograph, so no single ground can be resolved for it. Links "
    "on a page's own surfaces are measured through the rules that colour them."
)

UNRESOLVED: tuple[Exempt, ...] = (
    Exempt("custom.css", ".hero-inner", HERO_PHOTOGRAPH),
    Exempt("custom.css", ".hero-headline", HERO_PHOTOGRAPH),
    Exempt("custom.css", ".hero-tagline", HERO_PHOTOGRAPH),
    Exempt("custom.css", ".hero-subcopy", HERO_PHOTOGRAPH),
    Exempt("custom.css", ".secondary-cta, .tertiary-cta", HERO_VEIL),
    Exempt("custom.css", ".how-step", HERO_VEIL),
    Exempt("custom.css", ".how-step:not(:last-child)::after", HERO_VEIL),
    Exempt("custom.css", ".how-step h6", HERO_VEIL),
    Exempt("custom.css", ".how-step p", HERO_VEIL),
    Exempt("custom.css", ".how-it-works-intro", HERO_PHOTOGRAPH),
    Exempt("custom.css", ".trust-chip", HERO_VEIL),
    Exempt("custom.css", ".trust-chip-link:hover, .trust-chip-link:focus", HERO_VEIL),
    Exempt("main.css", "a", EVERY_LINK),
    Exempt("main.css", "a:hover, a:active, a:focus", EVERY_LINK),
    Exempt("main.css", "#home h1", HERO_PHOTOGRAPH),
    Exempt("main.css", "#home h3", HERO_PHOTOGRAPH),
    Exempt("main.css", ".slider .caption", HERO_PHOTOGRAPH),
)


# -------------------------------------------------------------- the unreached

BOUGHT_THEME = (
    "Selector appears on no page the gate can build: leftover from the bought "
    "theme. Deleting the unused half of main.css is its own change."
)
WRITTEN_AT_RUNTIME = (
    "The class is put on by script or by Django's form rendering, not by a "
    "template, so the gate can read the colour but not the ground under it. "
    "Wants a rendered check."
)

UNREACHED: tuple[Exempt, ...] = (
    Exempt(
        "custom.css",
        ".pwyw-pill",
        "No template carries this class; it is the pill the pay-what-you-want "
        "panel used before the panel was rebuilt.",
    ),
    Exempt("custom.css", ".appeal-phase-label-active", WRITTEN_AT_RUNTIME),
    Exempt("custom.css", ".appeal-phase-label-done", WRITTEN_AT_RUNTIME),
    Exempt("custom.css", ".appeal-phase-label-skipped", WRITTEN_AT_RUNTIME),
    Exempt("custom.css", ".appeal-phase-label-pending", WRITTEN_AT_RUNTIME),
    Exempt(
        "custom.css",
        ".betaribbon",
        "The markup that carries it is commented out in base.html, so the "
        "ribbon is not on any page today.",
    ),
    Exempt("custom.css", ".pro-interest-form .errorlist", WRITTEN_AT_RUNTIME),
    Exempt("main.css", ".select-option", BOUGHT_THEME),
    Exempt("main.css", "header a", BOUGHT_THEME),
    Exempt("main.css", "header span", BOUGHT_THEME),
    Exempt("main.css", "header span i", BOUGHT_THEME),
    Exempt("main.css", ".navbar-default .navbar-brand", BOUGHT_THEME),
    Exempt("main.css", ".navbar-default .navbar-brand .fa", BOUGHT_THEME),
    Exempt("main.css", ".navbar-default .navbar-nav>.active>a", BOUGHT_THEME),
    Exempt("main.css", ".navbar-default .navbar-nav>.active>a:hover", BOUGHT_THEME),
    Exempt("main.css", ".navbar-default .navbar-nav>.active>a:focus", BOUGHT_THEME),
    Exempt("main.css", ".slider .item-first .pro-version-text a", BOUGHT_THEME),
    Exempt("main.css", ".slider .item-first .pro-version-text a:visited", BOUGHT_THEME),
    Exempt("main.css", ".team-contact-info a", BOUGHT_THEME),
    Exempt("main.css", "#service h2", BOUGHT_THEME),
    Exempt("main.css", "#service h4", BOUGHT_THEME),
    Exempt("main.css", "#service p", BOUGHT_THEME),
    Exempt("main.css", "#service .fa", BOUGHT_THEME),
    Exempt("main.css", ".news-categories li a", BOUGHT_THEME),
    Exempt("main.css", ".news-tags li a", BOUGHT_THEME),
    Exempt("main.css", "#appointment label", BOUGHT_THEME),
    Exempt("main.css", "#appointment button#cf-submit", BOUGHT_THEME),
    Exempt("main.css", "#appointment button#cf-submit:hover", BOUGHT_THEME),
    Exempt("main.css", ".contact-info .fa", BOUGHT_THEME),
    Exempt("main.css", ".stories-info span", BOUGHT_THEME),
    Exempt("main.css", ".copyright-text p", BOUGHT_THEME),
    Exempt("main.css", ".angle-up-btn a", BOUGHT_THEME),
    Exempt("main.css", ".angle-up-btn a:hover", BOUGHT_THEME),
    Exempt("main.css", ".social-icon li a", BOUGHT_THEME),
    Exempt("main.css", ".social-icon li a:hover", BOUGHT_THEME),
)


def _normalise(selector: str) -> str:
    return " ".join(selector.split())


def _keys(entries: Sequence[Exempt]) -> set[tuple[str, str]]:
    return {(entry.stylesheet, _normalise(entry.selector)) for entry in entries}


def unreached_selectors(rules: Sequence[Rule]) -> list[tuple[str, int, str]]:
    """Colour rules that reach no element on any page the templates build."""
    dom = template_dom()
    missing: list[tuple[str, int, str]] = []
    for rule in rules:
        if not any(prop == "color" for prop, _, _ in rule.declarations):
            continue
        for selector in rule.selectors:
            if not dom.matching(split_selector(selector)):
                missing.append((rule.stylesheet, rule.line, selector))
    return missing


# ----------------------------------------------------------------- the checks


def test_stylesheets_are_linked_in_the_order_the_gate_assumes() -> None:
    """The cascade order the resolver uses has to be the page's real order."""
    base = (TEMPLATE_DIR / "base.html").read_text()
    linked = re.findall(r"static\s+'css/([a-z.]+\.css)'", base)
    ours = [name for name in linked if name in STYLESHEETS]
    assert ours == list(STYLESHEETS), (
        "base.html links %r; the gate measures every declaration in both "
        "sheets, so a reorder is safe, but this list has to be kept honest." % (ours,)
    )


def test_the_gate_reads_the_ground_out_of_the_templates() -> None:
    """The relationship that broke this branch is in the templates, not the CSS.

    .pwyw-thanks is a bare class. Nothing in its selector says it renders
    inside the hero's dark panel, so a gate that reads only stylesheets scores
    it against an imagined white page and passes it at 6:1 while a patient who
    has just paid reads it at about 2:1.
    """
    dom = template_dom()
    landing = [
        node
        for node in dom.by_class.get("pwyw-thanks", [])
        if node.template == "landing_base.html"
    ]
    assert landing, (
        "the payment confirmation was not found on the home page; the "
        "template walk is not following {% include %} into partials"
    )
    painter = Painter(load_rules(), dom)
    grounds = painter.grounds(landing[0])
    assert grounds, "no ground at all was resolved for the confirmation"
    for ground in grounds:
        assert relative_luminance(ground.high) < 0.25, (
            "the hero's payment panel resolved to %s, which is not the dark "
            "veil custom.css paints there" % ground.describe()
        )


def test_every_text_pair_in_the_stylesheets_meets_wcag_aa() -> None:
    rules = load_rules()
    exempt = _keys(EXCEPTIONS)
    unexcused = [
        pair
        for pair in failing_pairs(rules)
        if (pair.rule.stylesheet, _normalise(pair.rule.selector)) not in exempt
    ]
    seen: set[tuple[str, str]] = set()
    lines = []
    for pair in unexcused:
        key = (pair.rule.stylesheet, pair.rule.selector)
        if key in seen:
            continue
        seen.add(key)
        lines.append("  " + pair.describe())
    assert not lines, (
        "%d rule(s) put words on a fill below %.1f:1. Darken the fill or the "
        "ink, or add the rule to EXCEPTIONS with a reason:\n%s"
        % (len(lines), MINIMUM_RATIO, "\n".join(lines))
    )


def test_nothing_is_passed_on_a_ground_the_gate_could_not_resolve() -> None:
    """An unresolvable ground is not a pass, and has to say so out loud.

    This is the hole the hand-written selector list had, wearing a different
    coat: a rule the gate cannot measure is a rule nobody has checked, and
    reporting it green is worse than reporting nothing.
    """
    named = _keys(UNRESOLVED)
    seen: set[tuple[str, str]] = set()
    lines = []
    for pair in unresolved_pairs(load_rules()):
        key = (pair.rule.stylesheet, _normalise(pair.rule.selector))
        if key in named or key in seen:
            continue
        seen.add(key)
        lines.append("  " + pair.describe())
    assert not lines, (
        "%d rule(s) sit on a ground the stylesheets and templates cannot pin "
        "down. Give the element a fill the source can read, or name the rule "
        "in UNRESOLVED with a reason and a rendered check:\n%s"
        % (len(lines), "\n".join(lines))
    )


def test_no_unresolved_entry_outlives_the_rule_it_excuses() -> None:
    """The unresolved list may only shrink, the same as EXCEPTIONS."""
    rules = load_rules()
    still = {
        (pair.rule.stylesheet, _normalise(pair.rule.selector))
        for pair in unresolved_pairs(rules)
    }
    stale = [
        "%s  %s" % (entry.stylesheet, _normalise(entry.selector))
        for entry in UNRESOLVED
        if (entry.stylesheet, _normalise(entry.selector)) not in still
    ]
    assert not stale, (
        "The gate can measure these now, or they are gone. Either way the "
        "line has to go:\n  %s" % "\n  ".join(stale)
    )


def test_every_colour_rule_the_gate_never_reached_is_named() -> None:
    """A rule that paints nothing is not a passing rule either."""
    rules = load_rules()
    named = _keys(UNREACHED)
    missing = sorted(
        {
            "%s:%d  %s" % (stylesheet, line, selector)
            for stylesheet, line, selector in unreached_selectors(rules)
            if (stylesheet, _normalise(selector)) not in named
        }
    )
    assert not missing, (
        "%d colour rule(s) reach no element on any page the templates build, "
        "so nothing measured them. Delete the rule, or name it in UNREACHED "
        "with a reason:\n  %s" % (len(missing), "\n  ".join(missing))
    )
    reached = {
        (stylesheet, _normalise(selector))
        for stylesheet, _, selector in unreached_selectors(rules)
    }
    stale = [
        "%s  %s" % (entry.stylesheet, _normalise(entry.selector))
        for entry in UNREACHED
        if (entry.stylesheet, _normalise(entry.selector)) not in reached
    ]
    assert not stale, (
        "These are on a page now, or they are gone. Either way the line has "
        "to go:\n  %s" % "\n  ".join(stale)
    )


def test_every_excuse_names_a_rule_that_still_exists() -> None:
    """A selector that is edited or deleted must not take its excuse with it."""
    present: set[tuple[str, str]] = set()
    for rule in load_rules():
        present.add((rule.stylesheet, _normalise(rule.selector)))
        for selector in rule.selectors:
            present.add((rule.stylesheet, _normalise(selector)))
    missing = [
        "%s  %s" % (entry.stylesheet, _normalise(entry.selector))
        for entry in EXCEPTIONS + UNRESOLVED + UNREACHED
        if (entry.stylesheet, _normalise(entry.selector)) not in present
    ]
    assert not missing, (
        "EXCEPTIONS, UNRESOLVED or UNREACHED names rules that are not in the "
        "stylesheets any more. Delete the line rather than leaving a stale "
        "excuse behind:\n  %s" % "\n  ".join(missing)
    )


def test_no_exception_is_kept_after_its_rule_starts_passing() -> None:
    """The list may only shrink."""
    failing = {
        (pair.rule.stylesheet, _normalise(pair.rule.selector))
        for pair in failing_pairs(load_rules())
    }
    stale = [
        "%s  %s" % (entry.stylesheet, _normalise(entry.selector))
        for entry in EXCEPTIONS
        if (entry.stylesheet, _normalise(entry.selector)) not in failing
    ]
    assert (
        not stale
    ), "These rules now meet %.1f:1, so their exception has to go:\n  %s" % (
        MINIMUM_RATIO,
        "\n  ".join(stale),
    )


def test_every_excuse_carries_a_reason() -> None:
    unexplained = [
        "%s  %s" % (entry.stylesheet, _normalise(entry.selector))
        for entry in EXCEPTIONS + UNRESOLVED + UNREACHED
        if len(entry.reason.strip()) < 20
    ]
    assert (
        not unexplained
    ), "An excuse without a reason is just a skipped test:\n  %s" % "\n  ".join(
        unexplained
    )


# ---------------------------------------------------- the brand button itself

# The one token every label on the lime reads, the classes that wear the lime,
# and the two inks recorded beside the token as the candidates for a swap.
INK_TOKEN = "--fhi-btn-ink"
BRAND_BUTTON_CLASSES = frozenset(
    ("btn-green", "section-btn", "btn-delete", "pro-submit-btn", "pwyw-pill")
)
DARK_INK_CANDIDATES = ("#2b0f3d", "#1a1a1a")
DECISION_DATE = "2026-09-13"
def brand_button_labels(rules: Sequence[Rule]) -> list[tuple[Rule, str, str]]:
    """Every rule that writes a label onto a button wearing the brand lime."""
    found: list[tuple[Rule, str, str]] = []
    for rule in rules:
        raw = None
        for prop, value, _ in rule.declarations:
            if prop == "color":
                raw = value
        if raw is None:
            continue
        for selector in rule.selectors:
            steps = split_selector(selector)
            if steps and steps[-1][1].classes & BRAND_BUTTON_CLASSES:
                found.append((rule, selector, raw))
                break
    return found


def test_a_brand_buttons_label_comes_from_one_token() -> None:
    """Keeping the white label has to stay cheap to reverse.

    Every label on the lime reads --fhi-btn-ink, so moving the whole site to a
    dark ink is one value in :root rather than a hunt through two stylesheets
    for the literals that used to be there.
    """
    labelled = brand_button_labels(load_rules())
    assert len(labelled) >= 6, (
        "only %d rules were found writing a label on a brand button, so the "
        "classes this check looks for have been renamed and it is no longer "
        "checking anything" % len(labelled)
    )
    literals = [
        "%s:%d  %s  color: %s" % (rule.stylesheet, rule.line, selector, raw.strip())
        for rule, selector, raw in labelled
        if "var(%s)" % INK_TOKEN not in raw
    ]
    assert not literals, (
        "these rules write a brand button's label as a literal, so a swap of "
        "%s would miss them:\n  %s" % (INK_TOKEN, "\n  ".join(literals))
    )


_RECORDED_INK = re.compile(r"(#[0-9a-fA-F]{3,6})\s+([\d.]+):1\s+([\d.]+):1")


def test_the_inks_recorded_beside_the_token_measure_what_they_claim() -> None:
    """The table beside the token names the candidates and their ratios.

    A number in a comment rots. These are re-measured against the fill the
    stylesheet actually carries, so the table is either right or this says so.
    """
    text = (CSS_DIR / "custom.css").read_text()
    variables = custom_properties(load_rules())
    stops = []
    for token in ("--fhi-btn-fill-a", "--fhi-btn-fill-b"):
        stop = parse_colour(variables[token])
        assert stop is not None, token
        stops.append(stop[:3])
    recorded = _RECORDED_INK.findall(text)
    assert len(recorded) >= 3, (
        "the ink table beside %s is gone. It is what saves the next person "
        "from deriving these ratios again." % INK_TOKEN
    )
    for ink, on_bright, on_dark in recorded:
        colour = parse_colour(ink)
        assert colour is not None, ink
        for claimed, stop in zip((on_bright, on_dark), stops):
            actual = contrast_ratio(colour[:3], stop)
            assert round(actual, 2) == float(claimed), (
                "the table says %s is %s:1 on #%02x%02x%02x; it is %.2f:1"
                % ((ink, claimed) + stop + (actual,))
            )
    named = {ink.lower() for ink, _, _ in recorded}
    for candidate in DARK_INK_CANDIDATES:
        assert candidate in named, (
            "%s is one of the two inks the owner named as the way out, and it "
            "is not in the table" % candidate
        )
        assert "/* %s: %s; */" % (INK_TOKEN, candidate) in text, (
            "%s is in the table but is not sitting under the token commented "
            "out, ready to be swapped in" % candidate
        )


def test_the_white_label_is_excused_by_a_dated_decision() -> None:
    """White on the lime fails, and the gate says whose call that was.

    An exception carrying a reason is honest. A gate that quietly stops
    measuring is not, so these rules stay in the failing set and stay listed,
    and every other failing pair on the site still fails the build.
    """
    rules = load_rules()
    brand = {
        (rule.stylesheet, _normalise(rule.selector))
        for rule, _, _ in brand_button_labels(rules)
    }
    failing = {
        (pair.rule.stylesheet, _normalise(pair.rule.selector))
        for pair in failing_pairs(rules)
    }
    excused = {
        (entry.stylesheet, _normalise(entry.selector)): entry for entry in EXCEPTIONS
    }
    brand_failing = sorted(brand & failing)
    assert brand_failing, (
        "no brand button measures as failing any more. If %s was swapped to a "
        "dark ink, take these entries out of EXCEPTIONS rather than leaving a "
        "stale excuse behind." % INK_TOKEN
    )
    for key in brand_failing:
        entry = excused.get(key)
        assert entry is not None, "%s  %s fails and is not excused" % key
        for wanted in (DECISION_DATE, INK_TOKEN) + DARK_INK_CANDIDATES:
            assert wanted in entry.reason, (
                "the excuse for %s  %s does not name %s. An exception has to "
                "say whose decision it was, when, and what reverses it."
                % (key + (wanted,))
            )
        assert "product owner" in entry.reason.lower(), (
            "the excuse for %s  %s does not say who decided" % key
        )


def test_the_button_fill_is_the_brand_lime_as_a_sweep() -> None:
    """The identity: lime, and a gradient rather than a flat panel."""
    variables = custom_properties(load_rules())
    stops = []
    for token in ("--fhi-btn-fill-a", "--fhi-btn-fill-b"):
        stop = parse_colour(variables[token])
        assert stop is not None, token
        red, green, blue = stop[:3]
        assert green > red > blue, "%s (%s) is not a lime" % (token, variables[token])
        stops.append(stop[:3])
    bright, dark = stops
    assert relative_luminance(bright) > relative_luminance(dark), (
        "--fhi-btn-fill-a is meant to be the bright end of the sweep and "
        "--fhi-btn-fill-b the dark one"
    )
    assert contrast_ratio(bright, dark) >= 1.5, (
        "the two stops are %.2f:1 apart, which reads as a flat fill rather "
        "than a sweep" % contrast_ratio(bright, dark)
    )


def test_the_decorative_greens_keep_their_colour() -> None:
    """The lime surfaces are the visual identity and do not move with the fill."""
    variables = custom_properties(load_rules())
    assert variables["--fhi-green"] == "#a5c422"
    assert variables["--fhi-lime"] == "#ADD100"
    assert variables["--fhi-olive"] == "#7B920A"


def test_the_site_has_a_focus_ring() -> None:
    rules = load_rules()
    variables = custom_properties(rules)
    rings = [
        rule
        for rule in rules
        if any(":focus-visible" in selector for selector in rule.selectors)
        and any(prop == "outline" for prop, _, _ in rule.declarations)
    ]
    assert rings, "no :focus-visible outline anywhere in the stylesheets"
    for rule in rings:
        for prop, value, _ in rule.declarations:
            if prop != "outline":
                continue
            colour = next(iter(colours_in(resolve_vars(value, variables))), None)
            assert colour is not None, rule.selector
            assert contrast_ratio(colour[:3], WHITE) >= 3.0, (
                "the focus ring is %s, which is under 3:1 on white" % value
            )


# Bootstrap 5.2's own `.btn:focus-visible {outline: 0}`, and the weight it
# carries. A bare `:focus-visible` is (0, 1, 0) and loses to it in either load
# order, which is how a ring can exist in the stylesheet and still never appear
# on Next, Continue or Confirm Deletion.
BOOTSTRAP_BUTTON_RESET = (0, 2, 0)


def test_the_focus_ring_outranks_bootstraps_button_reset() -> None:
    rules = load_rules()
    variables = custom_properties(rules)
    winners = []
    for rule in rules:
        outline = None
        for prop, value, _ in rule.declarations:
            if prop == "outline":
                outline = resolve_vars(value, variables)
        if outline is None or outline.strip().lower() in ("none", "0"):
            continue
        for selector in rule.selectors:
            steps = split_selector(selector)
            if "focus-visible" not in selector:
                continue
            if not any("btn" in compound.classes for _, compound in steps):
                continue
            if specificity(selector) >= BOOTSTRAP_BUTTON_RESET:
                winners.append((selector, outline))
    assert winners, (
        "no :focus-visible rule names .btn at Bootstrap's own weight %r, so "
        "`.btn:focus-visible {outline: 0}` wins and the ring never reaches a "
        "button" % (BOOTSTRAP_BUTTON_RESET,)
    )
    for selector, outline in winners:
        colour = next(iter(colours_in(outline)), None)
        assert colour is not None, selector
        assert contrast_ratio(colour[:3], WHITE) >= 3.0, (
            "%s draws its ring in %s, under 3:1 on white" % (selector, outline)
        )


def test_nothing_switches_the_focus_ring_off() -> None:
    """A link's :focus rule used to clear the outline site wide."""
    offenders = []
    for rule in load_rules():
        if not any(":focus" in selector for selector in rule.selectors):
            continue
        for prop, value, _ in rule.declarations:
            if prop == "outline" and value.strip().lower() in ("none", "0"):
                offenders.append(
                    "%s:%d %s" % (rule.stylesheet, rule.line, rule.selector)
                )
    assert not offenders, "focus outline turned off:\n  %s" % "\n  ".join(offenders)


# A button on a ground the source cannot read has to say where its edge is some
# other way, or a reader on a photograph cannot tell it is a button at all.
BOUNDARY_RATIO = 3.0
HERO_BUTTONS = ("primary-cta", "pwyw-submit")


def test_a_button_on_an_unreadable_ground_draws_its_own_edge() -> None:
    """WCAG 1.4.11: a control needs 3:1 against what is behind it.

    The hero's buttons sit on a photograph and on a dark panel over that
    photograph, and no stylesheet can say what colour either of those is. The
    darkened fill is 8:1 against white and under 3:1 against the dark panel, so
    the label became readable while the button itself stopped having a shape.
    A border at 3:1 against the button's own fill gives it an edge that shows
    whatever the picture behind it turns out to be.
    """
    rules = load_rules()
    dom = template_dom()
    painter = Painter(rules, dom)
    for name in HERO_BUTTONS:
        nodes = dom.by_class.get(name, [])
        assert nodes, "no template carries .%s any more" % name
        borders = _border_colours(rules, painter.variables, name)
        assert borders, (
            ".%s sits on the hero, where no stylesheet can say what is behind "
            "it, and declares no border. Its fill alone cannot carry the "
            "button's edge." % name
        )
        fills = [
            ground
            for node in nodes
            for ground in painter.grounds(node)
            if ground.certain
        ]
        assert fills, ".%s has no fill the gate can read" % name
        for selector, colour in borders:
            for fill in fills:
                edge = flatten(colour, fill.low)
                ratio = contrast_ratio(edge, fill.low)
                assert ratio >= BOUNDARY_RATIO, (
                    "%s draws its edge at %.2f:1 against its own fill "
                    "#%02x%02x%02x, under the %.1f:1 a control needs"
                    % ((selector, ratio) + fill.low + (BOUNDARY_RATIO,))
                )


def _border_colours(
    rules: Sequence[Rule], variables: dict[str, str], name: str
) -> list[tuple[str, RGBA]]:
    found: list[tuple[str, RGBA]] = []
    for rule in rules:
        value = None
        for prop, raw, _ in rule.declarations:
            if prop in ("border", "border-color"):
                value = resolve_vars(raw, variables)
        if value is None:
            continue
        for selector in rule.selectors:
            steps = split_selector(selector)
            if steps and name in steps[-1][1].classes:
                colour = next(iter(colours_in(value)), None)
                if colour is not None:
                    found.append((selector, colour))
    return found


def test_the_dead_submit_button_rule_is_gone() -> None:
    """It was unreachable, so it was deleted rather than excused."""
    for name in STYLESHEETS:
        assert ".cat-submit-button" not in (CSS_DIR / name).read_text(), name


def test_the_drafts_phase_list_gets_its_colours_from_the_stylesheet() -> None:
    """A gate on CSS can never see a colour the fetcher writes at runtime."""
    fetcher = (
        REPO_ROOT / "fighthealthinsurance" / "static" / "js" / "appeal_fetcher.ts"
    ).read_text()
    stylesheet = (CSS_DIR / "custom.css").read_text()
    for state in ("active", "done", "skipped", "pending"):
        name = "appeal-phase-label-%s" % state
        assert name in fetcher, "%s is not applied by appeal_fetcher.ts" % name
        assert ".%s" % name in stylesheet, "%s is not declared in custom.css" % name

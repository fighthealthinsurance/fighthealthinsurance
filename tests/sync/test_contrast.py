"""Text contrast gate for the site's own stylesheets.

The point of this file is that it is not a list of buttons. It parses every
rule in custom.css and main.css, works out what colour the words are and what
colour they sit on, and fails any pair under the WCAG AA ratio of 4.5:1. A
hand written list of selectors is what let white-on-lime survive on the only
button on the delete-your-data page, so the gate reads the stylesheets instead
of trusting anybody's memory of them.

What it cannot do is see the DOM. Two consequences, both deliberate:

* Where a rule sets a colour and neither it nor any of its declared ancestors
  sets an opaque background, the background is taken to be white. That is
  fail-closed for dark text: if dark text fails on white it fails on anything
  lighter. It is the opposite for light text, because white text is only ever
  written for a dark surface, and on this site that surface is the hero
  photograph, which no CSS parser can measure. Light text with no opaque fill
  anywhere in its chain is therefore reported as unresolved rather than
  guessed at, and belongs to the rendered checks, not to this gate.
* Everything else that is under 4.5:1 and is not being fixed here has to be
  named in EXCEPTIONS below, with a reason. The list can only shrink: an entry
  that no longer matches a rule fails, and so does an entry whose rule now
  passes.
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
    declarations: tuple[tuple[str, str], ...]

    @property
    def selectors(self) -> list[str]:
        return [s.strip() for s in self.selector.split(",") if s.strip()]


def _strip_comments(css: str) -> str:
    return re.sub(
        r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S
    )


def _split_declarations(body: str) -> tuple[tuple[str, str], ...]:
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

    out: list[tuple[str, str]] = []
    for part in parts:
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        prop = prop.strip().lower()
        value = re.sub(r"!\s*important\s*$", "", value.strip()).strip()
        if prop and value and not prop.startswith("//"):
            out.append((prop, value))
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
            if char == "}":
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
        for prop, value in rule.declarations:
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


# ----------------------------------------------------------- background stack

WHITE = (255, 255, 255)
BACKGROUND_PROPERTIES = ("background", "background-color", "background-image")
_PSEUDO = re.compile(r"::?[a-zA-Z-]+(\([^)]*\))?")


def _without_pseudo(selector: str) -> str:
    return _PSEUDO.sub("", selector).strip()


def _ancestors(selector: str) -> list[str]:
    parts = [p for p in re.split(r"\s+", selector) if p and p not in (">", "+", "~")]
    return [" ".join(parts[:count]) for count in range(len(parts) - 1, 0, -1)]


@dataclass
class Surface:
    """What a rule's words sit on, as far as the source can tell."""

    colours: tuple[tuple[int, int, int], ...]
    opaque: bool  # an opaque fill was found; nothing was assumed
    photographic: bool  # a background image is in the stack


class StyleIndex:
    """Declarations grouped by single selector, for background resolution."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.variables = custom_properties(rules)
        self.by_selector: dict[str, list[tuple[str, str]]] = {}
        for rule in rules:
            for selector in rule.selectors:
                self.by_selector.setdefault(selector, []).extend(rule.declarations)

    def _own_layers(self, selector: str) -> list[tuple[str, list[RGBA]]]:
        """Every background layer declared for exactly this selector.

        All of them, not just the winner: .section-btn is declared in both
        stylesheets, and which one wins depends on the <link> order in
        base.html. Measuring every declaration means a reorder cannot turn a
        readable button back into an unreadable one.
        """
        layers: list[tuple[str, list[RGBA]]] = []
        for prop, raw in self.by_selector.get(selector, []):
            if prop not in BACKGROUND_PROPERTIES:
                continue
            value = resolve_vars(raw, self.variables)
            if "url(" in value:
                layers.append(("image", []))
                continue
            found = colours_in(value)
            if found:
                layers.append(("colour", found))
            elif value.strip().lower() in ("none", "inherit", "initial", "unset"):
                layers.append(("none", []))
        return layers

    def _parent_surfaces(self, selector: str, depth: int) -> list[Surface]:
        for ancestor in _ancestors(selector):
            for candidate in (ancestor, _without_pseudo(ancestor)):
                if candidate and self._own_layers(candidate):
                    return self.surfaces_for(candidate, depth + 1)
        return [Surface(colours=(WHITE,), opaque=False, photographic=False)]

    def surfaces_for(self, selector: str, depth: int = 0) -> list[Surface]:
        """One Surface per background declaration that can reach this selector."""
        if depth > 6:
            return [Surface(colours=(WHITE,), opaque=False, photographic=False)]

        layers: list[tuple[str, list[RGBA]]] = []
        for candidate in (selector, _without_pseudo(selector)):
            if candidate:
                layers = self._own_layers(candidate)
                if layers:
                    break

        real = [layer for layer in layers if layer[0] != "none"]
        if not real:
            return self._parent_surfaces(selector, depth)

        surfaces: list[Surface] = []
        for kind, stops in real:
            if kind == "image":
                surfaces.append(
                    Surface(colours=(WHITE,), opaque=False, photographic=True)
                )
                continue
            if all(stop[3] >= 1.0 for stop in stops):
                surfaces.append(
                    Surface(
                        colours=tuple(flatten(stop, WHITE) for stop in stops),
                        opaque=True,
                        photographic=False,
                    )
                )
                continue
            for under in self._parent_surfaces(selector, depth):
                backdrop = under.colours[0]
                surfaces.append(
                    Surface(
                        colours=tuple(flatten(stop, backdrop) for stop in stops),
                        opaque=under.opaque,
                        photographic=under.photographic,
                    )
                )
        return surfaces


# ------------------------------------------------------------------ the pairs


@dataclass(frozen=True)
class Pair:
    rule: Rule
    selector: str
    foreground: tuple[int, int, int]
    background: tuple[int, int, int]
    ratio: float

    def describe(self) -> str:
        return "%s:%d  %s  #%02x%02x%02x on #%02x%02x%02x is %.2f:1" % (
            (self.rule.stylesheet, self.rule.line, self.rule.selector)
            + self.foreground
            + self.background
            + (self.ratio,)
        )


def measurable_pairs(rules: Sequence[Rule]) -> list[Pair]:
    """Every foreground/background pair the stylesheets fully determine."""
    index = StyleIndex(rules)
    pairs: list[Pair] = []
    for rule in rules:
        declared = None
        for prop, raw in rule.declarations:
            if prop == "color":
                declared = resolve_vars(raw, index.variables)
        if declared is None:
            continue
        foregrounds = colours_in(declared)
        if not foregrounds:
            continue
        foreground = foregrounds[-1]
        for selector in rule.selectors:
            candidates: list[tuple[int, int, int]] = []
            for surface in index.surfaces_for(selector):
                if surface.photographic:
                    continue
                if not surface.opaque and relative_luminance(foreground[:3]) > 0.5:
                    # Light text with no fill declared anywhere in the chain.
                    # The surface is off in a photograph the source cannot
                    # read; guessing white here only invents failures.
                    continue
                candidates.extend(surface.colours)
            if not candidates:
                continue
            worst = min(
                (
                    contrast_ratio(flatten(foreground, background), background),
                    background,
                )
                for background in candidates
            )
            pairs.append(
                Pair(
                    rule=rule,
                    selector=selector,
                    foreground=flatten(foreground, worst[1]),
                    background=worst[1],
                    ratio=worst[0],
                )
            )
    return pairs


def failing_pairs(rules: Sequence[Rule]) -> list[Pair]:
    return [pair for pair in measurable_pairs(rules) if pair.ratio < MINIMUM_RATIO]


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


DEAD_THEME_RULE = (
    "Selector appears in no template, .py or .ts: leftover from the bought "
    "theme. Deleting the unused half of main.css is its own change."
)
BRAND_PINK = (
    "Brand pink #f02eaa on hover, 3.70:1 on white. Moving the brand pink is a "
    "brand decision, not a side effect of the button sweep."
)

EXCEPTIONS: tuple[Exempt, ...] = (
    Exempt(
        "custom.css",
        ".how-step:not(:last-child)::after",
        "Arrow glyph between the hero steps, drawn over the hero photograph. "
        "Decoration, and the surface is not in the source.",
    ),
    Exempt(
        "custom.css",
        "#home .hero-inner .benefit-content strong",
        "Lime heading inside the hero card, over the hero photograph. Measured "
        "on screen, not from source.",
    ),
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
    Exempt(
        "custom.css",
        ".share-btn-facebook",
        "Facebook's own blue #1877F2 at 4.23:1. A share button has to wear the "
        "platform's colour to be recognised.",
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
    Exempt("main.css", ".select-option", DEAD_THEME_RULE),
    Exempt("main.css", "header span i", DEAD_THEME_RULE),
    Exempt("main.css", ".navbar-default .navbar-brand .fa", DEAD_THEME_RULE),
    Exempt("main.css", "#service h2,\n#service h4", DEAD_THEME_RULE),
    Exempt("main.css", "#service p", DEAD_THEME_RULE),
    Exempt("main.css", "#service .fa", DEAD_THEME_RULE),
    Exempt("main.css", "#appointment button#cf-submit", DEAD_THEME_RULE),
    Exempt("main.css", ".stories-info span", DEAD_THEME_RULE),
)


def _normalise(selector: str) -> str:
    return " ".join(selector.split())


def _exempt_keys() -> set[tuple[str, str]]:
    return {(e.stylesheet, _normalise(e.selector)) for e in EXCEPTIONS}


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


def test_every_text_pair_in_the_stylesheets_meets_wcag_aa() -> None:
    rules = load_rules()
    exempt = _exempt_keys()
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


def test_every_exception_names_a_rule_that_still_exists() -> None:
    """A selector that is edited or deleted must not take its excuse with it."""
    present = {(rule.stylesheet, _normalise(rule.selector)) for rule in load_rules()}
    missing = [
        "%s  %s" % (e.stylesheet, _normalise(e.selector))
        for e in EXCEPTIONS
        if (e.stylesheet, _normalise(e.selector)) not in present
    ]
    assert not missing, (
        "EXCEPTIONS names rules that are not in the stylesheets any more. "
        "Delete the line rather than leaving a stale excuse behind:\n  %s"
        % "\n  ".join(missing)
    )


def test_no_exception_is_kept_after_its_rule_starts_passing() -> None:
    """The list may only shrink."""
    failing = {
        (pair.rule.stylesheet, _normalise(pair.rule.selector))
        for pair in failing_pairs(load_rules())
    }
    stale = [
        "%s  %s" % (e.stylesheet, _normalise(e.selector))
        for e in EXCEPTIONS
        if (e.stylesheet, _normalise(e.selector)) not in failing
    ]
    assert (
        not stale
    ), "These rules now meet %.1f:1, so their exception has to go:\n  %s" % (
        MINIMUM_RATIO,
        "\n  ".join(stale),
    )


def test_every_exception_carries_a_reason() -> None:
    unexplained = [
        "%s  %s" % (e.stylesheet, _normalise(e.selector))
        for e in EXCEPTIONS
        if len(e.reason.strip()) < 20
    ]
    assert (
        not unexplained
    ), "An exception without a reason is just a skipped test:\n  %s" % "\n  ".join(
        unexplained
    )


def test_the_button_fill_carries_white_labels() -> None:
    """Submit, Next, Continue, Generate My Appeal and Choose This One."""
    variables = custom_properties(load_rules())
    for token in ("--fhi-btn-fill-a", "--fhi-btn-fill-b"):
        colour = parse_colour(variables[token])
        assert colour is not None, token
        ratio = contrast_ratio(WHITE, colour[:3])
        assert ratio >= MINIMUM_RATIO, "white on %s (%s) is only %.2f:1" % (
            token,
            variables[token],
            ratio,
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
        and any(prop == "outline" for prop, _ in rule.declarations)
    ]
    assert rings, "no :focus-visible outline anywhere in the stylesheets"
    for rule in rings:
        for prop, value in rule.declarations:
            if prop != "outline":
                continue
            colour = next(iter(colours_in(resolve_vars(value, variables))), None)
            assert colour is not None, rule.selector
            assert contrast_ratio(colour[:3], WHITE) >= 3.0, (
                "the focus ring is %s, which is under 3:1 on white" % value
            )


def test_nothing_switches_the_focus_ring_off_for_links() -> None:
    """A link's :focus rule used to clear the outline site wide."""
    offenders = []
    for rule in load_rules():
        if not any(":focus" in selector for selector in rule.selectors):
            continue
        for prop, value in rule.declarations:
            if prop == "outline" and value.strip().lower() in ("none", "0"):
                if any(
                    selector.strip().startswith("a:") for selector in rule.selectors
                ):
                    offenders.append(
                        "%s:%d %s" % (rule.stylesheet, rule.line, rule.selector)
                    )
    assert not offenders, "focus outline turned off for links:\n  %s" % "\n  ".join(
        offenders
    )


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

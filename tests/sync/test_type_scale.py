"""The type scale, and the floors underneath it.

The site had fifty different font sizes across its two stylesheets with no
relationship between them, its headings set in em so that a heading inside any
container that set a size of its own came out a size nobody chose, and its
running text at 14px. These tests hold the scale that replaced that, and the
two floors that matter more than the scale does:

  running text is 16px, because the people reading it are reading a denial
  letter and an appeal, not a landing page

  a text field is never under 16px, because iOS Safari zooms the page when a
  smaller field takes focus and does not zoom back out

Sizes here were measured on the rendered pages before the change and kept, so
the scale is a description of what the site already looked like at its best
rather than a new opinion about how big a heading should be.
"""

import re
from typing import Optional

from tests.sync.test_contrast import (
    CSS_DIR,
    TEMPLATE_DIR,
    resolve_vars,
    Rule,
    custom_properties,
    load_rules,
    load_template_rules,
)

BODY_PX = 16.0
INPUT_TOKEN = "--fhi-text-input"
INPUT_FLOOR_PX = 16.0
MICRO_FLOOR_PX = 12.0

# A bingo cell is a fixed square in a five-by-five grid and the words have to
# fit inside it. That is a layout constraint, not a choice about readability,
# and it is the only one on the site. Nothing in the appeal flow is here.
# Each entry names the exact sizes that were agreed, not the selector as a
# whole. A selector-wide exemption would also excuse a below-floor size added
# later to a different rule with the same selector, which is not what anyone
# decided.
BELOW_FLOOR_BY_DESIGN = {
    # A bingo cell is a fixed square in a five-by-five grid and the words have
    # to fit inside it. Two sizes: the cell, and the longer squares.
    ("custom.css", ".bingo-cell"): (11.2, 9.6),
    # The tagline over the hero title, 11px in caps inside the 768px media
    # query. Deliberately subtle, not something a patient has to read to use
    # the site, and the sentence is a joke rather than an instruction.
    # Product owner's call of 2026-09-14. The desktop rule for the same
    # selector is 14px and never reaches this list. Listed, not skipped: the
    # gate still measures it.
    ("main.css", "#home h3"): (11.0,),
}


def _excused(stylesheet: str, selector: str, size: float) -> bool:
    allowed = BELOW_FLOOR_BY_DESIGN.get((stylesheet, selector.strip()))
    return bool(allowed) and any(abs(size - a) < 0.05 for a in allowed)

HEADING_TOKENS = {
    "h1": "--fhi-text-hero",
    "h2": "--fhi-text-page",
    "h3": "--fhi-text-section",
    "h4": "--fhi-text-title",
}

# What each fluid heading has to come out at on a 390px phone and a 1280px
# desktop. These are the sizes the breakpoints produced before the change.
FLUID_ENDS = {
    "--fhi-text-page": (34.0, 48.0),
    "--fhi-text-section": (24.0, 28.8),
    "--fhi-text-title": (22.4, 28.8),
}

# An element name, not a class that happens to contain one: .select-option
# and .chat-role-select are not form controls.
TEXT_INPUT_SELECTOR = re.compile(
    r"input\[type=[\"']?(?:text|email|tel|number|search|password|date)[\"']?\]"
    r"|(?<![\w.-])(?:textarea|select)(?![\w-])"
    r"|\.form-control(?![\w-])"
)
_CLAMP = re.compile(r"clamp\(\s*([^,]+),\s*([^,]+),\s*([^)]+)\)")


def _px(value: str) -> Optional[float]:
    value = value.strip()
    match = re.fullmatch(r"([\d.]+)(px|rem|em)", value)
    if match is None:
        return None
    size = float(match.group(1))
    return size if match.group(2) == "px" else size * BODY_PX


def _font_size(rule: Rule) -> Optional[str]:
    found = None
    for prop, value, _ in rule.declarations:
        if prop == "font-size":
            found = value.strip()
    return found


def _clamp_at(value: str, viewport_px: float) -> Optional[float]:
    """What a clamp() comes out at, at one viewport width."""
    match = _CLAMP.search(value)
    if match is None:
        return None
    low, preferred, high = (g.strip() for g in match.groups())
    total = 0.0
    for term in re.findall(r"[\d.]+(?:vw|rem|px)?", preferred):
        if term.endswith("vw"):
            total += float(term[:-2]) / 100.0 * viewport_px
        elif term.endswith("rem"):
            total += float(term[:-3]) * BODY_PX
        elif term.endswith("px"):
            total += float(term[:-2])
    low_px, high_px = _px(low), _px(high)
    assert low_px is not None and high_px is not None, value
    return max(low_px, min(total, high_px))


def test_no_heading_is_sized_in_em() -> None:
    """em compounds. A heading in em is a size nobody chose.

    h1 was 5em. Inside a container that set 0.9rem it came out at 64px rather
    than 80px, and nothing in the rule said so.
    """
    offenders = []
    for rule in load_rules() + load_template_rules():
        value = _font_size(rule)
        if value is None or "var(" in value:
            continue
        if not re.search(r"(^|[\s,>])h[1-6]\b", rule.selector):
            continue
        if re.search(r"[\d.]+em\b", value) and "rem" not in value:
            offenders.append(
                "%s:%d  %s  font-size: %s" % (rule.stylesheet, rule.line, rule.selector, value)
            )
    assert not offenders, "headings sized in em:\n  %s" % "\n  ".join(offenders)


def test_each_heading_level_reads_its_token() -> None:
    """One place decides how big an h1 is."""
    seen: dict[str, str] = {}
    for rule in load_rules():
        value = _font_size(rule)
        if value is None:
            continue
        for selector in rule.selectors:
            if selector.strip() in HEADING_TOKENS:
                seen[selector.strip()] = value
    for tag, token in HEADING_TOKENS.items():
        assert tag in seen, "%s no longer sets a size anywhere" % tag
        assert "var(%s)" % token in seen[tag], (
            "%s is sized %s rather than reading %s, so the scale has a hole "
            "in it" % (tag, seen[tag], token)
        )


def test_the_fluid_headings_land_where_the_breakpoints_did() -> None:
    """The clamp has to reproduce the sizes it replaced, at both ends.

    Three breakpoint declarations per heading became one fluid value. That is
    only an improvement if the phone and the desktop still get what they got.
    """
    variables = custom_properties(load_rules())
    for token, (phone, desktop) in FLUID_ENDS.items():
        value = variables.get(token)
        assert value is not None, "%s is gone" % token
        at_phone = _clamp_at(value, 390.0)
        at_desktop = _clamp_at(value, 1280.0)
        assert at_phone is not None, "%s is not a clamp: %s" % (token, value)
        assert abs(at_phone - phone) < 0.6, (
            "%s comes out at %.1fpx on a 390px phone; it was %.1fpx"
            % (token, at_phone, phone)
        )
        assert abs(at_desktop - desktop) < 0.6, (
            "%s comes out at %.1fpx on a 1280px desktop; it was %.1fpx"
            % (token, at_desktop, desktop)
        )


# The hero title is the one heading that keeps discrete steps, and these are
# the sizes main.css stepped through before the scale existed. Fluid put it at
# about 79px in the middle of the range where it had always been 64px; it does
# not need to be larger than it was (product owner, 2026-09-13).
HERO_STEPS = ((None, 80.0), (1200, 64.0), (768, 48.0))


def hero_sizes_by_media() -> dict:
    """Every --fhi-text-hero declaration, keyed by the @media it sits inside.

    None is the key for the one declared at the top level. The condition has
    to come from the raw text: the rule parser flattens media queries away,
    which is exactly the information this test is about.
    """
    text = (CSS_DIR / "custom.css").read_text()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    found: dict = {}
    stack: list = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            head = text[max(0, text.rfind("}", 0, index)) : index]
            head = head[head.rfind(";") + 1 :]
            condition = None
            if "@media" in head:
                match = re.search(r"max-width:\s*(\d+)px", head)
                condition = int(match.group(1)) if match else -1
            stack.append(condition)
        elif char == "}":
            if stack:
                stack.pop()
        elif text.startswith("--fhi-text-hero:", index):
            value = text[index + len("--fhi-text-hero:") :]
            value = value[: value.index(";")]
            media = next((c for c in reversed(stack) if c is not None), None)
            found[media] = _px(value)
        index += 1
    return found


def test_the_hero_title_keeps_its_phone_line_height() -> None:
    """Size is not the only thing that has to match.

    main.css set line-height 1.2 on h1 at the phone breakpoint and nowhere
    else. Dropping it left a 48px title in a 72px line box rather than a
    57.6px one, which grows the hero by 43px on the width where there is
    least room for it. The font sizes matched the whole time, so nothing that
    only checks font-size would have seen this.
    """
    text = (CSS_DIR / "main.css").read_text()
    phone = text[text.index("max-width: 768px") :]
    phone = phone[: phone.index("\n}\n")]
    assert re.search(r"h1\s*\{[^}]*line-height:\s*1\.2", phone), (
        "h1 has no line-height at the phone breakpoint, so it inherits 1.5 "
        "and the hero title takes a taller line box than it did on main"
    )


def test_the_hero_title_is_no_larger_than_it_ever_was() -> None:
    """One token, three widths, the same sizes the breakpoints gave."""
    sizes = hero_sizes_by_media()
    for width, expected in HERO_STEPS:
        assert width in sizes, (
            "the hero title has no size for the %s case, so it is whatever the "
            "next rule up says there"
            % ("widest" if width is None else "%dpx and below" % width)
        )
        assert sizes[width] == expected, (
            "the hero title is %.0fpx at %s; it was %.0fpx, and it does not "
            "need to be larger"
            % (
                sizes[width],
                "full width" if width is None else "%dpx and below" % width,
                expected,
            )
        )


def test_running_text_is_sixteen_pixels() -> None:
    """The body rule sets what most of the words on the site are set in."""
    variables = custom_properties(load_rules())
    body = _px(variables.get("--fhi-text-body", ""))
    assert body == BODY_PX, (
        "--fhi-text-body is %s, not %.0fpx. The p rule reading the token means "
        "nothing if the token is small." % (variables.get("--fhi-text-body"), BODY_PX)
    )
    for rule in load_rules():
        if rule.selector.strip() == "p":
            value = _font_size(rule)
            assert value is not None and "--fhi-text-body" in value, (
                "the p rule is sized %s. Running text on this site is a denial "
                "letter and an appeal, and it reads at the body size." % value
            )
            return
    raise AssertionError("the p rule is gone")


def test_no_text_field_is_small_enough_to_make_ios_zoom() -> None:
    """A field under 16px zooms iOS Safari on focus, and it stays zoomed.

    The patient is left panning a page sideways in the middle of typing an
    appeal. Nothing about a field's size is worth that.

    The first version of this test was worthless: it skipped every value
    containing var(), which is every value the fix writes, so the whole rule
    could be deleted or the token set to 0.5rem and it still passed. It
    resolves the token now, and requires the rule that sets the floor to
    exist, because a floor nothing declares is not a floor.
    """
    rules = load_rules()
    variables = custom_properties(rules)
    declared = variables.get(INPUT_TOKEN)
    assert declared is not None, "%s is gone from :root" % INPUT_TOKEN
    floor = _px(declared)
    assert floor is not None and floor >= INPUT_FLOOR_PX, (
        "%s is %s, under the %.0fpx a text field needs to stop iOS zooming "
        "the page on focus" % (INPUT_TOKEN, declared, INPUT_FLOOR_PX)
    )

    reached = []
    offenders = []
    for rule in rules + load_template_rules():
        value = _font_size(rule)
        if value is None or not TEXT_INPUT_SELECTOR.search(rule.selector):
            continue
        resolved = _px(resolve_vars(value, variables))
        if resolved is None:
            continue
        reached.append(rule.selector)
        if resolved < INPUT_FLOOR_PX:
            offenders.append(
                "%s:%d  %s  font-size: %s (%.1fpx)"
                % (rule.stylesheet, rule.line, rule.selector, value.strip(), resolved)
            )
    assert reached, (
        "no rule sizes a text field any more, so the floor is not applied to "
        "anything and this test is measuring nothing"
    )
    assert not offenders, (
        "these set a text field under %.0fpx:\n  %s"
        % (INPUT_FLOOR_PX, "\n  ".join(offenders))
    )


def test_nothing_is_set_below_the_floor() -> None:
    """--fhi-text-micro is the floor, on every surface that carries type.

    Reading only the two stylesheets let the appeal flow keep two sizes under
    it: the step names in partials/flow_progress.html at 11px, and the wait
    times appeal_fetcher.ts writes inline at 0.72rem. Both are in front of a
    patient while they wait for a letter.
    """
    variables = custom_properties(load_rules())
    offenders = []
    for rule in load_rules() + load_template_rules():
        value = _font_size(rule)
        if value is None:
            continue
        size = _px(resolve_vars(value, variables))
        if size is not None and _excused(rule.stylesheet, rule.selector, size):
            continue
        if size is not None and size < MICRO_FLOOR_PX:
            offenders.append(
                "%s:%d  %s  font-size: %s (%.1fpx)"
                % (rule.stylesheet, rule.line, rule.selector, value, size)
            )
    for path, size, snippet in _inline_font_sizes(variables):
        if size < MICRO_FLOOR_PX:
            offenders.append("%s  %s (%.1fpx)" % (path, snippet, size))
    assert not offenders, (
        "these are set below the %.0fpx floor:\n  %s"
        % (MICRO_FLOOR_PX, "\n  ".join(offenders))
    )


_INLINE_SIZE = re.compile(r"font-size:\s*([^;\"'}]+)")
_SCANNED_FOR_INLINE = ("fighthealthinsurance/templates", "fighthealthinsurance/static/js")


def _inline_font_sizes(variables: dict):
    """Sizes written straight into markup or into a JS template string.

    appeal_fetcher.ts builds the progress list as HTML in a string, so its
    sizes never appear in a stylesheet or in a style block. That is where the
    wait-time text sat at 0.72rem, in front of a patient waiting for a letter.
    """
    root = TEMPLATE_DIR.parent.parent
    for folder in _SCANNED_FOR_INLINE:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            # dist/workers/tesseract.js is a directory, not a file, and CI
            # builds it. A suffix check alone tried to read it and raised.
            if not path.is_file():
                continue
            if path.suffix not in (".html", ".ts", ".js"):
                continue
            if {"node_modules", "dist"} & set(path.parts):
                continue
            text = path.read_text(errors="replace")
            for match in _INLINE_SIZE.finditer(text):
                raw = match.group(1).strip()
                # a <style> block is parsed properly elsewhere; only take the
                # ones inside an attribute or a string
                line_start = text.rfind("\n", 0, match.start()) + 1
                line = text[line_start : text.find("\n", match.start())]
                if "style=" not in line and "`" not in line and "'" not in line and '"' not in line:
                    continue
                size = _px(resolve_vars(raw, variables))
                if size is None:
                    continue
                yield (
                    "%s:%d" % (path.relative_to(root).as_posix(), text[: match.start()].count("\n") + 1),
                    size,
                    "font-size: %s" % raw,
                )


_TOKEN_REF = re.compile(r"var\(\s*(--fhi-[a-z0-9-]+)")


def test_a_standalone_page_declares_every_token_it_uses() -> None:
    """A page that loads no stylesheet cannot read a token from one.

    This has now bitten twice. The error pages wrote their button label as
    var(--fhi-btn-ink) while loading nothing that defines it, and three more
    pages picked up var(--fhi-text-input) the same way. In both cases the
    property is invalid at computed-value time and the element quietly
    inherits instead, which looks fine until the inherited value is wrong.

    A template is allowed to use a token if it extends a base that loads the
    stylesheets, or if it declares the token itself.
    """
    included = set()
    for path in TEMPLATE_DIR.rglob("*.html"):
        for name in re.findall(
            r"{%\s*include\s+[\"']([^\"']+)", path.read_text(errors="replace")
        ):
            included.add(name)
    offenders = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = path.read_text(errors="replace")
        referenced = set(_TOKEN_REF.findall(text))
        if not referenced:
            continue
        # A page that extends a base, loads the stylesheet itself, or is only
        # ever rendered inside one of those, can see the token block.
        if "{% extends" in text or "custom.css" in text:
            continue
        if path.relative_to(TEMPLATE_DIR).as_posix() in included:
            continue
        for token in sorted(referenced):
            if re.search(re.escape(token) + r"\s*:", text):
                continue
            offenders.append(
                "%s uses %s and loads nothing that defines it"
                % (path.relative_to(TEMPLATE_DIR).as_posix(), token)
            )
    assert not offenders, (
        "these pages reference a token they cannot see, so the declaration is "
        "dropped and the element inherits instead:\n  %s" % "\n  ".join(offenders)
    )

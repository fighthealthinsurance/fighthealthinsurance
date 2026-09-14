"""The spacing scale, and a baseline that lets it spread without a rewrite.

The two stylesheets carried 75 distinct spacing values across 355
declarations, 134 of them off any 4px grid: 3px, 5px, 10px, 11px, 15px and
19px sitting beside each other with nothing to say which was meant and which
was a typo that stuck.

Converting all 355 in one change would be an enormous diff that no one could
review, and the predictable result of that is two spacing systems running side
by side for good: tokens in the parts someone got to, literals everywhere
else, and contributors copying whichever example they happened to open.

So this does two things instead. The shared shell, which every page on the
site renders, is migrated completely and is not allowed to drift back. The
rest is counted. The count may fall and may not rise, which means a new
off-scale value fails the build while the existing backlog is worked off at
whatever pace the surrounding work allows.
"""

import re

from tests.sync.test_contrast import Rule, custom_properties, load_rules

BASE_PX = 4.0
REM_PX = 16.0

# margin-inline-start and friends space things exactly like margin-left does,
# and a negative length is still a length. Leaving either out of the pattern
# let new off-grid spacing past the ratchet entirely.
SPACING_PROPERTY = re.compile(
    r"^(margin|padding|gap|row-gap|column-gap|inset)"
    r"(-(top|right|bottom|left|start|end|block|inline|block-start|block-end"
    r"|inline-start|inline-end|x|y))?$"
)
# Every length in the value, not only whole whitespace-separated tokens: a
# length inside calc() spaces things exactly like a bare one does, and CSS
# units are case-insensitive, so `calc(3px + 1rem)` and `10PX` both used to
# walk past the ratchet untouched.
LENGTH = re.compile(r"(-?[\d.]+)(px|rem|em)\b", re.IGNORECASE)

# Every page renders this. It is migrated, and a literal here is a regression.
SHELL_SELECTOR = re.compile(
    r"(^|[\s,>])(header|\.navbar|\.nav(?![\w-])|#nav(?![\w-])"
    r"|footer(?![\w-])|\.footer|\.copyright)"
)

# The nav call to action is spaced by the button scale, not this one: its
# padding is what gives it a 44px touch target, and 11.2px/17.6px are the
# sizes that produce it. Naming it here keeps it out of the shell check
# without weakening the check for everything else.
SHELL_SPACED_BY_THE_BUTTON_SCALE = frozenset(
    (".navbar-default .navbar-nav li.appointment-btn a",)
)

# 10px is exactly between two steps and this row holds nine links across.
# Rounding down narrows nine tap targets including Delete your Data; rounding
# up widens the row by 36px and wraps the last link onto a second line.
# Neither is worth a tidier number, so it keeps its literal and stays in the
# backlog like any other value that has not been converted.
SHELL_KEEPS_ITS_LITERAL = frozenset((".footer-link a",))

# Off-scale spacing declarations still in each stylesheet, counted on
# 2026-09-13. Lower these as the values are migrated; the test refuses to let
# them grow, and refuses to let a stale number sit above what is really there.
BASELINE = {"custom.css": 71, "main.css": 48}


def _lengths(value: str):
    """Every length in one declaration value, in pixels."""
    for number, unit in LENGTH.findall(value):
        size = float(number)
        yield size if unit.lower() == "px" else size * REM_PX


def _px(value: str):
    """The single length in a value, or None when it is not exactly one."""
    found = list(_lengths(value))
    return found[0] if len(found) == 1 else None


def _spacing_values(rule: Rule):
    for prop, value, _ in rule.declarations:
        if not SPACING_PROPERTY.match(prop):
            continue
        for token in value.split():
            yield prop, token.strip()


def _spacing_lengths(rule: Rule):
    """Every length any spacing property on this rule sets, calc() included."""
    for prop, value, _ in rule.declarations:
        if not SPACING_PROPERTY.match(prop):
            continue
        for size in _lengths(value):
            yield prop, value.strip(), size


def test_the_scale_is_a_four_pixel_grid() -> None:
    """Every step is a whole number of 4px, and they ascend."""
    variables = custom_properties(load_rules())
    steps = []
    for index in range(1, 12):
        value = variables.get("--fhi-space-%d" % index)
        assert value is not None, "--fhi-space-%d is missing" % index
        size = _px(value)
        assert size is not None, "--fhi-space-%d is %s" % (index, value)
        assert size % BASE_PX == 0, (
            "--fhi-space-%d is %.1fpx, which is not a multiple of %.0f. The "
            "point of the base is that every step lands on it."
            % (index, size, BASE_PX)
        )
        steps.append(size)
    assert steps == sorted(steps), "the scale does not ascend: %s" % steps
    assert len(set(steps)) == len(steps), "two steps are the same size: %s" % steps


def test_the_shared_shell_spaces_itself_from_the_scale() -> None:
    """The header, nav and footer are migrated, and stay migrated."""
    offenders = []
    for rule in load_rules():
        selector = " ".join(rule.selector.split())
        if not SHELL_SELECTOR.search(selector):
            continue
        if selector in SHELL_SPACED_BY_THE_BUTTON_SCALE:
            continue
        if selector in SHELL_KEEPS_ITS_LITERAL:
            continue
        for prop, token in _spacing_values(rule):
            size = _px(token)
            if size is None or size == 0:
                continue
            offenders.append(
                "%s:%d  %s  %s: %s"
                % (rule.stylesheet, rule.line, rule.selector, prop, token)
            )
    assert not offenders, (
        "the shell is on the scale and these put a literal back into it. Use "
        "the nearest --fhi-space step:\n  %s" % "\n  ".join(sorted(set(offenders)))
    )


def off_scale_counts() -> dict:
    counts = {name: 0 for name in BASELINE}
    for rule in load_rules():
        if rule.stylesheet not in counts:
            continue
        for _prop, _value, size in _spacing_lengths(rule):
            if size == 0:
                continue
            if abs(size) % BASE_PX != 0:
                counts[rule.stylesheet] += 1
    return counts


def test_no_stylesheet_adds_an_off_scale_spacing_value() -> None:
    """The backlog may fall. It may not rise."""
    actual = off_scale_counts()
    grown = [
        "%s: %d now, %d allowed" % (name, count, BASELINE[name])
        for name, count in sorted(actual.items())
        if count > BASELINE[name]
    ]
    assert not grown, (
        "these stylesheets gained spacing values that are not a multiple of "
        "%.0fpx. The scale is in :root as --fhi-space-1 through "
        "--fhi-space-11:\n  %s" % (BASE_PX, "\n  ".join(grown))
    )


def test_the_spacing_baseline_has_no_stale_numbers() -> None:
    """A number above what is really there stops holding anything down."""
    actual = off_scale_counts()
    stale = [
        "%s: allowed %d, only %d left" % (name, BASELINE[name], actual[name])
        for name in sorted(BASELINE)
        if actual[name] < BASELINE[name]
    ]
    assert not stale, (
        "lower these to what the stylesheet actually has, so the backlog "
        "cannot quietly grow back into the headroom:\n  %s" % "\n  ".join(stale)
    )

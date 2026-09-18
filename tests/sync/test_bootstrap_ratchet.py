"""Bootstrap can only shrink from here.

The site is built on Bootstrap 5 and the owner wants it gone, which is a long
job: its grid alone is used four hundred times across a hundred and twenty six
templates, and its JavaScript still drives the header. A long job needs a
mechanism rather than a resolution, so this is the mechanism. It counts what
is there today, per class, and fails when a count goes up.

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

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"

# Counted on 2026-09-18, after the input box moved to the site's own class.
# Lower these as uses go; never raise one.
BASELINE = {
    "form-control": 7,
    "form-check-input": 6,
    "card": 139,
    "btn": 125,
    "row": 140,
    "col-": 211,
    "container": 132,
    "alert": 51,
    "d-flex": 64,
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
}


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def bootstrap_counts() -> Counter:
    """How often each watched class appears, across every template."""
    counts: Counter = Counter()
    for path in _templates():
        text = path.read_text(errors="replace")
        for name in BASELINE:
            # col- is a prefix (col-6, col-md-2); the rest are whole words.
            pattern = (
                r"\bcol-[\w-]+" if name == "col-" else r"(?<![\w-])%s(?![\w-])" % name
            )
            counts[name] += len(re.findall(pattern, text))
    return counts


def test_the_templates_are_found_at_all() -> None:
    """A path that stops matching would make every count zero and pass."""
    found = _templates()
    assert len(found) > 100, "only found %d templates under %s" % (
        len(found),
        TEMPLATES,
    )


def test_no_page_reaches_for_more_bootstrap() -> None:
    counts = bootstrap_counts()
    grown = [
        "%s: %d now, %d allowed -- use %s instead"
        % (name, counts[name], BASELINE[name], INSTEAD[name])
        for name in sorted(BASELINE)
        if counts[name] > BASELINE[name]
    ]
    assert not grown, (
        "Bootstrap is on the way out and these went up:\n  %s\n"
        "If the use is genuinely needed, raise the number here in the same "
        "commit and say why." % "\n  ".join(grown)
    )


def test_the_baseline_has_no_stale_numbers() -> None:
    """A number above what is really there stops holding anything down."""
    counts = bootstrap_counts()
    stale = [
        "%s: allowed %d, only %d left" % (name, BASELINE[name], counts[name])
        for name in sorted(BASELINE)
        if counts[name] < BASELINE[name]
    ]
    assert not stale, (
        "lower these to what the templates actually have, so the backlog "
        "cannot quietly grow back into the headroom:\n  %s" % "\n  ".join(stale)
    )

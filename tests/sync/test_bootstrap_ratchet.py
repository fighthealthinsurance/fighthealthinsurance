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
    "form-control": 2,
    "form-check-input": 5,
    "card": 106,
    "btn": 118,
    "row": 136,
    "col-": 208,
    "container": 113,
    "alert": 16,
    "d-flex": 58,
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


# What each template carries today, counted 2026-09-18. Lower as uses go.
PER_TEMPLATE = {
    "404.html": {"btn": 1, "container": 1},
    "about_ai.html": {"container": 1},
    "about_us.html": {"card": 3, "col-": 11, "container": 1, "row": 4},
    "appeal.html": {"btn": 7, "col-": 2, "container": 1, "d-flex": 2},
    "appeals.html": {"btn": 4, "col-": 1, "container": 2, "d-flex": 1},
    "as_seen_on_pbs.html": {"btn": 1, "container": 1},
    "base.html": {"col-": 2, "container": 2, "row": 1},
    "brb.html": {"container": 1},
    "categorize.html": {"btn": 2, "container": 1},
    "chat_consent.html": {
        "alert": 1,
        "btn": 1,
        "card": 1,
        "col-": 1,
        "container": 1,
        "row": 1,
    },
    "confirm_delete.html": {"btn": 1, "col-": 2, "d-flex": 2, "row": 1},
    "contact.html": {"container": 1},
    "delete_data_email_sent.html": {"alert": 1, "col-": 2, "d-flex": 2, "row": 1},
    "denial_language_library.html": {
        "alert": 1,
        "btn": 4,
        "card": 2,
        "col-": 6,
        "container": 5,
        "d-flex": 4,
        "row": 7,
    },
    "escalation_packet.html": {"btn": 3, "col-": 1, "container": 1, "d-flex": 1},
    "escalation_packet_review.html": {"btn": 2, "container": 1},
    "explain_denial.html": {
        "alert": 1,
        "btn": 1,
        "card": 7,
        "col-": 9,
        "container": 3,
        "d-flex": 1,
        "form-control": 2,
        "row": 5,
    },
    "faq.html": {"container": 1},
    "faq_post.html": {"container": 1},
    "fax_followup_thankyou.html": {"container": 1},
    "fax_thankyou.html": {"container": 1},
    "faxfollowup.html": {"btn": 1, "container": 1},
    "find_next_steps_loading.html": {"btn": 3, "container": 1},
    "followup.html": {"btn": 2, "container": 1},
    "followup_thankyou.html": {"btn": 1, "container": 1},
    "how_to_help.html": {"btn": 10, "col-": 2, "container": 1, "row": 1},
    "landing_base.html": {"btn": 5, "container": 4, "d-flex": 1, "row": 1},
    "media_references.html": {"btn": 1, "container": 1},
    "medicaid_eligibility.html": {
        "alert": 1,
        "btn": 12,
        "card": 21,
        "col-": 26,
        "container": 9,
        "d-flex": 5,
        "row": 15,
    },
    "mfa_auth_base.html": {"card": 1, "container": 1},
    "mhmda.html": {"container": 1},
    "microsite.html": {
        "btn": 7,
        "card": 4,
        "col-": 10,
        "container": 10,
        "d-flex": 3,
        "row": 11,
    },
    "microsite_directory.html": {
        "alert": 1,
        "btn": 2,
        "card": 1,
        "col-": 4,
        "container": 1,
        "row": 5,
    },
    "other_resources.html": {"alert": 1, "col-": 7, "container": 1, "row": 6},
    "outside_help.html": {"btn": 1},
    "partials/bingo_board.html": {"container": 1},
    "partials/featured_section.html": {"col-": 11, "container": 1, "row": 4},
    "partials/financial_assistance_section.html": {
        "card": 5,
        "col-": 1,
        "container": 1,
        "row": 1,
    },
    "partials/pharmacy_coupon_section.html": {
        "card": 1,
        "col-": 1,
        "container": 1,
        "row": 1,
    },
    "partials/site_banner.html": {"alert": 1, "container": 1},
    "partials/user_consent_form_fields.html": {"col-": 5, "row": 2},
    "patient_access.html": {
        "alert": 1,
        "btn": 4,
        "card": 12,
        "col-": 18,
        "container": 6,
        "d-flex": 2,
        "row": 10,
    },
    "plan_documents.html": {"col-": 1},
    "preparing_2026.html": {
        "alert": 1,
        "btn": 8,
        "card": 12,
        "col-": 19,
        "container": 10,
        "d-flex": 9,
        "form-check-input": 5,
        "row": 15,
    },
    "privacy_policy.html": {"container": 1},
    "proconnector.html": {"card": 4},
    "proconnector_quick_intro.html": {"card": 3},
    "professional.html": {"alert": 1, "container": 1},
    "professional_available.html": {"btn": 1, "container": 1},
    "professional_thankyou.html": {"container": 1},
    "remove_data.html": {"alert": 1, "btn": 1, "col-": 2, "d-flex": 3, "row": 1},
    "removed_data.html": {"col-": 2, "d-flex": 2, "row": 1},
    "scrub.html": {"btn": 3, "col-": 1, "container": 1},
    "server_side_ocr.html": {"btn": 1},
    "server_side_ocr_error.html": {"btn": 1},
    "share_denial.html": {"col-": 2, "d-flex": 2, "row": 1},
    "single_optional_question.html": {"alert": 1, "btn": 2, "container": 1},
    "state_help.html": {
        "btn": 9,
        "card": 5,
        "col-": 11,
        "container": 7,
        "d-flex": 2,
        "row": 10,
    },
    "state_help_index.html": {
        "btn": 7,
        "card": 5,
        "col-": 15,
        "container": 5,
        "d-flex": 3,
        "row": 9,
    },
    "stripe_finish_error.html": {"alert": 1, "col-": 2, "d-flex": 2, "row": 1},
    "thankyou.html": {"container": 1},
    "tos.html": {"container": 1},
    "turning_26.html": {
        "btn": 8,
        "card": 14,
        "col-": 20,
        "container": 8,
        "d-flex": 8,
        "row": 14,
    },
    "understand_policy.html": {
        "alert": 2,
        "btn": 1,
        "card": 5,
        "col-": 9,
        "container": 3,
        "d-flex": 1,
        "row": 6,
    },
    "unsubscribed.html": {"col-": 2, "d-flex": 2, "row": 1},
    "warnings.html": {"container": 1},
}


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


CLASS_ATTR = re.compile(r"""class\s*=\s*["']([^"']*)["']""")


COMMENTS = re.compile(
    r"<!--.*?-->|{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.S
)


def _live_markup(text: str) -> str:
    """The template with its commented-out markup removed.

    A commented-out button counted as a use, which is wrong in both
    directions: it inflated the baseline, it blocked tidying the comment
    away, and uncommenting it changed no count at all.
    """
    return COMMENTS.sub("", text)


def _classes_in(text: str):
    """Every class name the markup actually puts on an element.

    Reading class attributes rather than the file's raw text, because the
    raw text also matches a selector in a page's own stylesheet, a class
    named inside a comment, and a word in prose. Counting those made the
    numbers wrong in both directions: three of the form-control matches were
    CSS selectors, and a commented-out button counted as a live one.
    """
    for attr in CLASS_ATTR.findall(text):
        for name in attr.split():
            yield name


# Known limits, so nobody reads more into a green run than is there. A class
# assembled by template logic ({% if %} inside the attribute, or a variable
# holding the name) is not seen, and neither is one added by JavaScript. This
# counts what is written literally in the markup, which is where Bootstrap
# actually sits in this codebase.


def bootstrap_counts() -> "dict[str, Counter]":
    """How often each watched class is used, per template.

    Per template, not per site, so removing a use on one page cannot pay for
    adding one on another.
    """
    counts: dict = {}
    for path in _templates():
        key = str(path.relative_to(TEMPLATES))
        here: Counter = Counter()
        for name in _classes_in(_live_markup(path.read_text(errors="replace"))):
            for watched in BASELINE:
                if watched == "col-":
                    if name.startswith("col-"):
                        here["col-"] += 1
                elif name == watched:
                    here[watched] += 1
        if here:
            counts[key] = here
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


def test_no_page_reaches_for_more_bootstrap() -> None:
    counts = totals()
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


def test_no_single_page_reaches_for_more_bootstrap() -> None:
    """The site total can hold steady while one page gets worse.

    Removing a use from one template must not buy the right to add one to
    another, so each template is held to what it has today.
    """
    counts = bootstrap_counts()
    grown = [
        "%s: %s went from %d to %d"
        % (path, name, PER_TEMPLATE.get(path, {}).get(name, 0), found)
        for path, here in sorted(counts.items())
        for name, found in sorted(here.items())
        if found > PER_TEMPLATE.get(path, {}).get(name, 0)
    ]
    assert not grown, (
        "these templates reached for more Bootstrap:\n  %s\n"
        "Use the site's own classes, or raise the number in PER_TEMPLATE in "
        "the same commit and say why." % "\n  ".join(grown)
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


def test_the_per_template_baseline_has_no_stale_numbers() -> None:
    counts = bootstrap_counts()
    stale = [
        "%s: %s allowed %d, only %d left"
        % (path, name, allowed, counts.get(path, {}).get(name, 0))
        for path, here in sorted(PER_TEMPLATE.items())
        for name, allowed in sorted(here.items())
        if counts.get(path, {}).get(name, 0) < allowed
    ]
    assert (
        not stale
    ), "lower these to what the templates actually have:\n  %s" % "\n  ".join(stale)

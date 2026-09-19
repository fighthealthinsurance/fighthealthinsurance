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

# Counted on 2026-09-18, after the input box moved to the site's own class,
# and recounted on 2026-09-19 once classes behind a template condition became
# visible. That recount moved "alert" from 16 to 33, all seventeen of them in
# admin_status.html, where ".stat-card.alert" is that page's own class,
# defined in its own style block, that happens to share Bootstrap's name.
# Lower these as uses go. The only thing that raises one is a page arriving
# that was written before this existed, and then by exactly what that page
# brings, with the page named in PER_TEMPLATE below, so no page can grow
# under cover of a total. 2026-09-19: the two glossary pages, written before
# the ratchet landed.
BASELINE = {
    "form-control": 2,
    "form-check-input": 5,
    "card": 108,
    "btn": 126,
    "row": 148,
    "col-": 219,
    "container": 122,
    "alert": 33,
    "d-flex": 61,
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
    # Not Bootstrap's alert: this page defines .stat-card.alert itself.
    "admin_status.html": {"alert": 17},
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
    # Written before this ratchet existed, and counted here so they cannot
    # grow. Converting them to our own classes is its own change: the
    # col- and row uses are the Bootstrap grid, so it is a layout edit
    # that wants somebody looking at the rendered page.
    "glossary.html": {
        "btn": 4,
        "card": 1,
        "col-": 7,
        "container": 6,
        "d-flex": 1,
        "row": 7,
    },
    "glossary_index.html": {
        "btn": 4,
        "card": 1,
        "col-": 4,
        "container": 3,
        "d-flex": 2,
        "row": 5,
    },
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
ATTRIBUTE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)"""
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
    for tag in OPENING_TAG.findall(TEMPLATE_TAGS.sub(" ", text)):
        for attribute, value in ATTRIBUTE.findall(tag):
            if attribute.lower() != "class":
                continue
            for name in value.strip("\"'").split():
                yield name


# Known limits, so nobody reads more into a green run than is there. A class
# whose name arrives in a variable is not seen, and neither is one added by
# JavaScript. This counts what is written literally in the markup, inside a
# condition or not, which is where Bootstrap actually sits in this codebase.


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

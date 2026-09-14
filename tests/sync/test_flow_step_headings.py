"""Every page in the appeal flow names its step in a real heading.

Until now no page in the flow had an ``h1``. health_history.html,
plan_documents.html and the single_optional_question.html shell they extend
carried no heading of any level at all, so someone skimming with a screen
reader on a very long page had nothing to skim: the browser tab title was the
only structural signal that the step had changed.

The pages checked here are exactly the ones the change touches. scrub.html,
the upload step, is deliberately not in the list: its heading arrives with the
upload rebuild, not here.

Rendering the template is not enough on its own. A page in this flow is read
after its script has run, and appeal_fetcher.ts hides the whole loading block
on appeals.html once the drafts land, taking five headings out of the
accessibility tree with it. A template-only check passes on headings nobody can
reach, so the order is checked three times: once on the server render, once
with the blocks that script hides taken out, and once with the blocks that ship
display:none taken out as well, because #external-models-prompt is revealed only
when generation comes back short and is otherwise never read. The same goes for
the two conditional sections outside_help.html includes, which render only for a
denial that matches a medication or a financial-assistance programme.

A heading that is only styling does not count, which is why every assertion
below looks for a real ``h1`` element and reads its text.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.template import engines
from django.template.loader import render_to_string
from django.test import SimpleTestCase

_HEADING = re.compile(r"^h[1-6]$")
# The character itself stays out of the file: \u2014 is the em dash.
_EM_DASH = "\u2014"

_JS = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"

# `const name = document.getElementById("some-id")` and, separately, the
# `name.style.display = "none"` that takes it off the page. Matching the two
# and intersecting them is what keeps this test tracking the script instead of
# a hard-coded list of ids that can quietly go stale.
_BOUND_ELEMENT = re.compile(
    r"""const\s+(\w+)\s*=\s*document\.getElementById\(\s*["']([^"']+)["']"""
)
_DISPLAY_NONE = re.compile(r"""(\w+)\.style\.display\s*=\s*["']none["']""")

_SHELL_CONTEXT = {"form": [], "next": "/next-step/"}

# Stand-ins for the two objects that make outside_help.html render its
# conditional sections. The templates read plain attributes, so dicts are
# enough; only the shape matters here, not the values.
_PHARMACY_SUGGESTION = {
    "bridge_message": "While you appeal, these may lower what you pay today.",
    "pharmacy_options": [
        {
            "name": "Example discount card",
            "url": "https://example.invalid/card",
            "description": "Shows the cash price at pharmacies near you.",
        }
    ],
    "oop_max_warning": "Cash prices may not count toward your out-of-pocket maximum.",
}

_PROGRAM = {
    "name": "Example assistance programme",
    "url": "https://example.invalid/programme",
    "description": "Helps with the cost of the medication while you appeal.",
    "eligibility_note": "Income limits apply.",
    "phone": "555-0100",
}

_FINANCIAL_ASSISTANCE = {
    "diagnosis_specific": [_PROGRAM],
    "manufacturer": [_PROGRAM],
    "general": [_PROGRAM],
    "safety_net": [_PROGRAM],
    "state_medicaid_name": "Example State Medicaid",
    "state_medicaid_url": "https://example.invalid/medicaid",
    "state_medicaid_phone": "555-0101",
}

# template -> (the step name its h1 says, the context it needs to render)
FLOW_PAGES = {
    "health_history.html": (
        "Your health history",
        dict(_SHELL_CONTEXT, current_step=2),
    ),
    "plan_documents.html": (
        "Your plan documents",
        dict(_SHELL_CONTEXT, current_step=3),
    ),
    "entity_extract.html": (
        "Reading your letter",
        dict(_SHELL_CONTEXT, current_step=4, form_context={"denial_id": 1}),
    ),
    "categorize.html": (
        "Details",
        {"current_step": 5, "post_infered_form": "", "back_url": "/back/"},
    ),
    "find_next_steps_loading.html": (
        "Questions",
        {"current_step": 6},
    ),
    "outside_help.html": (
        "Questions",
        {
            "current_step": 6,
            "combined": None,
            "denial_form": "",
            "outside_help_details": [],
            "pharmacy_suggestion": None,
            "financial_assistance": None,
        },
    ),
    "appeals.html": (
        "Your appeal",
        {
            "current_step": 7,
            "user_email": "someone@example.com",
            "denial_id": 1,
            "semi_sekret": "sekret",
            "form_context": {"denial_id": 1},
        },
    ),
    "appeal.html": (
        "Send your appeal",
        {
            "current_step": 8,
            "user_email": "someone@example.com",
            "denial_id": 1,
            "appeal": "Dear insurer,",
            "fax_form": "",
        },
    ),
}


def _page(template):
    name, (_, context) = template, FLOW_PAGES[template]
    return BeautifulSoup(render_to_string(name, context), "html.parser")


def _headings(page):
    return [
        (int(tag.name[1]), tag.get_text(" ", strip=True))
        for tag in page.find_all(_HEADING)
    ]


def _ids_hidden_by(script, function):
    """The element ids ``function`` in ``script`` sets to display:none.

    Read out of the TypeScript rather than copied into this file, so that a
    block which starts or stops being hidden changes what the test checks.
    """
    source = (_JS / script).read_text()
    bound = dict(_BOUND_ELEMENT.findall(source))
    start = source.find(f"function {function}(")
    if start < 0:
        raise AssertionError(
            f"{script} no longer defines {function}(). This test reads that "
            "function to learn which blocks leave the page, so it has to move "
            "with it."
        )
    # Every top-level function in these bundles closes on a brace in column 0.
    body = source[start : source.find("\n}", start)]
    return {bound[name] for name in _DISPLAY_NONE.findall(body) if name in bound}


class FlowStepHeadingTest(SimpleTestCase):
    def test_every_flow_page_carries_exactly_one_h1(self):
        for template in FLOW_PAGES:
            with self.subTest(template=template):
                ones = [
                    tag.get_text(" ", strip=True)
                    for tag in _page(template).find_all("h1")
                ]
                self.assertEqual(
                    len(ones),
                    1,
                    f"{template} rendered {len(ones)} h1 elements, wanted exactly "
                    f"one: {ones}",
                )

    def test_the_h1_names_the_step_in_the_persons_words(self):
        for template, (step_name, _) in FLOW_PAGES.items():
            with self.subTest(template=template):
                heading = _page(template).find("h1")
                self.assertIsNotNone(heading, f"{template} has no h1 at all")
                self.assertEqual(heading.get_text(" ", strip=True), step_name)

    def assert_opens_at_h1_and_skips_nothing(self, label, levels):
        self.assertTrue(levels, f"{label} renders no headings")
        self.assertEqual(levels[0][0], 1, f"{label} opens with h{levels[0][0]}, not h1")
        for (previous, before), (level, text) in zip(levels, levels[1:]):
            self.assertLessEqual(
                level,
                previous + 1,
                f"{label}: h{level} {text!r} follows h{previous} {before!r}, "
                "skipping a level",
            )

    def test_no_flow_page_skips_a_heading_level(self):
        for template in FLOW_PAGES:
            with self.subTest(template=template):
                self.assert_opens_at_h1_and_skips_nothing(
                    template, _headings(_page(template))
                )

    def test_the_appeals_page_skips_nothing_once_its_script_hides_the_loading_block(
        self,
    ):
        # appeals.html is where people wait longest, and by the time the drafts
        # arrive appeal_fetcher.ts has hidden the entire loading block. The
        # headings inside it are then out of the accessibility tree, so the
        # order a screen reader actually walks is the one left behind here.
        gone = _ids_hidden_by("appeal_fetcher.ts", "hideLoading")
        page = _page("appeals.html")
        removed = []
        for element_id in sorted(gone):
            for tag in page.find_all(id=element_id):
                removed.extend(_headings(tag))
                tag.decompose()
        # Without this the test would pass for the wrong reason the day the
        # script stops hiding anything that carries a heading.
        self.assertTrue(
            removed,
            f"nothing carrying a heading was hidden (ids: {sorted(gone)}), so this "
            "test is no longer checking the state it was written for",
        )
        self.assert_opens_at_h1_and_skips_nothing(
            "appeals.html after hideLoading()", _headings(page)
        )

    def test_the_appeals_page_skips_nothing_for_a_reader_who_never_opens_the_prompt(
        self,
    ):
        # hideLoading() is not the only thing keeping a heading off the page.
        # #external-models-prompt ships with display:none in the markup and is
        # revealed only when generation comes back short, so for most readers
        # its h2 is never in the accessibility tree at all. Decomposing only the
        # blocks the script hides would check an order that has an h2 in it
        # nobody reaches, and would still pass on the day that h2 is the only
        # thing bridging the page's h1 to a lower heading below it.
        gone = _ids_hidden_by("appeal_fetcher.ts", "hideLoading")
        page = _page("appeals.html")
        for element_id in sorted(gone):
            for tag in page.find_all(id=element_id):
                tag.decompose()

        unread = [
            tag
            for tag in page.find_all(style=True)
            if "display:none" in tag["style"].replace(" ", "")
        ]
        # Named before anything is decomposed, because bs4 empties a tag's
        # attributes when it goes and the failure message needs them.
        hidden_names = [tag.get("id") or tag.name for tag in unread]
        removed = []
        for tag in unread:
            # An inline-hidden block can sit inside another one, and taking the
            # outer one out takes the inner with it.
            if tag.decomposed:
                continue
            removed.extend(_headings(tag))
            tag.decompose()
        # The guard has to be the headings that left, not the existence of a
        # hidden block. appeals.html also ships div#base-form with
        # display:none and that block carries no heading, so asserting on
        # `unread` stayed true with #external-models-prompt made visible, and
        # this test silently became a copy of the one above it. This is the
        # same shape the sibling test uses.
        self.assertTrue(
            removed,
            f"no inline-hidden block carries a heading any more (hidden: "
            f"{hidden_names}), so this test no longer checks a state different "
            "from the one above it",
        )

        self.assert_opens_at_h1_and_skips_nothing(
            "appeals.html as a reader meets it once the drafts land",
            _headings(page),
        )

    def test_outside_help_skips_nothing_when_the_assistance_sections_render(self):
        # Both sections are conditional on the denial matching a medication or
        # a programme, so the ordinary render says nothing about the page the
        # patients they exist for are reading.
        _, context = FLOW_PAGES["outside_help.html"]
        page = BeautifulSoup(
            render_to_string(
                "outside_help.html",
                dict(
                    context,
                    pharmacy_suggestion=_PHARMACY_SUGGESTION,
                    financial_assistance=_FINANCIAL_ASSISTANCE,
                ),
            ),
            "html.parser",
        )
        for section in ("pharmacy-coupons", "financial-assistance"):
            self.assertIsNotNone(
                page.find(id=section),
                f"{section} did not render, so this test checks nothing",
            )
        self.assert_opens_at_h1_and_skips_nothing(
            "outside_help.html with both assistance sections", _headings(page)
        )

    def test_the_share_panel_sits_at_the_level_the_page_asks_for(self):
        # Seven templates outside this flow include the same panel, so the
        # default has to stay exactly the h5 they were built around, and only
        # a page that asks gets something else.
        engine = engines["django"]
        unasked = engine.from_string(
            "{% include 'partials/share_buttons.html' %}"
        ).render({})
        self.assertIsNotNone(
            BeautifulSoup(unasked, "html.parser").find("h5", id="share-heading"),
            "a caller that says nothing no longer gets the h5 it had",
        )
        asked = engine.from_string(
            "{% include 'partials/share_buttons.html' with share_heading_level=2 %}"
        ).render({})
        self.assertIsNotNone(
            BeautifulSoup(asked, "html.parser").find("h2", id="share-heading"),
            "the panel ignored the level the page asked for",
        )

    def test_the_two_optional_steps_name_different_steps(self):
        # They share single_optional_question.html. A heading written into the
        # shell instead of into each child would give them the same one.
        history = _page("health_history.html").find("h1").get_text(" ", strip=True)
        documents = _page("plan_documents.html").find("h1").get_text(" ", strip=True)
        self.assertNotEqual(history, documents)

    def test_the_shared_shell_gives_each_child_somewhere_to_put_its_heading(self):
        engine = engines["django"]
        overridden = engine.from_string(
            "{% extends 'single_optional_question.html' %}"
            "{% block page_heading %}<h1>A step of its own</h1>{% endblock %}"
        ).render(dict(_SHELL_CONTEXT, current_step=2))
        self.assertIn("<h1>A step of its own</h1>", overridden)

        # The shell writes no heading of its own, so a child that names no step
        # is visibly missing one rather than inheriting a wrong one.
        bare = engine.from_string(
            "{% extends 'single_optional_question.html' %}"
        ).render(dict(_SHELL_CONTEXT, current_step=2))
        self.assertIsNone(BeautifulSoup(bare, "html.parser").find("h1"))

    def test_only_one_element_on_the_appeals_page_claims_the_skip_link_target(self):
        page = _page("appeals.html")
        skip_link = page.find("a", class_="skip-link")
        self.assertIsNotNone(skip_link, "the skip link is gone")
        target = skip_link["href"].lstrip("#")

        landing = page.find_all(id=target)
        self.assertEqual(
            len(landing),
            1,
            f"{len(landing)} elements carry id={target!r}; the skip link can only "
            "reach the first one",
        )
        self.assertEqual(landing[0].name, "main")

    def test_no_heading_in_the_flow_uses_an_em_dash(self):
        for template in FLOW_PAGES:
            with self.subTest(template=template):
                for level, text in _headings(_page(template)):
                    self.assertNotIn(_EM_DASH, text, f"{template}: h{level} {text!r}")

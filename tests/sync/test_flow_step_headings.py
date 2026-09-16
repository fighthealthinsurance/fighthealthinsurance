"""The appeal-flow pages in FLOW_PAGES name their step in a real heading.

FLOW_PAGES is the whole of what is claimed here. scrub.html, the upload step,
is out of it on purpose: its heading arrives with the upload rebuild.

These are structural checks on rendered markup, not checks of what the browser
does. Where one covers a state a script produces, it gets there by reading ids
out of the TypeScript and removing those nodes from the parsed page. Nothing
here runs JavaScript, so whether hideLoading() is reached at all, its guards
and its one-second timer are all outside what these can see. What they do
check is that the markup left in that state still opens at h1 and skips no
level.
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

# Pairing the binding with the assignment that hides it keeps this reading the
# script rather than a hard-coded list of ids that can go stale.
_BOUND_ELEMENT = re.compile(
    r"""const\s+(\w+)\s*=\s*document\.getElementById\(\s*["']([^"']+)["']"""
)
_DISPLAY_NONE = re.compile(r"""(\w+)\.style\.display\s*=\s*["']none["']""")

_SHELL_CONTEXT = {"form": [], "next": "/next-step/"}

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
    """The element ids ``function`` in ``script`` sets to display:none."""
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

    def test_the_appeals_page_skips_nothing_without_the_blocks_hideloading_hides(
        self,
    ):
        gone = _ids_hidden_by("appeal_fetcher.ts", "hideLoading")
        page = _page("appeals.html")
        removed = []
        for element_id in sorted(gone):
            for tag in page.find_all(id=element_id):
                removed.extend(_headings(tag))
                tag.decompose()
        self.assertTrue(
            removed,
            f"nothing carrying a heading was hidden (ids: {sorted(gone)}), so this "
            "test is no longer checking the state it was written for",
        )
        self.assert_opens_at_h1_and_skips_nothing(
            "appeals.html with the blocks hideLoading() hides removed", _headings(page)
        )

    def test_the_appeals_page_skips_nothing_without_the_blocks_that_ship_hidden(
        self,
    ):
        # #external-models-prompt ships display:none and is revealed only when
        # generation comes back short, so the order most people are served does
        # not have its h2 in it to bridge down to the headings below.
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
            # Taking out an outer hidden block takes its nested ones with it.
            if tag.decomposed:
                continue
            removed.extend(_headings(tag))
            tag.decompose()
        self.assertTrue(
            removed,
            f"no inline-hidden block carries a heading any more (hidden: "
            f"{hidden_names}), so this test no longer checks a state different "
            "from the one above it",
        )

        self.assert_opens_at_h1_and_skips_nothing(
            "appeals.html with the hidden blocks and the loading block removed",
            _headings(page),
        )

    def test_outside_help_skips_nothing_when_the_assistance_sections_render(self):
        # Both sections render only for a denial that matches a medication or
        # a programme, so the default context never puts them on the page.
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
        # Templates outside this flow include the panel without asking for a
        # level, so the default has to stay the h5 they were built around.
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
        # They share single_optional_question.html, so a heading written into
        # the shell rather than each child would give them the same one.
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

        # A child that names no step gets none, rather than a wrong one.
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

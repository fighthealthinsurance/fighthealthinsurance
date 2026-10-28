"""Every place the site takes a document is one kind of box, built once.

There are two shapes, each drawn by one template, and one script behind both:

- a letter's text box with its file button inside it
  (partials/letter_box_pick.html): the intake page and Explain Denial;
- a box for files alone (partials/file_drop_box.html, which a page
  includes and form_utils.FileDropInput and MultipleFileInput render for a
  form field): plan documents, the follow-up form, Understand Policy, the
  server-side reading page and Share your plan documents;
- static/js/file_drop.js, loaded on every page, which takes a file dropped
  on any box marked data-file-drop (the chat's message box too) and hands it
  to that box's own file input.

The script's behaviour runs in node over tests/js/file_drop_behaviour.cjs,
skipped, not silently passed, where node is not installed. The rest reads
the rendered pages and the sources.
"""

import json
import pathlib
import re
import subprocess

import pytest
from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.chat_forms import (
    SharePlanDocumentsForm,
    UnderstandPolicyForm,
)
from fighthealthinsurance.forms import FollowUpForm, PlanDocumentsForm
from tests.sync.test_escalation_packet_behaviour import NODE, needs_node

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
SCRIPT = APP / "static" / "js" / "file_drop.js"
DRIVER = REPO_ROOT / "tests" / "js" / "file_drop_behaviour.cjs"
TEMPLATES = APP / "templates"
# The two templates that draw a file input; everything else uses them.
SHARED = (
    TEMPLATES / "partials" / "letter_box_pick.html",
    TEMPLATES / "partials" / "file_drop_box.html",
)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@pytest.fixture(scope="module")
def dropped():
    result = subprocess.run(
        [NODE, str(DRIVER), str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_a_file_dropped_on_a_box_goes_to_its_input_as_if_chosen(dropped):
    """Every file, to the box's own input, and its change event fired once,
    bubbling, so the listener that reads a chosen file reads it."""
    drop = dropped["dropped_on_the_box"]
    assert drop["files"] == ["page-1.png", "page-2.png"], drop
    assert drop["changes"] == [{"bubbles": True}], drop


@needs_node
def test_a_box_lights_up_while_a_file_is_over_it(dropped):
    drop = dropped["dropped_on_the_box"]
    assert drop["over"] == {"prevented": True, "effect": "copy"}, drop
    assert drop["litWhileOver"] is True, drop
    assert drop["litAfter"] is False, drop
    leaving = dropped["leaving_the_box"]
    assert leaving["litMovingWithin"] is True, leaving
    assert leaving["litAfterLeaving"] is False, leaving


@needs_node
def test_the_browser_never_opens_a_file_dropped_on_a_box(dropped):
    """Cancelling the browser's own handling is what stops it opening the
    file in place of the page, as well as reading it."""
    assert dropped["dropped_on_the_box"]["drop"]["prevented"] is True


@needs_node
def test_a_file_dropped_beside_the_box_is_refused_not_opened(dropped):
    """On a page with a box, a file let go anywhere else would be opened by
    the browser in place of the page, taking everything typed so far."""
    beside = dropped["dropped_beside_the_box"]
    assert beside["over"] == {"prevented": True, "effect": "none"}, beside
    assert beside["drop"]["prevented"] is True, beside
    assert beside["files"] is None and beside["changes"] == [], beside


@needs_node
def test_a_page_with_no_box_is_left_to_the_browser(dropped):
    page = dropped["dropped_on_a_page_with_no_box"]
    assert page["over"]["prevented"] is False, page
    assert page["drop"]["prevented"] is False, page


@needs_node
def test_dragging_text_into_a_box_is_left_to_the_browser(dropped):
    """Only a drag that carries files is taken over. Dragging a selection of
    text into or within the letter box still drops the text."""
    text = dropped["text_dragged_into_the_box"]
    assert text["over"]["prevented"] is False, text
    assert text["drop"]["prevented"] is False, text
    assert text["files"] is None and text["changes"] == [], text


@needs_node
def test_a_box_whose_input_is_off_takes_nothing(dropped):
    """The chat turns its file input off while it reads a file; a drop then
    is refused like one beside the box, not read on top of the first."""
    off = dropped["dropped_while_the_input_is_off"]
    assert off["over"]["effect"] == "none", off
    assert off["drop"]["prevented"] is True, off
    assert off["files"] is None and off["changes"] == [], off


@needs_node
def test_a_box_for_one_file_takes_the_first_of_several(dropped):
    one = dropped["two_dropped_on_a_box_for_one"]
    assert one["files"] == ["page-1.png"], one
    assert one["changes"] == [{"bubbles": True}], one


@needs_node
def test_the_page_says_drops_are_taken_once_the_script_has_run(dropped):
    """custom.css shows "or drop the file here" only then: without the
    script a file let go on a box would be opened in place of the page."""
    assert dropped["page_marked"] is True


def test_the_script_only_hands_the_file_to_the_boxs_own_input():
    """A dropped letter must not open a second way out for the document. The
    intake page's input has no name, so a file there never leaves the
    device; nothing in the script may send a file, name the input, or build
    an input of its own to carry it."""
    src = SCRIPT.read_text()
    assert "box.input.files = files;" in src
    for way_out in (
        "fetch(",
        "XMLHttpRequest",
        "FormData",
        "sendBeacon",
        "WebSocket",
        ".submit(",
        ".name",
        "createElement",
        "setAttribute",
        "appendChild",
    ):
        assert way_out not in src, f"file_drop.js uses {way_out}"


class EveryPageLoadsTheScriptTest(TestCase):
    def test_base_loads_it(self):
        for url in (reverse("scan"), reverse("explain_denial"), reverse("about")):
            with self.subTest(url=url):
                soup = BeautifulSoup(self.client.get(url).content, "html.parser")
                sources = [tag.get("src", "") for tag in soup.find_all("script")]
                self.assertTrue(
                    any("js/file_drop.js" in src for src in sources), sources
                )


def _inputs_outside_a_box(html: str) -> "list[str]":
    soup = BeautifulSoup(html, "html.parser")
    inputs = soup.find_all("input", attrs={"type": "file"})
    assert inputs, "no file input rendered"
    return [
        str(tag)
        for tag in inputs
        if tag.find_parent(attrs={"data-file-drop": True}) is None
    ]


class EveryUploadIsABoxTest(TestCase):
    """Each place a person gives us a document, as rendered."""

    def test_the_pages(self):
        for name in (
            "scan",
            "explain_denial",
            "understand_policy",
            "server_side_ocr",
            "share_plan_documents",
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(_inputs_outside_a_box(response.content.decode()), [])

    def test_the_form_fields(self):
        for form, field in (
            (PlanDocumentsForm(), "plan_documents"),
            (FollowUpForm(), "followup_documents"),
            (UnderstandPolicyForm(), "policy_document"),
            (SharePlanDocumentsForm(), "plan_documents"),
        ):
            with self.subTest(field=field):
                html = str(form[field])
                self.assertEqual(_inputs_outside_a_box(html), [])
                box = BeautifulSoup(html, "html.parser").find(class_="fhi-drop-box")
                self.assertIsNotNone(box, html)

    def test_a_page_with_no_form_field_includes_the_same_box(self):
        """Plan documents and the server-side reading page include the box
        with a name; the input takes it as its id too."""
        html = render_to_string(
            "partials/file_drop_box.html", {"name": "plan_documents", "multiple": True}
        )
        tag = BeautifulSoup(html, "html.parser").find("input")
        self.assertEqual(tag["id"], "plan_documents")
        self.assertEqual(tag["name"], "plan_documents")
        self.assertIn("multiple", tag.attrs)
        self.assertEqual(_inputs_outside_a_box(html), [])
        for page in ("plan_documents.html", "partials/server_ocr_form.html"):
            with self.subTest(page=page):
                self.assertIn(
                    '{% include "partials/file_drop_box.html"',
                    (TEMPLATES / page).read_text(),
                )

    def test_a_form_field_keeps_its_own_attributes(self):
        """What a form gives its field (an id, the file types, required) is
        on the input the box draws."""
        tag = BeautifulSoup(
            str(UnderstandPolicyForm()["policy_document"]), "html.parser"
        ).find("input")
        self.assertEqual(tag["id"], "policy_document")
        self.assertEqual(tag["name"], "policy_document")
        self.assertEqual(tag["accept"], ".pdf,.docx,.txt")
        self.assertIn("required", tag.attrs)
        self.assertNotIn("multiple", tag.attrs)

    def test_a_box_says_it_takes_a_drop_in_its_own_number(self):
        many = str(PlanDocumentsForm()["plan_documents"])
        one = str(UnderstandPolicyForm()["policy_document"])
        self.assertIn("or drop the files here", many)
        self.assertIn("or drop the file here", one)


def test_no_template_draws_a_file_input_of_its_own():
    """A new upload uses a shared box rather than an input of its own: the
    box for files (included, or a form field), or the letter box partial for
    a letter's text. Markup inside an HTML comment does not count."""
    raw = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        if template in SHARED:
            continue
        source = HTML_COMMENT.sub("", template.read_text())
        if re.search(r"""type=["']?file""", source):
            raw.append(str(template.relative_to(TEMPLATES)))
    assert raw == [], raw


def test_the_chat_box_takes_a_drop_on_its_paperclip_input():
    """The chat's paperclip already sits in its message box. The box is a
    drop target, and the paperclip's input is inside it."""
    src = (APP / "static" / "js" / "chat_interface.tsx").read_text()
    box = src.index('data-file-drop=""')
    paperclip = src.index("<FileButton", box)
    assert src.index("<Textarea", box) < paperclip, "the box is not the message box"
    assert "inputProps={{ disabled: state.isProcessingFile }}" in src[paperclip:]

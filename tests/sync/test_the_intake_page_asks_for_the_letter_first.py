"""The intake page asks for the denial letter first.

It used to open with five boxes about the person (first and last name,
email, street and ZIP) before the one thing they came with, and the file
picker and the letter box sat below all of them. Most sites that take a
document ask for it first, so the letter's step leads, its file button and
its box together, and the details about the person follow.

These read the rendered page in document order, the order a screen reader
and a phone both take it in.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.urls import reverse

JS = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"
# The scripts the page's bundle runs, and what each reads off the page.
SCRIPTS = ("scrub.ts", "scrub_client_side_form.ts", "scrub_scrub.ts", "scrub_ocr.ts")
# Ids those scripts look up that belong to other pages or to markup this page
# dropped long ago (validateAndStore's form and its email_address field, an
# old second scrub button). Every lookup of them is guarded and does nothing
# here.
NOT_ON_THIS_PAGE = {"email_address", "scrub", "scrubform", "storeButton"}

IDENTITY_FIELDS = ("store_fname", "store_lname", "email", "store_street", "store_zip")
AGREEMENTS = ("personalonly", "privacy", "pii", "tos")
OPTIONAL = (
    "store_raw_email",
    "use_external_models",
    "subscribe",
    "referral_source",
    "referral_source_details",
    "persistence_enabled",
)
STEPS = ["Your denial letter", "About you", "Policies", "Optional choices"]
HEADING = re.compile(r"^h[1-6]$")


class TheIntakePageAsksForTheLetterFirstTest(TestCase):
    def setUp(self):
        html = self.client.get(reverse("scan")).content.decode()
        self.main = BeautifulSoup(html, "html.parser").find("main")
        self.assertIsNotNone(self.main, "the intake page renders no <main>")
        self.elements = self.main.find_all(True)

    def _at(self, element) -> int:
        for index, candidate in enumerate(self.elements):
            if candidate is element:
                return index
        raise AssertionError("%r is not on the page" % element)

    def _id(self, ident: str) -> int:
        found = self.main.find_all(id=ident)
        self.assertEqual(
            len(found), 1, "#%s is on the page %d times" % (ident, len(found))
        )
        return self._at(found[0])

    def _heading(self, text: str) -> int:
        for tag in self.main.find_all(HEADING):
            if tag.get_text(" ", strip=True) == text:
                return self._at(tag)
        raise AssertionError("no heading reads %r" % text)

    def test_the_letter_box_and_the_file_button_come_before_the_first_identity_field(
        self,
    ):
        first_identity = min(self._id(field) for field in IDENTITY_FIELDS)
        button = self.main.find("label", attrs={"for": "uploader"})
        self.assertIsNotNone(button, "the file input has no visible button")
        for name, at in (
            ("the file input", self._id("uploader")),
            ("the file button", self._at(button)),
            ("the letter box", self._id("denial_text")),
        ):
            with self.subTest(control=name):
                self.assertLess(
                    at, first_identity, "%s comes after the identity fields" % name
                )

    @override_settings(ADVANCED_OCR_OFFERED=True)
    def test_the_file_button_leads_the_step_when_better_reading_is_offered(self):
        """The better text recognition option is a tick box with a paragraph
        of fine print. Above the button it pushed the letter's first control
        most of a phone screen down, so it follows the button. It is read
        when the file is, so it says to tick it first."""
        self.setUp()
        option = self._id("advanced_ocr_enabled")
        button = self._at(self.main.find("label", attrs={"for": "uploader"}))
        self.assertLess(self._heading("Your denial letter"), button)
        self.assertLess(button, option, "the option sits above the file button")
        self.assertLess(option, self._id("denial_text"))
        section = self.main.find(id="advanced_ocr_section").get_text(" ", strip=True)
        self.assertIn("Tick this before you choose the file.", section)

    def test_the_steps_are_headed_in_outline_order(self):
        """One h1, the page's own, then the steps as h2s in the order they are
        asked, and never a step down of more than one level."""
        headings = [
            (int(tag.name[1]), tag.get_text(" ", strip=True))
            for tag in self.main.find_all(HEADING)
        ]
        self.assertEqual([level for level, _ in headings].count(1), 1, headings)
        self.assertEqual(
            headings[0][0], 1, "the page does not open on its h1: %s" % headings
        )
        self.assertEqual(
            [text for level, text in headings if level == 2][: len(STEPS)],
            STEPS,
            headings,
        )
        for (above, above_text), (level, text) in zip(headings, headings[1:]):
            self.assertLessEqual(
                level,
                above + 1,
                "h%d %r follows h%d %r" % (level, text, above, above_text),
            )

    def test_each_step_holds_its_own_fields(self):
        """Each heading sits above what it names and below the step before,
        so a heading never labels the wrong group of boxes."""
        letter, about, policies, optional = (self._heading(step) for step in STEPS)
        groups = [
            (letter, ("uploader", "ocr_in_progress", "denial_text")),
            (about, IDENTITY_FIELDS + ("scrub-2",)),
            (policies, AGREEMENTS),
            (optional, OPTIONAL),
        ]
        ends = [about, policies, optional, self._id("submit")]
        for (start, fields), end in zip(groups, ends):
            for field in fields:
                with self.subTest(field=field):
                    at = self._id(field)
                    self.assertLess(
                        start, at, "#%s sits above its step's heading" % field
                    )
                    self.assertLess(at, end, "#%s sits in the next step" % field)

    def test_the_hidden_file_input_is_drawn_as_a_button_that_says_what_it_does(self):
        """The input itself is hidden from sight, so its label is the only
        part of it anyone sees; without the label the control is gone."""
        button = self.main.find("label", attrs={"for": "uploader"})
        self.assertIsNotNone(button)
        self.assertIn("fhi-button", button.get("class", []))
        self.assertEqual(
            button.get_text(" ", strip=True), "Choose a file or take a photo"
        )


def _script_hooks() -> "tuple[set[str], set[str]]":
    """Every id the page's scripts look up by name, and every control they
    read as a property of the form (form.pii, form.denial_text)."""
    ids: set[str] = set()
    names: set[str] = set()
    for script in SCRIPTS:
        src = (JS / script).read_text()
        ids |= set(re.findall(r'getElementById\(\s*"([^"]+)"', src))
        ids |= set(re.findall(r'(?:show|rehide)HiddenMessage\("([^"]+)"\)', src))
        names |= set(re.findall(r"\bform\.([a-z_]+)\.", src))
    return ids - NOT_ON_THIS_PAGE, names


class EveryHookTheScriptsReadIsOnThePageOnceTest(TestCase):
    """Moving the page's parts around must not lose one a script finds by id,
    or leave two: getElementById returns the first, so a second copy is a
    control that silently stops working."""

    @override_settings(ADVANCED_OCR_OFFERED=True)
    def test_each_id_the_scripts_read_is_on_the_page_once(self):
        ids, _ = _script_hooks()
        self.assertIn("uploader", ids, "the hook list no longer reads the scripts")
        soup = BeautifulSoup(
            self.client.get(reverse("scan")).content.decode(), "html.parser"
        )
        for ident in sorted(ids):
            with self.subTest(id=ident):
                self.assertEqual(len(soup.find_all(id=ident)), 1, "#%s" % ident)

    def test_each_control_the_scripts_read_off_the_form_is_in_it_once(self):
        _, names = _script_hooks()
        self.assertIn("denial_text", names, "the hook list no longer reads the scripts")
        soup = BeautifulSoup(
            self.client.get(reverse("scan")).content.decode(), "html.parser"
        )
        form = soup.find(id="fuck_health_insurance_form")
        self.assertIsNotNone(form)
        for name in sorted(names):
            with self.subTest(control=name):
                found = [
                    tag
                    for tag in form.find_all(True)
                    if tag.get("id") == name or tag.get("name") == name
                ]
                self.assertEqual(len(found), 1, "form.%s" % name)

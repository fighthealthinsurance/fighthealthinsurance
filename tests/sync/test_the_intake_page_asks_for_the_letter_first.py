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
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import views
from fighthealthinsurance.forms import DenialForm
from fighthealthinsurance.models import Denial

JS = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"
# The scripts the page's bundle runs, and what each reads off the page.
SCRIPTS = ("scrub.ts", "scrub_client_side_form.ts", "scrub_scrub.ts", "scrub_ocr.ts")
# Ids those scripts look up that belong to other pages or to markup this page
# dropped long ago (validateAndStore's form and its email_address field, an
# old second scrub button). Every lookup of them is guarded and does nothing
# here.
NOT_ON_THIS_PAGE = {"email_address", "scrub", "scrubform", "storeButton"}

IDENTITY_FIELDS = ("store_fname", "store_lname", "email", "store_street", "store_zip")
# In page order.
AGREEMENTS = ("pii", "privacy", "tos", "personalonly")
# Each box's label, word for word.
AGREEMENT_LABELS = {
    "pii": "I've taken my personal details out of the letter above.",
    "privacy": "I have read and understand the privacy policy.",
    "tos": "I agree to the terms of service. I'll use this site only for my own "
    "insurance appeals, not to diagnose or treat any condition.",
    "personalonly": "This is for my own appeal. (Doctors, therapists and offices: "
    "see our professional version.)",
}
# Each message, the field it sits under, and the fields it describes when it
# shows. The agreements' message sits at the foot of their group, under the
# last box.
MESSAGES = {
    "need_denial": ("denial_text", ("denial_text",)),
    "email_error": ("email", ("email",)),
    "pii_error": ("pii", ("pii",)),
    "agree_chk_error": ("personalonly", ("privacy", "tos", "personalonly")),
}
CONTROLS = ("input", "textarea", "select", "button")
EVERYTHING_BUT_THE_PERSONAL_USE_BOX = {
    "denial_text": "My MRI was denied as not medically necessary.",
    "email": "someone@example.com",
    "pii": "on",
    "privacy": "on",
    "tos": "on",
}
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
        ids |= set(re.findall(r'\bcheck\(\s*"([^"]+)"', src))
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


def _scan_page(client, data=None) -> BeautifulSoup:
    """The intake page as served: on a GET, or sent back after a POST."""
    response = (
        client.post(reverse("process"), data)
        if data is not None
        else client.get(reverse("scan"))
    )
    assert response.status_code == 200, response.status_code
    return BeautifulSoup(response.content.decode(), "html.parser")


def _follows(soup: BeautifulSoup, earlier, later) -> bool:
    elements = soup.find_all(True)
    order = {id(element): at for at, element in enumerate(elements)}
    return order[id(earlier)] < order[id(later)]


class TheAgreementsAreOneGroupTest(TestCase):
    def setUp(self):
        self.soup = _scan_page(self.client)

    def test_the_four_boxes_are_one_fieldset_named_by_the_steps_heading(self):
        boxes = [self.soup.find(id=box) for box in AGREEMENTS]
        groups = {id(box.find_parent("fieldset")) for box in boxes}
        self.assertEqual(len(groups), 1, "the boxes are not in one fieldset")
        fieldset = boxes[0].find_parent("fieldset")
        self.assertIsNotNone(fieldset)
        legend = fieldset.find("legend", recursive=False)
        self.assertIsNotNone(legend, "the group has no legend")
        self.assertEqual(legend.get_text(" ", strip=True), "Policies")
        # The legend holds the step's heading, so the outline keeps it.
        self.assertIsNotNone(legend.find("h2", string="Policies"))
        self.assertEqual(
            [box["id"] for box in fieldset.find_all("input", type="checkbox")],
            list(AGREEMENTS),
        )

    def test_the_boxes_keep_their_names_and_words_in_the_new_order(self):
        boxes = self.soup.find_all("input", type="checkbox")
        ids = [box["id"] for box in boxes]
        self.assertEqual(
            [ident for ident in ids if ident in AGREEMENTS],
            list(AGREEMENTS),
            "the agreements are not in the order pii, privacy, tos, personal use",
        )
        for box in AGREEMENTS:
            with self.subTest(box=box):
                tag = self.soup.find(id=box)
                self.assertEqual(tag.get("name"), box)
                label = self.soup.find("label", attrs={"for": box})
                self.assertEqual(
                    re.sub(r"\s+", " ", label.get_text()).strip(),
                    AGREEMENT_LABELS[box],
                )


class EachMessageSitsUnderItsFieldTest(TestCase):
    def setUp(self):
        self.soup = _scan_page(self.client)

    def test_each_message_is_an_empty_live_region_next_to_its_field(self):
        controls = self.soup.find_all(CONTROLS)
        for message_id, (field_id, _) in MESSAGES.items():
            with self.subTest(message=message_id):
                message = self.soup.find(id=message_id)
                field = self.soup.find(id=field_id)
                self.assertEqual(message.get_text(), "", "it is not empty")
                self.assertEqual(message.get("aria-live"), "polite")
                self.assertTrue(message.get("data-message"), "it has no words")
                self.assertTrue(_follows(self.soup, field, message))
                # No other control between the field and its message.
                after = [c for c in controls if _follows(self.soup, field, c)]
                if after:
                    self.assertTrue(
                        _follows(self.soup, message, after[0]),
                        "#%s comes after #%s" % (message_id, after[0].get("id")),
                    )

    def test_the_agreements_message_is_at_the_foot_of_their_group(self):
        message = self.soup.find(id="agree_chk_error")
        fieldset = self.soup.find(id="personalonly").find_parent("fieldset")
        self.assertIsNotNone(fieldset, "the agreements are not in a fieldset")
        self.assertIs(fieldset.find_all(True, recursive=False)[-1], message)

    def test_the_page_has_no_raw_error_dump(self):
        page = _scan_page(self.client, {})
        self.assertIsNone(page.find(class_="errorlist"), "form.errors is dumped")

    def test_a_fresh_page_marks_no_field_invalid(self):
        for _, fields in MESSAGES.values():
            for field_id in fields:
                with self.subTest(field=field_id):
                    field = self.soup.find(id=field_id)
                    self.assertIsNone(field.get("aria-invalid"))
                    self.assertIsNone(field.get("aria-describedby"))


class TheFormHoldsEveryControlTest(TestCase):
    """The rendered page has the messages, Submit and every control inside
    the <form> element itself, read the way an HTML parser builds the page,
    so a close tag out of place in the template shows up here."""

    def _assert_inside(self, soup):
        form = soup.find("form", id="fuck_health_insurance_form")
        self.assertIsNotNone(form)
        main = soup.find("main")
        held = list(main.find_all(CONTROLS)) + [
            soup.find(id=message) for message in MESSAGES
        ]
        held += main.find_all(class_="fhi-form-errors")
        self.assertIn(soup.find(id="submit"), held)
        for tag in held:
            with self.subTest(tag=tag.get("id") or tag.name):
                self.assertIn(form, tag.parents, "it is outside the form")

    @override_settings(ADVANCED_OCR_OFFERED=True)
    def test_on_the_page_as_first_served(self):
        self._assert_inside(_scan_page(self.client))

    def test_on_the_page_sent_back_with_errors(self):
        with patch.object(
            DenialForm, "clean", side_effect=ValidationError("Something else.")
        ):
            soup = _scan_page(self.client, {})
        self.assertIsNotNone(soup.find(class_="fhi-form-errors"))
        self._assert_inside(soup)


class TheServerSendsEachErrorBackByItsFieldTest(TestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def assert_shows(self, soup, message_id, words=None):
        message = soup.find(id=message_id)
        expected = words or message["data-message"]
        self.assertEqual(message.get_text(), expected)
        _, fields = MESSAGES[message_id]
        return message, fields

    def test_a_post_without_the_personal_use_box_is_refused_by_the_box(self):
        soup = _scan_page(self.client, EVERYTHING_BUT_THE_PERSONAL_USE_BOX)
        self.assertFalse(
            Denial.objects.filter(
                hashed_email=Denial.get_hashed_email("someone@example.com")
            ).exists()
        )
        self.assert_shows(soup, "agree_chk_error")
        box = soup.find(id="personalonly")
        self.assertEqual(box.get("aria-invalid"), "true")
        self.assertEqual(box.get("aria-describedby"), "agree_chk_error")
        for other in ("pii", "privacy", "tos"):
            with self.subTest(box=other):
                tag = soup.find(id=other)
                self.assertIsNone(tag.get("aria-invalid"))
                # What the person ticked is still ticked.
                self.assertIn("checked", tag.attrs)
        self.assertNotIn("checked", box.attrs)
        for quiet in ("need_denial", "email_error", "pii_error"):
            with self.subTest(message=quiet):
                self.assertEqual(soup.find(id=quiet).get_text(), "")
        self.assertEqual(
            soup.find(id="denial_text").get_text(),
            EVERYTHING_BUT_THE_PERSONAL_USE_BOX["denial_text"],
        )

    def test_an_empty_post_marks_each_field_with_its_own_message(self):
        soup = _scan_page(self.client, {})
        for message_id in MESSAGES:
            with self.subTest(message=message_id):
                _, fields = self.assert_shows(soup, message_id)
                for field_id in fields:
                    field = soup.find(id=field_id)
                    self.assertEqual(field.get("aria-invalid"), "true")
                    self.assertEqual(field.get("aria-describedby"), message_id)

    def test_an_email_the_server_cannot_use_is_explained_in_its_own_words(self):
        data = dict(EVERYTHING_BUT_THE_PERSONAL_USE_BOX, personalonly="on")
        data["email"] = "someone@example"
        soup = _scan_page(self.client, data)
        self.assert_shows(soup, "email_error", "Enter a valid email address.")
        self.assertEqual(soup.find(id="email").get("aria-invalid"), "true")

    def test_an_error_with_no_field_message_is_listed_at_the_top_of_the_form(self):
        with patch.object(
            DenialForm, "clean", side_effect=ValidationError("Something else.")
        ):
            soup = _scan_page(self.client, {})
        summary = soup.find(class_="fhi-form-errors")
        self.assertIsNotNone(summary)
        self.assertEqual(
            [item.get_text() for item in summary.find_all("li")], ["Something else."]
        )
        form = soup.find("form", id="fuck_health_insurance_form")
        first_step = form.find("section")
        self.assertTrue(_follows(soup, summary, first_step))

    def test_a_complete_post_goes_on_to_the_next_step(self):
        data = dict(EVERYTHING_BUT_THE_PERSONAL_USE_BOX, personalonly="on")
        response = self.client.post(reverse("process"), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Denial.objects.filter(
                hashed_email=Denial.get_hashed_email("someone@example.com")
            ).exists()
        )


class TheLetterTheServerReadIsInTheBoxTest(TestCase):
    """/server_side_ocr sends the same page back with the letter it read in
    the box. That render has no form in its context, so the page's messages
    and boxes render from nothing at all there."""

    def test_the_page_after_reading_an_upload_holds_the_letter(self):
        upload = SimpleUploadedFile(
            "letter.png", b"not read: _ocr is patched", content_type="image/png"
        )
        with patch.object(views.OCRView, "_ocr", return_value="Your MRI was denied."):
            response = self.client.post(
                reverse("server_side_ocr"), {"uploader": upload}
            )
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content.decode(), "html.parser")
        self.assertEqual(soup.find(id="denial_text").get_text(), "Your MRI was denied.")
        for message in MESSAGES:
            with self.subTest(message=message):
                self.assertEqual(soup.find(id=message).get_text(), "")

"""About you, filled from the letter in the box, run rather than read.

When a denial letter arrives in the intake page's box (pasted, read from a
file on this device, or put there by the server), letter_details.ts fills the
About you fields the person has left empty: first and last name, street and
ZIP code. It reads them from the addressee block and nowhere else, so the
insurer's return address and the provider's are never taken, and it fills
nothing rather than guess. Each field it fills says "From your letter. Please
check." until the person edits it.

These compile the real TypeScript with the repo's own tsc (the same flags as
test_entity_fetcher_behaviour.py), load it in node over tests/js/fake_page.cjs
through tests/js/letter_details_behaviour.cjs, and assert on the fields, the
hints and the note. The fake page records every console call and every
network call, so "the module logs nothing and sends nothing" is asserted on
what ran. (The ZIP field is part of the form, so a filled ZIP is sent with
it like a typed one: see TheFakePageIsTheIntakePageTest.)

Skipped, not silently passed, where node or the front-end toolchain is not
installed. The checks against the rendered page and scrub.ts need neither.
"""

import json
import os
import pathlib
import re
import subprocess

import pytest
from bs4 import BeautifulSoup
from django.test import TestCase
from django.urls import reverse

from tests.sync.test_entity_fetcher_behaviour import (
    NODE,
    SHIP_LIB,
    SHIP_TARGET,
    TSC,
    needs_node,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = REPO_ROOT / "fighthealthinsurance" / "static" / "js"
MODULE = JS / "letter_details.ts"
DRIVER = REPO_ROOT / "tests" / "js" / "letter_details_behaviour.cjs"

HINT = "From your letter. Please check."
NOTE = "We filled in what we found in your letter."
FILLABLE = ("store_fname", "store_lname", "store_street", "store_zip")
EVERY_FIELD = ("store_fname", "store_lname", "email", "store_street", "store_zip")
EMPTY = {field: "" for field in EVERY_FIELD}

# Every person, address and ZIP here is made up.
INSURER = "Acme Example Health Plan\nPO Box 0000\nAnytown, NY 00000\n"
MEMBER = "Jordan Example\n123 Sample Street\nSpringfield, IL 62701\n"
PROVIDER = "Pat Provider MD\n456 Example Avenue\nAnytown, IL 00001\n"
JORDAN = {
    "store_fname": "Jordan",
    "store_lname": "Example",
    "email": "",
    "store_street": "123 Sample Street",
    "store_zip": "62701",
}
JORDAN_NAME_ONLY = dict(EMPTY, store_fname="Jordan", store_lname="Example")

# The insurer's address on top, then the member's block, the salutation,
# and the provider's address at the foot.
TYPICAL_LETTER = (
    INSURER
    + "\nOctober 1, 2026\n\n"
    + MEMBER
    + "\nMember ID: XYZ000000\nEmail on file: jordan@example.com\n\n"
    + "Dear Jordan Example,\n\n"
    + "We reviewed your request for an MRI and found it not medically necessary.\n\n"
    + "cc:\n"
    + PROVIDER
)
DEAR_MEMBER_LETTER = (
    INSURER
    + "\nDear Member,\n\nMember name: Jordan Example\nMember ID: XYZ000000\n\n"
    + "We reviewed your request.\n"
)
SHOUTED_LETTER = (
    INSURER.upper()
    + "\nJORDAN A EXAMPLE\n123 SAMPLE STREET\nSPRINGFIELD IL 62701-0000\n\n"
    + "Dear Member:\nMember name: EXAMPLE, JORDAN A     Member ID: XYZ000000\n"
)
NO_MEMBER_LETTER = INSURER + "\nDear Sir or Madam,\n\nWe reviewed the request.\n"
# A prior authorization denial to the doctor, with the patient labelled.
TO_THE_DOCTOR_LETTER = (
    INSURER
    + "\nPatient: Jordan Example\nMember ID: XYZ000000\n\n"
    + "Dear Casey Doctorson, MD:\n\n"
    + "Your request for prior authorization for your patient is denied.\n"
)
# A greeting the rule has no word for, which reads like a name.
POLICY_HOLDER_LETTER = (
    "Dear Policy Holder,\nMember name: Jordan Example\n"
    + "Your policy does not cover this service. As a policyholder you may appeal.\n"
)
APARTMENT_LETTER = (
    "Dear Jordan Example,\n\nJordan Example\n123 Sample Street\nApt 4B\n"
    + "Springfield, IL 62701\n"
)


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """The real letter_details.ts, compiled the way the bundle is built."""
    out = tmp_path_factory.mktemp("letter-details")
    result = subprocess.run(
        [
            NODE,
            str(TSC),
            "--target",
            SHIP_TARGET,
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--lib",
            SHIP_LIB,
            "--strict",
            "--esModuleInterop",
            "--allowSyntheticDefaultImports",
            "--forceConsistentCasingInFileNames",
            "--skipLibCheck",
            "--outDir",
            str(out),
            str(MODULE),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    built = out / "letter_details.js"
    if not built.exists():
        pytest.fail(
            f"tsc did not emit letter_details.js\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return built


def run(compiled: pathlib.Path, **spec) -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), str(compiled), json.dumps(spec)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ, NODE_ENV="test"),
    )
    if result.returncode != 0:
        pytest.fail(
            f"scenario {spec} crashed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def hinted(result: dict) -> "set[str]":
    return {field for field, hint in result["hints"].items() if hint is not None}


@needs_node
def test_a_typical_letter_fills_the_members_block_not_the_insurers(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste")
    assert result["fields"] == JORDAN


@needs_node
def test_each_filled_field_says_where_it_came_from(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste")
    assert hinted(result) == set(FILLABLE)
    for field in FILLABLE:
        hint = result["hints"][field]
        assert hint == {
            "text": HINT,
            "tag": "small",
            "className": "fhi-hint",
            "rightAfterTheField": True,
        }, field
        assert result["describedBy"][field] == field + "_from_letter"
    assert result["note"] == {"text": NOTE, "hidden": False}


@needs_node
def test_a_paste_fills_once_the_text_is_in_the_box(compiled):
    """The paste event comes before the browser puts the text in, so the
    fill waits for it rather than reading the empty box."""
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste")
    assert result["beforeTheTextLanded"] == EMPTY
    assert result["fields"] == JORDAN


@needs_node
def test_typing_the_letter_in_fills_nothing(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="type")
    assert result["fields"] == EMPTY
    assert result["note"] == {"text": "", "hidden": True}


@needs_node
def test_a_letter_the_server_put_in_the_box_fills_on_load(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="load")
    assert result["fields"] == JORDAN


@needs_node
def test_a_file_read_on_this_device_fills_the_same_way(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="read")
    assert result["fields"] == JORDAN
    assert hinted(result) == set(FILLABLE)


@needs_node
def test_a_dear_member_letter_fills_the_labelled_name_and_no_address(compiled):
    """The only address in it is the insurer's, which is not anchored to
    the name."""
    result = run(compiled, letter=DEAR_MEMBER_LETTER, arrive="paste")
    assert result["fields"] == JORDAN_NAME_ONLY
    assert hinted(result) == {"store_fname", "store_lname"}


@needs_node
def test_a_shouted_last_first_label_reads_its_block(compiled):
    """A label of "EXAMPLE, JORDAN A" is Jordan Example, and the block under
    "JORDAN A EXAMPLE" is theirs. The street goes in as the letter has it."""
    result = run(compiled, letter=SHOUTED_LETTER, arrive="paste")
    assert result["fields"] == dict(JORDAN, store_street="123 SAMPLE STREET")


@needs_node
def test_a_letter_with_no_member_block_fills_nothing(compiled):
    result = run(compiled, letter=NO_MEMBER_LETTER, arrive="paste")
    assert result["fields"] == EMPTY
    assert hinted(result) == set()
    assert result["note"] == {"text": "", "hidden": True}
    assert result["remembered"] == []


@needs_node
def test_what_is_already_in_a_field_is_never_written_over(compiled):
    """Typed, or restored from this browser: it stays, unmarked, and only
    the empty fields beside it are filled."""
    typed = {"store_fname": "Jordan", "store_street": "123 Sample Street"}
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == JORDAN
    assert hinted(result) == {"store_lname", "store_zip"}
    assert [field for field, _ in result["remembered"]] == [
        "store_lname",
        "store_zip",
    ]


@needs_node
def test_a_name_already_there_that_is_not_the_letters_fills_nothing(compiled):
    """Then the letter is about someone else, and none of it is theirs."""
    typed = {"store_fname": "Sam"}
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == dict(EMPTY, store_fname="Sam")
    assert hinted(result) == set()


@needs_node
def test_a_last_name_already_there_that_is_not_the_letters_fills_nothing(
    compiled,
):
    typed = {"store_lname": "Sample"}
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == dict(EMPTY, store_lname="Sample")
    assert hinted(result) == set()


@needs_node
def test_a_street_already_there_that_is_not_the_letters_keeps_the_zip_empty(
    compiled,
):
    typed = {"store_street": "9 Other Lane"}
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == dict(JORDAN_NAME_ONLY, store_street="9 Other Lane")


@needs_node
def test_a_zip_already_there_that_is_not_the_letters_keeps_the_street_empty(
    compiled,
):
    typed = {"store_zip": "62702"}
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == dict(JORDAN_NAME_ONLY, store_zip="62702")


@needs_node
def test_a_letter_this_browser_put_back_in_the_box_does_not_fill_on_load(
    compiled,
):
    """Only a letter the server rendered into the page fills on load. One
    restored from this browser's storage was there before, along with what
    the person did to the fields after it."""
    result = run(compiled, letter=TYPICAL_LETTER, arrive="restored")
    assert result["fields"] == EMPTY
    assert result["remembered"] == []


@needs_node
def test_a_field_the_letter_filled_and_the_person_emptied_stays_empty(compiled):
    """Page two pasted under page one does not put back a street the person
    took out, or save it over their empty one."""
    result = run(
        compiled,
        letter=TYPICAL_LETTER,
        arrive="paste",
        clears=["store_street"],
        pasteAgain="\nPage 2 of 2\nYour appeal rights are below.\n",
    )
    assert result["fieldsAtEnd"] == dict(JORDAN, store_street="")
    assert result["hints"]["store_street"] is None
    assert result["remembered"] == [[field, JORDAN[field]] for field in FILLABLE]


@needs_node
def test_a_letter_to_the_doctor_fills_the_patients_name_not_the_doctors(
    compiled,
):
    """A letter to "Casey Doctorson, MD" is to the provider, like one to
    "Dr."; the Patient label names the person."""
    result = run(compiled, letter=TO_THE_DOCTOR_LETTER, arrive="paste")
    assert result["fields"] == JORDAN_NAME_ONLY
    assert result["remembered"] == [
        ["store_fname", "Jordan"],
        ["store_lname", "Example"],
    ]


@needs_node
def test_a_generic_greeting_is_not_taken_for_the_persons_name(compiled):
    """Taken as "Policy Holder", Remove personal details would turn "your
    policy" into "your {{FIRST_NAME}}" and leave the real name in."""
    result = run(compiled, letter=POLICY_HOLDER_LETTER, arrive="paste")
    assert result["fields"] == JORDAN_NAME_ONLY


@needs_node
def test_a_unit_line_fills_the_zip_but_not_the_street(compiled):
    """The street field would drop "Apt 4B": the finished appeal would lose
    it, and Remove personal details would leave it in the letter."""
    result = run(compiled, letter=APARTMENT_LETTER, arrive="paste")
    assert result["fields"] == dict(JORDAN, store_street="")
    assert hinted(result) == {"store_fname", "store_lname", "store_zip"}


@needs_node
def test_a_unit_line_and_a_street_already_there_keep_the_zip_empty(compiled):
    """With no street of the letter's to check it against, the one there
    may be a different address, so the letter's ZIP stays out."""
    typed = {"store_street": "123 Sample Street Apt 4B"}
    result = run(compiled, letter=APARTMENT_LETTER, arrive="paste", typed=typed)
    assert result["fields"] == dict(
        JORDAN_NAME_ONLY, store_street="123 Sample Street Apt 4B"
    )


@needs_node
def test_the_email_is_never_filled(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste")
    assert "jordan@example.com" in TYPICAL_LETTER
    assert result["fields"]["email"] == ""
    assert result["hints"]["email"] is None


@needs_node
def test_a_hint_goes_when_the_person_edits_its_field(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", edits=["store_fname"])
    assert hinted(result) == {"store_lname", "store_street", "store_zip"}
    assert result["describedBy"]["store_fname"] is None
    assert result["note"] == {"text": NOTE, "hidden": False}


@needs_node
def test_the_note_goes_once_every_filled_field_is_checked(compiled):
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste", edits=FILLABLE)
    assert hinted(result) == set()
    assert result["note"] == {"text": "", "hidden": True}


@needs_node
def test_filled_fields_are_kept_the_way_typing_keeps_them(compiled):
    """Through the page's storage helper, which honours "Remember what I
    typed", so the appeal page can put the name back into the letter."""
    result = run(compiled, letter=TYPICAL_LETTER, arrive="paste")
    assert result["remembered"] == [[field, JORDAN[field]] for field in FILLABLE]


@needs_node
@pytest.mark.parametrize("arrive", ["paste", "load", "read", "type"])
def test_nothing_is_logged_sent_or_navigated(compiled, arrive):
    result = run(compiled, letter=TYPICAL_LETTER, arrive=arrive, edits=FILLABLE)
    assert result["logs"] == []
    assert result["network"] == []
    assert result["sockets"] == 0
    assert result["leftThePage"] == []


NAME = {"firstName": "Jordan", "lastName": "Example"}
FULL = dict(NAME, street="123 Sample Street", zip="62701")
SHOUTED = dict(NAME, street="123 SAMPLE STREET", zip="62701")
DOCTOR = "Casey Doctorson\n456 Example Avenue\nAnytown, IL 60001\n"

# What the rule finds in each letter, and why.
RULE_CASES = {
    # A letter to a doctor is the provider's.
    "Dear Dr. Pat Provider,\n" + MEMBER: {},
    "Dear Mr. Jordan Example:\n" + MEMBER: FULL,
    # A salutation alone is not enough: nothing backs it up.
    "Dear JORDAN EXAMPLE:\n": {},
    "Dear Jordan Example,\n" + INSURER + PROVIDER: {},
    "JORDAN EXAMPLE\n123 SAMPLE STREET\nSPRINGFIELD, IL 62701\n\n"
    "Dear JORDAN EXAMPLE:\n": SHOUTED,
    "Dear Jordan McExample,\nMember: Jordan McExample\n": {
        "firstName": "Jordan",
        "lastName": "McExample",
    },
    # A middle name spelled out cannot be told from a two-word last name.
    "Dear Jordan Alex Example,\nJordan Alex Example\n123 Sample Street\n"
    "Springfield, IL 62701\n": {},
    # Generic, or to a team rather than a person.
    "Dear Valued Member,\n" + MEMBER: {},
    "Dear Benefits Administrator,\n": {},
    "Dear Claims Department:\n": {},
    "Dear Jordan Example and Sam Example,\n": {},
    # Two people, by first name or by last name.
    "Dear Jordan Example,\n" + MEMBER + "Page two\nDear Sam Example,\n": {},
    "Dear Jordan Example,\n" + MEMBER + "Page two\nDear Jordan Sample,\n": {},
    "Dear Member,\nSubscriber: Alex Example\nPatient: Jordan Example\n": {},
    "To whom it may concern:\n" + MEMBER: {},
    "Patient: Jordan Example  DOB: 01/01/1980\n": NAME,
    # A label's value stops at the next label, wide gap or not.
    "Dear Member,\nPatient: Jordan Example Date of Birth 01/01/1980\n": NAME,
    "Member name: Example Sample, Jordan\n": {
        "firstName": "Jordan",
        "lastName": "Example Sample",
    },
    "Dear Member: we reviewed your request\n": {},
    "Dear Maria de la Cruz,\nMaria de la Cruz\n9 Sample Road\n"
    "Springfield, IL 62701\n": {
        "firstName": "Maria",
        "lastName": "de la Cruz",
        "street": "9 Sample Road",
        "zip": "62701",
    },
    # Not a state.
    "Member name: Jordan Example\nJordan Example\n123 Sample Street\n"
    "Springfield, ZZ 62701\n": NAME,
    # Two blocks for the same name that disagree.
    "Dear Jordan Example,\n"
    + MEMBER
    + "Jordan Example\n9 Other Lane\nSpringfield, IL 62702\n": NAME,
    # A reading that double-spaces the block.
    "Jordan Example\n\n123 Sample Street\n\nSpringfield, IL 62701\n\n"
    "Dear Jordan Example,\n": FULL,
    # A block under someone else's name.
    "Dear Jordan Example,\nMember name: Jordan Example\nSam Example\n"
    "123 Sample Street\nSpringfield, IL 62701\n": NAME,
    "Member: Jordan Example\n" + INSURER + PROVIDER: NAME,
    # The member's own PO box.
    "JORDAN EXAMPLE\nPO BOX 0000\nSPRINGFIELD IL 62701-0000\n\n"
    "Dear JORDAN EXAMPLE,\n": dict(NAME, street="PO BOX 0000", zip="62701"),
    # A clinician's credential after the comma: to the provider. The
    # Patient label still names the person.
    TO_THE_DOCTOR_LETTER: NAME,
    "Dear Casey Doctorson, M.D.,\nPatient name: Jordan Example\n": NAME,
    "Dear Casey Doctorson, NP:\nPatient: Jordan Example\n": NAME,
    "Dear Casey Doctorson, DO\nCasey Doctorson, DO\n456 Example Avenue\n"
    "Anytown, IL 60001\nPatient: Jordan Example\n": NAME,
    "Dear Casey Doctorson, MD:\n" + DOCTOR: {},
    # Greetings that read like a name. Alone they are not backed up; beside
    # a label for someone else they disagree with it.
    POLICY_HOLDER_LETTER: NAME,
    "Dear Policy Holder,\n": {},
    "Dear Card Holder,\n": {},
    "Dear Covered Person,\n": {},
    "Dear Medicaid Recipient,\n": {},
    "Dear Treating Practitioner,\n": {},
    "Dear Healthcare Professional,\n": {},
    "Dear Review Committee,\n": {},
    "Dear Utilization Management,\n": {},
    "Dear Card Holder,\nMember name: Jordan Example\n": {},
    # A label in capitals with no comma could be either way round: it needs
    # a block under the name, or a salutation that agrees.
    "Dear Member:\nMember Name: EXAMPLE JORDAN    Member ID: XYZ000000\n": {},
    "JORDAN EXAMPLE\n123 SAMPLE STREET\nSPRINGFIELD IL 62701\n\n"
    "Member Name: JORDAN EXAMPLE    Member ID: XYZ000000\n": SHOUTED,
    "Dear Jordan Example,\nMember Name: JORDAN EXAMPLE\n": NAME,
    # A unit line: the ZIP, not the street.
    APARTMENT_LETTER: dict(NAME, zip="62701"),
    # A name line and someone else's address, run together by a reading
    # that dropped one blank line and not the other.
    "Dear Jordan Example,\n\nJordan Example\n\n456 Example Avenue, Suite 200\n"
    "Anytown, IL 60001\n": {},
    "Member name: Jordan Example\n\nJordan Example\n\n"
    "456 Example Avenue, Suite 200\nAnytown, IL 60001\n": NAME,
    # A block under a heading for something else.
    "Dear Member,\nMember name: Jordan Example\nServices for:\nJordan Example\n"
    "789 Hospital Drive\nSpringfield, IL 62702\n": NAME,
}


@needs_node
def test_the_rule_finds_only_what_the_letter_says_for_sure(compiled):
    letters = list(RULE_CASES)
    result = run(compiled, find=letters)
    assert result["logs"] == []
    for letter, found in zip(letters, result["found"]):
        assert found == RULE_CASES[letter], letter


def _js_function(src: str, name: str) -> str:
    """The body of one JS/TS function, brace-matched."""
    start = src.index(name)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


def test_the_page_watches_the_box_after_both_restores():
    """What this browser kept for About you is in its fields before the
    letter is looked at, so it is never written over."""
    setup = _js_function((JS / "scrub.ts").read_text(), "function setupScrub")
    watch = setup.index("watchLetterForDetails(letterBox, setLocalStorageItemWithTTL);")
    assert setup.index("nodes.forEach(handleStorage);") < watch, setup
    assert setup.index("textareas.forEach") < watch, setup


def test_a_file_read_here_fills_once_its_current_selection_is_done():
    reader = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    fill = reader.index(
        "fillDetailsFromLetter(textarea.value, setLocalStorageItemWithTTL);"
    )
    assert reader.index("endOcr(selection);") < fill, reader
    superseded = reader.index("if (selection !== latestOcrSelection) {")
    assert superseded < fill, reader
    assert re.search(
        r"if \(ocrChars > 0\) \{\s*fillDetailsFromLetter\(", reader[superseded:]
    ), reader


def test_the_module_sends_nothing_and_logs_nothing():
    src = MODULE.read_text()
    for call in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "console."):
        assert call not in src, call


def test_it_fills_the_name_street_and_zip_and_never_the_email():
    table = re.search(r"const FIELDS[^=]*=\s*\[(.*?)\];", MODULE.read_text(), re.S)
    assert table, "the field table is gone"
    filled = re.findall(r'\[\s*"\w+",\s*"(\w+)"\s*\]', table.group(1))
    assert tuple(filled) == FILLABLE


class TheFakePageIsTheIntakePageTest(TestCase):
    """The driver's markup is only worth anything while it matches the page."""

    def setUp(self):
        html = self.client.get(reverse("scan")).content.decode()
        self.soup = BeautifulSoup(html, "html.parser")

    def test_each_field_sits_in_its_own_group_where_its_hint_goes(self):
        driver = DRIVER.read_text()
        for field in EVERY_FIELD + ("denial_text", "details_from_letter"):
            with self.subTest(field=field):
                self.assertEqual(len(self.soup.find_all(id=field)), 1)
                self.assertIn(f'id="{field}"', driver)
        for field in FILLABLE:
            with self.subTest(field=field):
                group = self.soup.find(id=field).parent
                self.assertIn("fhi-field-group", group.get("class", []))

    def test_the_name_and_street_stay_in_the_browser(self):
        """No form name, so the form never sends them. The ZIP has one, as it
        always had (the server keeps three digits of it)."""
        for field in ("store_fname", "store_lname", "store_street"):
            with self.subTest(field=field):
                self.assertIsNone(self.soup.find(id=field).get("name"))
        self.assertEqual(self.soup.find(id="store_zip").get("name"), "zip")

    def test_the_note_waits_hidden_and_empty_under_the_about_you_heading(self):
        note = self.soup.find(id="details_from_letter")
        self.assertIn("hidden", note.attrs)
        self.assertEqual(note.get_text(), "")
        self.assertEqual(note.get("role"), "status")
        heading = note.find_previous_sibling(True)
        self.assertEqual(heading.name, "h2")
        self.assertEqual(heading.get_text(strip=True), "About you")

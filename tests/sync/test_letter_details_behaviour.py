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
it like a typed one: see TheFakePageIsTheIntakePageTest.) Some run the whole
intake script (scrub.ts), which sets the page up as it loads, with pdf.js
stood in for by text items like the ones it hands over for a text PDF. A
few run what takes the person's details back out of a letter: Remove
personal details on the intake page, and the chat's scrubPersonalInfo.

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
# Compiled with it: the PDF text join, the intake script that uses both, and
# the chat's scrubber.
ALSO_COMPILED = (JS / "pdf_text.ts", JS / "scrub.ts", JS / "user_info_storage.ts")
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
    """The real letter_details.ts, compiled the way the bundle is built, with
    pdf_text.ts and scrub.ts (and what it imports) beside it."""
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
            *(str(path) for path in ALSO_COMPILED),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    built = out / "letter_details.js"
    for name in (
        "letter_details.js",
        "pdf_text.js",
        "scrub.js",
        "user_info_storage.js",
    ):
        if not (out / name).exists():
            pytest.fail(
                f"tsc did not emit {name}\n"
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
def test_a_street_the_person_typed_and_then_emptied_stays_empty(compiled):
    """The letter did not fill it (it was already theirs), so it was not
    among the fields the letter had filled; emptying it is still their
    choice, and page two pasted under page one does not undo it."""
    result = run(
        compiled,
        types={"store_street": "123 Sample Street"},
        letter=TYPICAL_LETTER,
        arrive="paste",
        clears=["store_street"],
        pasteAgain="\nPage 2 of 2\n" + MEMBER + "Your appeal rights are below.\n",
    )
    assert result["fields"] == JORDAN
    assert result["fieldsAtEnd"] == dict(JORDAN, store_street="")
    assert result["hints"]["store_street"] is None
    assert "store_street" not in [field for field, _ in result["remembered"]]


@needs_node
def test_a_street_the_person_typed_and_emptied_keeps_the_zip_out_too(compiled):
    """Type a street that is not the letter's, paste, empty the street, paste
    page two: the ZIP goes in only with the letter's street, and the street
    field is the person's, so it stays empty and so does the ZIP."""
    result = run(
        compiled,
        types={"store_street": "9 Other Lane"},
        letter=TYPICAL_LETTER,
        arrive="paste",
        clears=["store_street"],
        pasteAgain="\nPage 2 of 2\n" + MEMBER + "Your appeal rights are below.\n",
    )
    assert result["fields"] == dict(JORDAN_NAME_ONLY, store_street="9 Other Lane")
    assert result["fieldsAtEnd"] == JORDAN_NAME_ONLY
    assert result["hints"]["store_zip"] is None
    assert [field for field, _ in result["remembered"]] == [
        "store_fname",
        "store_lname",
    ]


@needs_node
def test_the_zip_goes_in_beside_the_letters_street_already_there(compiled):
    """The other way round: the street the person typed is the letter's, so
    its ZIP goes in beside it."""
    result = run(
        compiled,
        types={"store_street": "123 Sample Street"},
        letter=TYPICAL_LETTER,
        arrive="paste",
    )
    assert result["fields"] == JORDAN
    assert hinted(result) == {"store_fname", "store_lname", "store_zip"}


@needs_node
def test_a_restored_field_emptied_without_an_input_event_is_not_filled(compiled):
    """What this browser put back is the person's, even once something other
    than their typing (a form reset) has emptied it."""
    result = run(
        compiled,
        typed={"store_street": "123 Sample Street"},
        resets=["store_street"],
        letter=TYPICAL_LETTER,
        arrive="paste",
    )
    # The ZIP goes in only beside the letter's street, and the street is
    # not going in.
    assert result["fields"] == JORDAN_NAME_ONLY
    assert hinted(result) == {"store_fname", "store_lname"}


@needs_node
@pytest.mark.parametrize("arrive", ["paste", "load", "read", "type"])
def test_nothing_is_logged_sent_or_navigated(compiled, arrive):
    result = run(compiled, letter=TYPICAL_LETTER, arrive=arrive, edits=FILLABLE)
    assert result["logs"] == []
    assert result["network"] == []
    assert result["sockets"] == 0
    assert result["leftThePage"] == []


# A page with a full or blocked storage, and a fill that fails outright:
# the rest of the page is wired up either way.
WIRED = {
    "upload": 1,
    "paste": 1,
    "removePersonalDetails": True,
    "submitCheck": 1,
}


@needs_node
def test_a_full_storage_still_fills_and_the_page_is_still_set_up(compiled):
    """Storage that takes no writes (QuotaExceededError) used to throw out
    of the fill on load and stop the page's setup before the file button,
    Remove personal details and the submit check were wired up. The fields
    are filled and marked; they are just not remembered."""
    result = run(compiled, page=True, storageFull=True, letter=TYPICAL_LETTER)
    assert result["setupError"] is None
    assert result["wired"] == WIRED
    assert result["fields"] == JORDAN
    assert hinted(result) == set(FILLABLE)
    assert result["note"] == {"text": NOTE, "hidden": False}
    assert result["stored"] == []


@needs_node
def test_a_blocked_storage_still_sets_the_page_up_and_fills(compiled):
    """Where the browser blocks this site's storage, reading localStorage
    throws. That used to stop the page's setup at the "Remember what I
    typed" box, before About you, the file button, Remove personal details
    and the submit check, and to throw again when the document finished
    loading. Blocked storage now reads as remembering turned off: the box
    shows unticked, the fields are filled and marked, and nothing is kept."""
    result = run(compiled, page=True, storageBlocked=True, letter=TYPICAL_LETTER)
    assert result["setupError"] is None
    assert result["domReadyError"] is None
    assert result["unhandled"] == []
    assert result["wired"] == WIRED
    assert result["remembering"] is False
    assert result["fields"] == JORDAN
    assert hinted(result) == set(FILLABLE)
    assert result["note"] == {"text": NOTE, "hidden": False}
    assert result["stored"] == []


@needs_node
def test_the_page_with_working_storage_fills_and_remembers(compiled):
    """The same page with storage that works, so the tests above are about
    the storage and nothing else."""
    result = run(compiled, page=True, letter=TYPICAL_LETTER)
    assert result["setupError"] is None
    assert result["domReadyError"] is None
    assert result["wired"] == WIRED
    assert result["remembering"] is True
    assert result["fields"] == JORDAN
    assert set(result["stored"]) == set(FILLABLE)


@needs_node
def test_ticking_remember_with_blocked_storage_unticks_and_never_throws(compiled):
    """Ticking "Remember what I typed" wrote the setting to storage the
    browser blocks, so the change threw a SecurityError and the box stayed
    ticked, saying typing was kept when nothing could be. It goes back to
    unticked."""
    result = run(compiled, page=True, storageBlocked=True, persistenceClicks=[True])
    assert result["rememberErrors"] == []
    assert result["unhandled"] == []
    assert result["remembering"] is False
    assert result["stored"] == []


@needs_node
def test_remember_with_full_storage_never_throws(compiled):
    """A full storage takes no writes, the setting included: unticking and
    ticking again throw nothing."""
    result = run(compiled, page=True, storageFull=True, persistenceClicks=[False, True])
    assert result["rememberErrors"] == []
    assert result["unhandled"] == []


@needs_node
def test_remember_with_working_storage_unticks_and_ticks_again(compiled):
    """The same box with storage that works, so the tests above are about
    the storage: unticked it stays unticked, and ticked again it keeps."""
    unticked = run(compiled, page=True, persistenceClicks=[False])
    assert unticked["rememberErrors"] == []
    assert unticked["remembering"] is False
    ticked = run(compiled, page=True, persistenceClicks=[False, True])
    assert ticked["rememberErrors"] == []
    assert ticked["remembering"] is True


@needs_node
def test_a_broken_fill_never_stops_the_page(compiled):
    """Whatever goes wrong in the fill, on load or after a file is read, the
    page is set up and the read finishes; the console gets the kind of error
    and nothing from the letter."""
    result = run(
        compiled,
        page=True,
        fillThrows=True,
        letter=TYPICAL_LETTER,
        pdf=[pdf_page(PDF_LETTER)],
    )
    assert result["setupError"] is None
    assert result["unhandled"] == []
    # Everything but the fill's own paste listener, which is what broke.
    assert result["wired"] == dict(WIRED, paste=0)
    assert result["fields"] == EMPTY
    assert result["box"] == TYPICAL_LETTER + as_read(PDF_LETTER)
    warning = ["warn", "scrub: About you was not filled from the letter:", "TypeError"]
    # Once on load, once after the file was read.
    warned = [entry for entry in result["logs"] if entry[0] == "warn"]
    assert warned == [warning, warning]
    assert not any("Jordan" in " ".join(entry) for entry in result["logs"])


def text_run(text: str, baseline: float, x: float = 72, height: float = 12, width=None):
    """One of pdf.js's text items, as getTextContent hands it over: 12 point
    text, each character half an em wide unless the width is given."""
    return {
        "str": text,
        "dir": "ltr",
        "width": 6.0 * len(text) if width is None else width,
        "height": height,
        "transform": [12, 0, 0, 12, x, baseline],
        "fontName": "g_d0_f1",
        "hasEOL": False,
    }


def ends_the_line(item: dict) -> dict:
    return dict(item, hasEOL=True)


# The gap pdf.js leaves between two columns, in points: five ems.
COLUMN_GAP = 60


def pdf_page(letter: str) -> list:
    """The items pdf.js hands over for a text PDF page carrying this letter.
    Each line is one run, or a run per column where the line has a wide gap
    (two spaces or more here), each column COLUMN_GAP points right of where
    the one before it ends, with pdf.js's own " " run, as wide as the gap,
    between them; each baseline is 14 points under the one above; two line
    ends in three are marked hasEOL (pdf.js does not mark every break, so
    the rest end where the baseline moves); and the page is wrapped in
    marked content, which carries no text. A blank line leaves no item, as
    in a PDF."""
    items = [{"type": "beginMarkedContent", "id": "mc0"}]
    lines = [line.strip() for line in letter.split("\n") if line.strip()]
    for number, line in enumerate(lines):
        baseline = 720 - 14 * number
        x = 72
        for column, part in enumerate(re.split(r"\s{2,}", line)):
            if column:
                items.append(text_run(" ", baseline, x=x, height=0, width=COLUMN_GAP))
                x += COLUMN_GAP
            items.append(text_run(part, baseline, x=x))
            x += items[-1]["width"]
        if number % 3 != 2:
            items[-1] = ends_the_line(items[-1])
    items.append({"type": "endMarkedContent"})
    return items


def as_read(letter: str) -> str:
    """The letter as the box gets it from a text PDF page: a line per line,
    blank lines gone, a wide gap a tab, and the page's closing break."""
    lines = [line.strip() for line in letter.split("\n") if line.strip()]
    return "\n".join(re.sub(r"\s{2,}", "\t", line) for line in lines) + "\n"


# A text PDF of the typical letter, with a second column on one line.
PDF_LETTER = TYPICAL_LETTER.replace(
    "Member ID: XYZ000000\n", "Member ID: XYZ000000    Group number: 00000\n"
)


@needs_node
def test_a_text_pdf_keeps_its_lines_and_fills_about_you(compiled):
    """Its runs used to be joined with spaces into one line, so the block
    under the person's name was lost and nothing was filled. A gap between
    columns shows in the box as a tab."""
    result = run(compiled, page=True, pdf=[pdf_page(PDF_LETTER)])
    assert result["setupError"] is None
    assert "Member ID: XYZ000000\tGroup number: 00000\n" in result["box"]
    assert result["box"] == as_read(PDF_LETTER)
    assert result["fields"] == JORDAN
    assert hinted(result) == set(FILLABLE)


@needs_node
def test_a_right_column_on_the_street_line_is_not_part_of_the_street(compiled):
    """A letter often prints the member ID in a right column on the street
    line's baseline. Joined with a space it read as "123 Sample Street
    Member ID: XYZ000000", which was put in the street field."""
    letter = TYPICAL_LETTER.replace(
        "123 Sample Street\n", "123 Sample Street        Member ID: XYZ000000\n"
    )
    result = run(compiled, page=True, pdf=[pdf_page(letter)])
    assert "123 Sample Street\tMember ID: XYZ000000\n" in result["box"]
    assert result["fields"] == JORDAN


@needs_node
def test_a_member_name_label_with_a_right_column_fills_the_name(compiled):
    """The words "Service Date" do not end a label, so read across the gap
    the name was "Jordan Example Service" and nothing was filled."""
    # Long enough that the page's text layer is read rather than scanned.
    letter = DEAR_MEMBER_LETTER.replace(
        "Member name: Jordan Example\n",
        "Member name: Jordan Example        Service Date: 09/01/2026\n",
    ) + (
        "We found the MRI your doctor asked for not medically necessary.\n"
        "You can appeal this decision within 180 days of this letter.\n"
    )
    result = run(compiled, page=True, pdf=[pdf_page(letter)])
    assert "Member name: Jordan Example\tService Date: 09/01/2026\n" in result["box"]
    assert result["fields"] == JORDAN_NAME_ONLY


# Long enough that a page's text layer is read rather than scanned.
FINDING = "We reviewed your request for an MRI and found it not medically necessary."


@needs_node
def test_a_heading_in_a_right_column_keeps_the_block_under_it_out(compiled):
    """A text PDF that puts "Services for:" in a right column on the
    "Member ID:" line, over the facility's block, which starts with the
    person's name. Only the first column of the line above was checked
    for a heading, so the facility's street and ZIP were filled in as the
    person's."""
    page = [
        ends_the_line(
            text_run("Acme Example Health Plan", 740, height=11, width=133.88)
        ),
        ends_the_line(text_run("PO Box 0000", 726, height=11, width=65.43)),
        ends_the_line(text_run("Anytown, NY 00000", 712, height=11, width=97.22)),
        ends_the_line(text_run("Dear Member,", 680, height=11, width=70.29)),
        ends_the_line(
            text_run("Member name: Jordan Example", 660, height=11, width=156.5)
        ),
        text_run("Member ID: XYZ000000", 640, height=11, width=118.6),
        text_run(" ", 640, x=190.6, height=0, width=159.4),
        ends_the_line(text_run("Services for:", 640, x=350, height=11, width=61.13)),
        ends_the_line(text_run("Jordan Example", 626, x=350, height=11, width=79.47)),
        ends_the_line(
            text_run("789 Hospital Drive", 612, x=350, height=11, width=89.86)
        ),
        ends_the_line(
            text_run("Springfield, IL 62702", 598, x=350, height=11, width=100.89)
        ),
        text_run(FINDING, 570, height=11, width=366.2),
    ]
    result = run(compiled, page=True, pdf=[page])
    assert "Member ID: XYZ000000\tServices for:\nJordan Example\n" in result["box"]
    assert result["fields"] == JORDAN_NAME_ONLY


def courier_run(text: str, baseline: float, x: float = 72):
    """A run of 10 point Courier, as pdf.js hands it over: each character
    six points (0.6 em) wide."""
    return dict(
        text_run(text, baseline, x=x, height=10, width=6.0 * len(text)),
        transform=[10, 0, 0, 10, x, baseline],
    )


@needs_node
def test_a_zip_two_monospaced_spaces_past_the_state_is_read(compiled):
    """A monospaced PDF that places "62701" two spaces (1.2 em) past
    "Springfield, IL". The gap comes through as a tab, and the city line
    cut at it had no ZIP, so no address was filled."""
    page = [
        ends_the_line(courier_run("Acme Example Health Plan", 740)),
        ends_the_line(courier_run("Jordan Example", 700)),
        ends_the_line(courier_run("123 Sample Street", 688)),
        courier_run("Springfield, IL", 676),
        text_run(" ", 676, x=162, height=0, width=12),
        ends_the_line(courier_run("62701", 676, x=174)),
        ends_the_line(courier_run("Dear Jordan Example,", 640)),
        ends_the_line(courier_run(FINDING, 620)),
        courier_run("You can appeal this decision within 180 days.", 608),
    ]
    result = run(compiled, page=True, pdf=[page])
    assert "Springfield, IL\t62701\n" in result["box"]
    assert result["fields"] == JORDAN


@needs_node
def test_remove_personal_details_finds_a_street_the_letter_splits(compiled):
    """The person typed "123 Sample Street Apt 4B"; the letter puts "Apt
    4B" on the line under the street. Matched with its literal spaces, the
    street and the unit were left in the letter."""
    typed = {
        "store_fname": "Jordan",
        "store_lname": "Example",
        "store_street": "123 Sample Street Apt 4B",
    }
    result = run(
        compiled,
        page=True,
        typed=typed,
        letter=APARTMENT_LETTER,
        removePersonalDetails=True,
    )
    assert result["removeError"] is None
    assert "{{ADDRESS}}" in result["box"]
    assert "Sample Street" not in result["box"]
    assert "Apt 4B" not in result["box"]


def turned_run(text: str, x: float, along: float):
    """A run of text turned a quarter, up the page: its baseline is
    upright, so its height on the page moves along the line."""
    return dict(text_run(text, along, x=x), transform=[0, 12, -12, 0, x, along])


@needs_node
def test_pdf_text_items_come_out_a_line_per_line(compiled):
    page = [
        {"type": "beginMarkedContentProps", "id": "mc0"},
        # pdf.js's own space between two runs is one space.
        text_run("Jordan", 700),
        text_run(" ", 700, height=0),
        ends_the_line(text_run("Example", 700)),
        # A right column on the street line's baseline, five ems past where
        # the street ends (at 174): the gap is a tab, not a space.
        text_run("123 Sample Street", 686),
        text_run(" ", 686, x=174, height=0, width=60),
        ends_the_line(text_run("Member ID: XYZ000000", 686, x=234)),
        # pdf.js's empty run that marks the same break again: no blank line.
        ends_the_line(text_run("", 686)),
        # Two runs on one line with no space run between them.
        text_run("Springfield, IL", 672),
        text_run("62701", 672, x=160),
        # pdf.js's empty run that only ends a line.
        ends_the_line(text_run("", 672)),
        # A run that ends in a space, then pdf.js's space runs, with less
        # than an em between the runs (the label ends at 150): still one
        # space. Then a right column, far past the name: a tab.
        text_run("Member name: ", 658),
        text_run(" ", 658, height=0),
        text_run(" ", 658, height=0),
        text_run("Jordan Example", 658, x=160),
        text_run(" ", 658, x=244, height=0, width=116),
        text_run("Service Date: 09/01/2026", 658, x=360),
        # Not marked hasEOL above, but on the next baseline down.
        text_run("Member ID: XYZ000000", 644),
        # A footnote mark a little above the line stays on it.
        text_run("1", 648, x=200, height=7),
        text_run("Dear Jordan Example,", 620),
        {"type": "endMarkedContent"},
    ]
    # A monospaced letter: each word placed on its own, with one space (0.6
    # em) between, which pdf.js may mark with a " " run. A word space, not a
    # tab.
    monospaced = [
        text_run("123", 700, width=21.6),
        text_run(" ", 700, x=93.6, height=0, width=7.2),
        text_run("Sample", 700, x=100.8, width=43.2),
        text_run(" ", 700, x=144, height=0, width=7.2),
        ends_the_line(text_run("Street", 700, x=151.2, width=43.2)),
    ]
    # Turned text: only hasEOL says where its lines end.
    turned = [
        ends_the_line(turned_run("Page 2 of 2", 40, 300)),
        turned_run("Your appeal", 54, 300),
        ends_the_line(turned_run("rights", 54, 380)),
    ]
    result = run(compiled, pdfText=[page, turned, monospaced, []])
    assert result["texts"] == [
        "Jordan Example\n"
        "123 Sample Street\tMember ID: XYZ000000\n"
        "Springfield, IL 62701\n"
        "Member name: Jordan Example\tService Date: 09/01/2026\n"
        "Member ID: XYZ000000 1\n"
        "Dear Jordan Example,",
        "Page 2 of 2\nYour appeal rights",
        "123 Sample Street",
        "",
    ]
    assert result["logs"] == []


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
    # Another column past a wide gap (a tab, or three spaces or more) is not
    # part of the street, the name line or the city line.
    "Dear Jordan Example,\nJordan Example\n"
    "123 Sample Street      Member ID: XYZ000000\nSpringfield, IL 62701\n": FULL,
    "Dear Jordan Example,\nJordan Example\tOctober 1, 2026\n"
    "123 Sample Street\tMember ID: XYZ000000\n"
    "Springfield, IL 62701\tGroup number: 00000\n": FULL,
    # Two spaces are not a gap: OCR leaves them inside a street.
    "Dear Jordan Example,\nJordan Example\n123  Sample Street\n"
    "Springfield, IL 62701\n": dict(FULL, street="123 Sample Street"),
    # A unit in the column beside the street: the ZIP, not the street.
    "Dear Jordan Example,\nJordan Example\n123 Sample Street\tApt 4B\n"
    "Springfield, IL 62701\n": dict(NAME, zip="62701"),
    # A label's value runs from past the gap after the label to the next one.
    "Dear Member,\nMember name:\tJordan Example\tService Date: 09/01/2026\n": NAME,
    "Dear Member,\nMember name: Jordan Example    Service Date: 09/01/2026\n": NAME,
    # For a label, two spaces are a gap.
    "Dear Member,\nMember name: Jordan Example  Service Date: 09/01/2026\n": NAME,
    # ...but only before another label: a name that runs on past two spaces
    # gives nothing rather than a wrong last name.
    "Dear Member,\nMember name: Jordan Lee  Example\n": {},
    "Dear Member,\nPatient: Jordan  Example\n": {},
    # A tab is a column whatever follows it.
    "Dear Member,\nMember name: Jordan Example\tAccount 12345\n": NAME,
    # A heading in any column of the line above, from a PDF (a tab) or pasted
    # (a run of spaces, and the block indented under it).
    "Dear Member,\nMember name: Jordan Example\nMember ID: XYZ000000\tServices for:\n"
    "Jordan Example\n789 Hospital Drive\nSpringfield, IL 62702\n": NAME,
    "Dear Member,\nMember name: Jordan Example\n"
    "Claim number: 0000000\tRendering provider:\nJordan Example\n"
    "789 Hospital Drive\nSpringfield, IL 62702\n": NAME,
    "Dear Member,\nMember name: Jordan Example\n"
    "Member ID: XYZ000000      Services for:\n"
    "                              Jordan Example\n"
    "                              789 Hospital Drive\n"
    "                              Springfield, IL 62702\n": NAME,
    # The city line is read whole: any gap between the city, the state and
    # the ZIP, and the ZIP up to the end or another column.
    "Dear Jordan Example,\nJordan Example\n123 Sample Street\n"
    "Springfield, IL\t62701\n": FULL,
    "Dear Jordan Example,\nJordan Example\n123 Sample Street\n"
    "Springfield,\tIL\t62701-0000\tGroup number: 00000\n": FULL,
    # But a column gap does not join two columns into one city.
    "Dear Jordan Example,\nMember name: Jordan Example\nJordan Example\n"
    "123 Sample Street\nSpringfield      Anytown, IL 60001\n": NAME,
}


# Middle initials: two that differ are two people, so nothing is filled;
# a name with one and the same name without one are the same person.
DIFFERENT_INITIALS = (
    "Dear Jordan A. Example,\nMember name: Jordan B. Example\n",
    "Member: Example, Jordan A\nPatient: Jordan B Example\n",
    # Each agrees with the salutation, but not with each other.
    "Dear Jordan Example,\nMember: Jordan A. Example\nPatient: Jordan B. Example\n",
    # The block under a different initial is someone else's, so nothing
    # backs the salutation up.
    "Dear Jordan A. Example,\nJordan B. Example\n123 Sample Street\n"
    "Springfield, IL 62701\n",
    # And so is one under a middle name spelled out.
    "Dear Jordan A. Example,\nJordan Bob Example\n123 Sample Street\n"
    "Springfield, IL 62701\n",
)
# A block whose name line has a middle name spelled out fills no address,
# whatever else names the person.
SPELLED_OUT_MIDDLE = {
    "Dear Jordan A. Example,\nMember name: Jordan A. Example\n"
    "Jordan Bob Example\n123 Sample Street\nSpringfield, IL 62701\n": NAME,
    "Dear Jordan Example,\nMember name: Jordan Example\n"
    "Example, Jordan Bob\n123 Sample Street\nSpringfield, IL 62701\n": NAME,
}
ONE_INITIAL = {
    "Dear Jordan A. Example,\nMember name: Jordan Example\n": NAME,
    "Dear Jordan Example,\nMember name: Jordan B. Example\n": NAME,
    "Dear Jordan A. Example,\nJordan A Example\n123 Sample Street\n"
    "Springfield, IL 62701\n": FULL,
    "Dear Jordan Example,\nJordan A. Example\n123 Sample Street\n"
    "Springfield, IL 62701\n": FULL,
}


@needs_node
def test_two_different_middle_initials_are_two_people(compiled):
    result = run(compiled, find=list(DIFFERENT_INITIALS))
    for letter, found in zip(DIFFERENT_INITIALS, result["found"], strict=True):
        assert found == {}, letter


@needs_node
def test_a_block_under_a_spelled_out_middle_name_is_not_the_persons(compiled):
    letters = list(SPELLED_OUT_MIDDLE)
    result = run(compiled, find=letters)
    for letter, found in zip(letters, result["found"], strict=True):
        assert found == SPELLED_OUT_MIDDLE[letter], letter


@needs_node
def test_a_middle_initial_on_one_name_only_still_agrees(compiled):
    letters = list(ONE_INITIAL)
    result = run(compiled, find=letters)
    for letter, found in zip(letters, result["found"], strict=True):
        assert found == ONE_INITIAL[letter], letter


@needs_node
def test_the_rule_finds_only_what_the_letter_says_for_sure(compiled):
    letters = list(RULE_CASES)
    result = run(compiled, find=letters)
    assert result["logs"] == []
    for letter, found in zip(letters, result["found"], strict=True):
        assert found == RULE_CASES[letter], letter


# The chat's scrubPersonalInfo, with what the person gave the consent form.
# The state is deliberately left in (see user_info_storage.ts).
CHAT_USER = {
    "firstName": "Jordan",
    "lastName": "Example",
    "email": "jordan@example.com",
    "address": "123 Sample Street Apt 4B",
    "city": "Springfield",
    "state": "IL",
    "zipCode": "62701",
    "acceptedTerms": True,
}
SCRUBBED = {
    # The street and its unit on two lines, as a letter prints them.
    "Jordan Example\n123 Sample Street\nApt 4B\nSpringfield, IL 62701\n": (
        "{{PATIENT_NAME}}\n{{ADDRESS}}\n{{CITY}}, IL {{ZIP_CODE}}\n"
    ),
    # A tab or a run of spaces between the words.
    "I live at 123 Sample Street\tApt 4B.": "I live at {{ADDRESS}}.",
    "I live at 123  Sample   Street Apt 4B.": "I live at {{ADDRESS}}.",
    # The name across a line break.
    "Dear Jordan\nExample, write to jordan@example.com": (
        "Dear {{PATIENT_NAME}}, write to {{Your Email Address}}"
    ),
}


@needs_node
def test_the_chat_takes_out_what_was_typed_however_the_letter_spaces_it(
    compiled,
):
    messages = list(SCRUBBED)
    result = run(
        compiled, scrubPersonalInfo=[[message, CHAT_USER] for message in messages]
    )
    assert result["logs"] == []
    for message, scrubbed in zip(messages, result["scrubbed"], strict=True):
        assert scrubbed == SCRUBBED[message], message


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
    # And before the file button, Remove personal details and the submit
    # check, which a failed fill must not keep from being wired up.
    assert re.search(
        r"try \{\s*watchLetterForDetails\(letterBox, setLocalStorageItemWithTTL\);"
        r"\s*\} catch",
        setup,
    ), setup


def test_a_file_read_here_fills_once_its_current_selection_is_done():
    reader = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    fill = reader.index(
        "fillDetailsFromLetter(textarea.value, setLocalStorageItemWithTTL);"
    )
    assert reader.index("endOcr(selection);") < fill, reader
    superseded = reader.index("if (selection !== latestOcrSelection) {")
    assert superseded < fill, reader
    assert re.search(
        r"if \(ocrChars > 0\) \{\s*try \{\s*fillDetailsFromLetter\(",
        reader[superseded:],
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

    def test_the_controls_the_page_wires_up_are_the_pages(self):
        """The file button, Remove personal details, Remember what I typed,
        and the four boxes the submit check reads, by the ids and names the
        script looks for."""
        driver = DRIVER.read_text()
        for control in ("uploader", "scrub-2", "persistence_enabled"):
            with self.subTest(control=control):
                self.assertEqual(len(self.soup.find_all(id=control)), 1)
                self.assertIn(f'id="{control}"', driver)
        for box in ("pii", "privacy", "tos", "personalonly"):
            with self.subTest(box=box):
                self.assertEqual(self.soup.find(id=box).get("name"), box)
                self.assertIn(f'id="{box}" name="{box}"', driver)

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

"""What the person typed is taken out of a letter only where it stands as
whole words, run rather than read.

Remove personal details on the intake page (scrub_scrub.ts) and the chat's
scrubPersonalInfo (user_info_storage.ts) look for what the person typed
through typed_value_pattern.ts. It matched anywhere, so a short first name
was taken out of the middle of other words: in a live test one turned
"Example Health Plan" into "Exa {{FIRST_NAME}}ple Health Plan", and an "Ed",
"Al", "Ann" or "Sam" would turn "denied", "medically", "annual" and "same"
into placeholders before the letter was drafted from. A value is now found
only with no letter or digit, in any script, right before or after it.

These compile the real TypeScript with the repo's own tsc (the same flags as
test_entity_fetcher_behaviour.py), run it in node through
tests/js/scrub_whole_words_behaviour.cjs, and assert on the text that comes
back. Remove personal details runs over every input the rendered intake page
has, with the value the page gives each, so a tick box's fixed value is in
play the way it is in a browser.

Skipped, not silently passed, where node or the front-end toolchain is not
installed. Every name, address, email and number here is made up.
"""

import json
import os
import pathlib
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
MODULE = JS / "typed_value_pattern.ts"
# Compiled with it: the two places that use it (and shared.ts, which
# scrub_scrub.ts imports).
ALSO_COMPILED = (JS / "scrub_scrub.ts", JS / "user_info_storage.ts")
DRIVER = REPO_ROOT / "tests" / "js" / "scrub_whole_words_behaviour.cjs"


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """typed_value_pattern.ts and the two scrubbers, compiled the way the
    bundle is built."""
    out = tmp_path_factory.mktemp("scrub-whole-words")
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
    for name in (
        "typed_value_pattern.js",
        "scrub_scrub.js",
        "shared.js",
        "user_info_storage.js",
    ):
        if not (out / name).exists():
            pytest.fail(
                f"tsc did not emit {name}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    assert result.returncode == 0, result.stdout + result.stderr
    return out / "typed_value_pattern.js"


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


def assert_found(compiled: pathlib.Path, cases: dict) -> None:
    """Each (typed value, text) marked where the value is found, [[like
    this]], or None where the value is not looked for at all."""
    pairs = list(cases)
    result = run(compiled, find=[list(pair) for pair in pairs])
    assert result["logs"] == []
    for pair, found in zip(pairs, result["found"], strict=True):
        assert found == cases[pair], pair


@needs_node
def test_a_short_name_is_not_found_inside_other_words(compiled):
    assert_found(
        compiled,
        {
            ("Ed", "Your claim was denied, Ed. Edward reviewed it."): (
                "Your claim was denied, [[Ed]]. Edward reviewed it."
            ),
            ("Al", "Not medically necessary, Al. Alaska. Total."): (
                "Not medically necessary, [[Al]]. Alaska. Total."
            ),
            ("Ann", "Your annual limit, Ann. Annette. Joann. Planned."): (
                "Your annual limit, [[Ann]]. Annette. Joann. Planned."
            ),
            ("Sam", "The same as Sam asked. Samuel. Balsam."): (
                "The same as [[Sam]] asked. Samuel. Balsam."
            ),
            # The live report's letter, with a two-letter name in it.
            ("Am", "Example Health Plan"): "Example Health Plan",
        },
    )


@needs_node
def test_a_name_is_found_at_either_end_of_the_text_and_beside_punctuation(
    compiled,
):
    assert_found(
        compiled,
        {
            ("Ann", "Ann"): "[[Ann]]",
            ("Ann", "Ann, your claim"): "[[Ann]], your claim",
            ("Ann", "This is for Ann."): "This is for [[Ann]].",
            ("Ann", "(Ann) “Ann” Ann's file; Ann’s plan"): (
                "([[Ann]]) “[[Ann]]” [[Ann]]'s file; [[Ann]]’s plan"
            ),
            ("Ann", "Re:Ann\nAnn\tDoe"): "Re:[[Ann]]\n[[Ann]]\tDoe",
            # A hyphen is not part of a word: this is still the person's name.
            ("Doe", "Ann Doe-Rivera"): "Ann [[Doe]]-Rivera",
        },
    )


@needs_node
def test_an_apostrophe_joined_to_a_word_is_part_of_it(compiled):
    assert_found(
        compiled,
        {
            ("Don", "We don't cover it. Don't wait, Don."): (
                "We don't cover it. Don't wait, [[Don]]."
            ),
            ("Brien", "O'Brien and O’Brien, not Brien"): (
                "O'Brien and O’Brien, not [[Brien]]"
            ),
            # Typed straight, printed curly, and the other way round.
            ("O'Brien", "Dear O’Brien, O'Brien's file"): (
                "Dear [[O’Brien]], [[O'Brien]]'s file"
            ),
            ("O’Brien", "O'BRIEN"): "[[O'BRIEN]]",
        },
    )


@needs_node
def test_a_name_of_several_words_is_found_across_line_breaks(compiled):
    assert_found(
        compiled,
        {
            ("Ann Doe", "Dear Ann\nDoe,\nAnn  Doe\tor Ann Doering"): (
                "Dear [[Ann\nDoe]],\n[[Ann  Doe]]\tor Ann Doering"
            ),
            ("Mary Ann", "Rosemary Ann and Mary Annette"): (
                "Rosemary Ann and Mary Annette"
            ),
        },
    )


@needs_node
def test_case_does_not_matter(compiled):
    assert_found(
        compiled,
        {
            ("ann doe", "ANN DOE, Ann Doe and ann doe"): (
                "[[ANN DOE]], [[Ann Doe]] and [[ann doe]]"
            ),
            ("JOSÉ", "josé and José"): "[[josé]] and [[José]]",
        },
    )


@needs_node
def test_accented_names_are_whole_words_too(compiled):
    """\\b knows only A to Z: it never ended a match after the "é" of
    "José", and it let "Jos" match inside "José"."""
    assert_found(
        compiled,
        {
            ("José", "Dear José, José's claim. Joséphine."): (
                "Dear [[José]], [[José]]'s claim. Joséphine."
            ),
            ("Jos", "Dear José"): "Dear José",
            ("Zoë", "Zoë. Zoëlla."): "[[Zoë]]. Zoëlla.",
            ("Núñez", "Ann Núñez-Ruiz, Núñezes"): "Ann [[Núñez]]-Ruiz, Núñezes",
            # A letter or a mark after the value stops it: "José" written
            # with a combining accent is not a typed "Jose".
            ("Jose", "Jose\u0301 and Jose."): "Jose\u0301 and [[Jose]].",
        },
    )


@needs_node
def test_names_in_other_scripts_are_whole_words_too(compiled):
    assert_found(
        compiled,
        {
            ("Мария", "Уважаемая Мария, Марияна"): "Уважаемая [[Мария]], Марияна",
            ("Ελένη", "Dear Ελένη, Ελένης"): "Dear [[Ελένη]], Ελένης",
            ("王伟", "Patient: 王伟, 王伟明"): "Patient: [[王伟]], 王伟明",
            ("김민준", "Dear 김민준."): "Dear [[김민준]].",
        },
    )


@needs_node
def test_a_lone_initial_is_not_looked_for(compiled):
    """As a whole word an initial is also "a", "I", Medicare's "Part B" and
    every "1." in a list, and on its own it says almost nothing about who
    someone is. One character from a script without capitals is a whole
    name, and still found."""
    assert_found(
        compiled,
        {
            ("A", "a plan"): None,
            ("J.", "J. Doe"): None,
            ("B", "Medicare Part B"): None,
            ("7", "1. 7 days"): None,
            ("-", "a - b"): None,
            ("", "text"): None,
            ("   ", "text"): None,
            ("王", "Patient: 王, Wei. 王国"): "Patient: [[王]], Wei. 王国",
        },
    )


@needs_node
def test_addresses_and_numbers_are_found_only_whole(compiled):
    assert_found(
        compiled,
        {
            ("123 Sample Street Apt 4B", "123 Sample Street\nApt 4B\n"): (
                "[[123 Sample Street\nApt 4B]]\n"
            ),
            ("123 Sample Street", "1123 Sample Street; 123 Sample Streets"): (
                "1123 Sample Street; 123 Sample Streets"
            ),
            ("4B", "Apt 4B, not 14B or 4BX"): "Apt [[4B]], not 14B or 4BX",
            ("#4B", "Unit #4B"): "Unit #[[4B]]",
            # Punctuation at the ends of what was typed is left off.
            ("123 Sample St.", "123 Sample St, Springfield"): (
                "[[123 Sample St]], Springfield"
            ),
            ("62701", "IL 62701-0000, claim 627012, 162701"): (
                "IL [[62701]]-0000, claim 627012, 162701"
            ),
            ("XYZ000000", "Member ID: XYZ000000. Ref XYZ0000001"): (
                "Member ID: [[XYZ000000]]. Ref XYZ0000001"
            ),
            ("ann.doe@example.com", "jann.doe@example.com or ann.doe@example.com."): (
                "jann.doe@example.com or [[ann.doe@example.com]]."
            ),
        },
    )


# The chat's scrubPersonalInfo, with what the person gave the consent form.
CHAT_USER = {
    "firstName": "Ann",
    "lastName": "Doe",
    "email": "ann@example.com",
    "address": "123 Sample Street Apt 4B",
    "city": "Mesa",
    "state": "AZ",
    "zipCode": "85201",
    "acceptedTerms": True,
}
ACCENTED_USER = dict(CHAT_USER, firstName="José", lastName="Núñez")
CHAT_SCRUBBED = [
    (
        "My annual limit was reached and Annette Doering was denied, Ann.",
        CHAT_USER,
        "My annual limit was reached and Annette Doering was denied, {{FIRST_NAME}}.",
    ),
    (
        "Ann\nDoe, 123 Sample Street\nApt 4B, Mesa AZ 85201-1234",
        CHAT_USER,
        "{{PATIENT_NAME}}, {{ADDRESS}}, {{CITY}} AZ {{ZIP_CODE}}-1234",
    ),
    (
        "Mesalamine was denied. Write to joann@example.com or ann@example.com.",
        CHAT_USER,
        "Mesalamine was denied. Write to joann@example.com or {{Your Email Address}}.",
    ),
    # Matched anywhere, the street was found inside a longer number.
    (
        "1123 Sample Street Apt 4B, claim 852011",
        CHAT_USER,
        "1123 Sample Street Apt 4B, claim 852011",
    ),
    # \b never matched these at all: the name was sent as typed.
    (
        "Dear José, the Núñez family's appeal",
        ACCENTED_USER,
        "Dear {{FIRST_NAME}}, the {{LAST_NAME}} family's appeal",
    ),
    (
        "José Núñez",
        ACCENTED_USER,
        "{{PATIENT_NAME}}",
    ),
]


@needs_node
def test_the_chat_takes_out_whole_words_only(compiled):
    result = run(compiled, chat=[[message, user] for message, user, _ in CHAT_SCRUBBED])
    assert result["logs"] == []
    for (message, _, expected), scrubbed in zip(
        CHAT_SCRUBBED, result["scrubbed"], strict=True
    ):
        assert scrubbed == expected, message


@needs_node
def test_nothing_needs_a_lookbehind_that_older_safari_cannot_read(compiled):
    """Safari before 16.4 (iOS 15 and early iOS 16) throws on a pattern with
    a lookbehind in it. Built when the scrubber loads, one would stop the
    whole intake page there; built per value, Remove personal details and
    the chat. So the character before a value is matched and put back
    instead (typed_value_pattern.ts), and this runs both scrubbers where
    making a lookbehind throws."""
    message = "Dear José, your annual limit"
    chat = run(compiled, noLookbehind=True, chat=[[message, ACCENTED_USER]])
    assert chat["scrubbed"] == ["Dear {{FIRST_NAME}}, your annual limit"]
    found = run(compiled, noLookbehind=True, find=[["Ann", "Ann's annual"]])
    assert found["found"] == ["[[Ann]]'s annual"]


# Remove personal details, typed into About you on the intake page.
ANN = {
    "store_fname": "Ann",
    "store_lname": "Doe",
    "email": "ann.doe@example.com",
    "store_street": "123 Sample Street Apt 4B",
    "store_zip": "62701",
}
LETTER = (
    "Acme Example Health Plan\nPO Box 0000\nAnytown, NY 00000\n\n"
    "Ann Doe\n123 Sample Street\nApt 4B\nSpringfield, IL 62701\n\n"
    "Dear Ann Doe,\n\n"
    "Your annual limit was reached. We checked the records for your "
    "inpatient stay and outpatient services, and Annette Doering's notes."
)
LETTER_REMOVED = (
    "Acme Example Health Plan\nPO Box 0000\nAnytown, NY 00000\n\n"
    "{{FIRST_NAME}} {{LAST_NAME}}\n{{ADDRESS}}\nSpringfield, IL {{ZIP_CODE}}\n\n"
    "Dear {{FIRST_NAME}} {{LAST_NAME}},\n\n"
    "Your annual limit was reached. We checked the records for your "
    "inpatient stay and outpatient services, and Annette Doering's notes."
)


@pytest.fixture(scope="class")
def compiled_for_class(request, compiled):
    request.cls.compiled = compiled


@needs_node
@pytest.mark.usefixtures("compiled_for_class")
class RemovePersonalDetailsTest(TestCase):
    compiled: pathlib.Path

    def setUp(self):
        html = self.client.get(reverse("scan")).content.decode()
        soup = BeautifulSoup(html, "html.parser")
        # Every input on the page, with the value the page gives it: a tick
        # box keeps its value attribute whether or not it is ticked.
        self.inputs = [
            {
                "id": tag.get("id") or "",
                "type": tag.get("type") or "text",
                "value": tag.get("value") or "",
            }
            for tag in soup.find_all("input")
        ]

    def remove(self, *cases, **spec) -> list:
        result = run(
            self.compiled,
            remove={"inputs": self.inputs, "cases": [list(case) for case in cases]},
            **spec,
        )
        return result["letters"]

    def test_the_page_has_the_boxes_these_cases_type_into(self):
        """And a tick box whose id the scrubber reads (store_raw_email), with
        a value that is a word a letter can hold."""
        by_id = {field["id"]: field for field in self.inputs}
        for field in ANN:
            with self.subTest(field=field):
                self.assertIn(by_id[field]["type"], ("text", "email"))
        self.assertEqual(by_id["store_raw_email"]["type"], "checkbox")
        self.assertEqual(by_id["store_raw_email"]["value"], "checked")

    def test_what_was_typed_comes_out_and_the_words_around_it_stay(self):
        """The street across two lines, the name in the address block and
        the greeting; "annual", "Annette", "Doering's", "checked",
        "inpatient" and "outpatient" left as they were."""
        self.assertEqual(self.remove((ANN, LETTER)), [LETTER_REMOVED])

    def test_short_names_leave_the_words_they_sit_inside(self):
        cases = {
            "The claim was denied, Ed.": (
                {"store_fname": "Ed"},
                "The claim was denied, {{FIRST_NAME}}.",
            ),
            "Not medically necessary for Al.": (
                {"store_fname": "Al"},
                "Not medically necessary for {{FIRST_NAME}}.",
            ),
            "The same applies to Sam.": (
                {"store_fname": "Sam"},
                "The same applies to {{FIRST_NAME}}.",
            ),
            # The live report: one typed letter took the "m" out of Example.
            "Example Health Plan": ({"store_fname": "M"}, "Example Health Plan"),
        }
        letters = self.remove(
            *((typed, letter) for letter, (typed, _) in cases.items())
        )
        for (letter, (_, expected)), removed in zip(
            cases.items(), letters, strict=True
        ):
            with self.subTest(letter=letter):
                self.assertEqual(removed, expected)

    def test_it_loads_and_runs_where_a_lookbehind_throws(self):
        """The label rules are made when the scrubber loads, and the intake
        script loads with it (see
        test_nothing_needs_a_lookbehind_that_older_safari_cannot_read)."""
        self.assertEqual(
            self.remove((ANN, LETTER), noLookbehind=True), [LETTER_REMOVED]
        )

    def test_an_accented_name_after_dear_is_taken_whole(self):
        """The greeting rule read "Dear José" as "Dear Jos" and left the
        "é" behind it."""
        self.assertEqual(
            self.remove(({}, "Dear José Núñez,\nYour request")),
            ["Dear {{FIRST_NAME}} {{LAST_NAME}},\nYour request"],
        )

    def test_a_label_inside_a_word_is_not_a_label(self):
        """ "patient" inside "inpatient" took the next word as the person's
        name and wrote a Patient label into the middle of the sentence."""
        letter = "Your inpatient admission and outpatient visits; a subgroup: none."
        self.assertEqual(self.remove(({}, letter)), [letter])

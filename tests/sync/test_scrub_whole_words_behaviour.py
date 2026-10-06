"""What the person typed is taken out of a letter by whole words, run
rather than read.

Remove personal details on the intake page (scrub_scrub.ts) and the chat's
scrubPersonalInfo (user_info_storage.ts) look for what the person typed
through typed_value_pattern.ts. It matched anywhere, so a short first name
was taken out of the middle of other words: in a live test one turned
"Example Health Plan" into "Exa {{FIRST_NAME}}ple Health Plan", and an "Ed",
"Al", "Ann" or "Sam" would turn "denied", "medically", "annual" and "same"
into placeholders before the letter was drafted from.

The rule now: a word character is a letter of a script with capitals, a
digit, or a mark on one of those, and there is a word edge between two
characters unless both are word characters. A value of one word is taken
out only where it stands whole. A value of more words is taken out wherever
matching anywhere found it, and where its first word runs on to the left or
its last word to the right, the whole printed word comes out with it ("283
24th Street" for a typed "283 24th St"). Letters of scripts without capitals
are not word characters, so a value in them is found anywhere, as before.
Only the site's own placeholders are passed over. That nothing matching
anywhere took out is left, apart from a one-word value inside a longer
word, is checked against main itself in
test_scrub_never_leaves_what_main_removed.py.

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


def compile_typescript(
    out: pathlib.Path, sources: list[pathlib.Path], cwd: pathlib.Path
) -> None:
    """The sources compiled into out the way the bundle is built (the same
    flags as test_entity_fetcher_behaviour.py), failing the test where tsc
    reports an error or leaves a file out."""
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
            *(str(path) for path in sources),
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )
    for source in sources:
        name = source.with_suffix(".js").name
        if not (out / name).exists():
            pytest.fail(
                f"tsc did not emit {name}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """typed_value_pattern.ts and the two scrubbers, compiled the way the
    bundle is built, with shared.ts, which scrub_scrub.ts imports."""
    out = tmp_path_factory.mktemp("scrub-whole-words")
    compile_typescript(out, [MODULE, *ALSO_COMPILED], cwd=JS)
    if not (out / "shared.js").exists():
        pytest.fail("tsc did not emit shared.js")
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
def test_an_apostrophe_is_not_part_of_a_word(compiled):
    """Matching anywhere took "Brien" out of "O'Brien" and "Don" out of
    "don't", and an apostrophe is not a letter or a digit, so whole words
    take them out too. The apostrophe itself is matched as typed, as main
    matched it."""
    assert_found(
        compiled,
        {
            ("Don", "We don't cover it. Don't wait, Don."): (
                "We [[don]]'t cover it. [[Don]]'t wait, [[Don]]."
            ),
            ("Brien", "O'Brien and O’Brien, not Brien"): (
                "O'[[Brien]] and O’[[Brien]], not [[Brien]]"
            ),
            ("O'Brien", "Dear O'Brien, O'BRIEN's file"): (
                "Dear [[O'Brien]], [[O'BRIEN]]'s file"
            ),
            ("Doe", "DOE'S appeal"): "[[DOE]]'S appeal",
        },
    )


@needs_node
def test_a_name_of_several_words_is_found_across_line_breaks(compiled):
    assert_found(
        compiled,
        {
            ("Ann Doe", "Dear Ann\nDoe,\nAnn  Doe\tor Ann\u00a0Doe."): (
                "Dear [[Ann\nDoe]],\n[[Ann  Doe]]\tor [[Ann\u00a0Doe]]."
            ),
        },
    )


@needs_node
def test_a_value_of_several_words_takes_the_words_it_runs_on_into(compiled):
    """Matching anywhere took a typed "283 24th St" (the intake page's own
    hint) out of "283 24th Street" and left "reet"; leaving the whole street
    because it does not end where the typed one does would leave what main
    took out. So the first word may run on to the left and the last to the
    right, and the whole printed word comes out. The words inside are whole
    already, and the first word running on to the right ("Annette Doe") is
    not the typed value, there or on main."""
    assert_found(
        compiled,
        {
            ("283 24th St", "Ann Doe\n283 24th Street\nSan Francisco, CA 94103"): (
                "Ann Doe\n[[283 24th Street]]\nSan Francisco, CA 94103"
            ),
            ("Ann Doe", "Joann Doe, Ann Doering, Annette Doe, Ann Doe."): (
                "[[Joann Doe]], [[Ann Doering]], Annette Doe, [[Ann Doe]]."
            ),
            ("Mary Ann", "Rosemary Ann and Mary Annette"): (
                "[[Rosemary Ann]] and [[Mary Annette]]"
            ),
            ("Smith-Jones", "Smith-Joneses, Smith-Jones's"): (
                "[[Smith-Joneses]], [[Smith-Jones]]'s"
            ),
            # Only to the end of the word: the line break, the comma and
            # what follows stay.
            ("283 24th St", "283 24th Street,\nApt 2"): "[[283 24th Street]],\nApt 2",
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
            # A mark after the value sits on its last letter: "José" written
            # with a combining accent is not a typed "Jose".
            ("Jose", "José and Jose."): "José and [[Jose]].",
        },
    )


@needs_node
def test_names_in_other_scripts_with_capitals_are_whole_words_too(compiled):
    assert_found(
        compiled,
        {
            ("Мария", "Уважаемая Мария, Марияна"): "Уважаемая [[Мария]], Марияна",
            ("Ελένη", "Dear Ελένη, Ελένης"): "Dear [[Ελένη]], Ελένης",
            ("김민준", "Dear 김민준."): "Dear [[김민준]].",
        },
    )


@needs_node
def test_a_name_in_a_script_without_capitals_is_found_in_running_text(compiled):
    """Chinese, Japanese and Thai put no space between words, and Korean,
    Arabic and Hebrew join particles and prefixes to a name. Each letter of
    these scripts is a word to itself, so the name is found as matching
    anywhere found it, 王 in 王国 too."""
    assert_found(
        compiled,
        {
            ("王小明", "患者王小明的申请被拒绝"): "患者[[王小明]]的申请被拒绝",
            ("王伟", "Patient: 王伟, 王伟明"): "Patient: [[王伟]], [[王伟]]明",
            (
                "김민수",
                "김민수님께, 김민수의 청구",
            ): "[[김민수]]님께, [[김민수]]의 청구",
            ("田中", "田中太郎様、田中さん"): "[[田中]]太郎様、[[田中]]さん",
            ("佐々木", "佐々木様"): "[[佐々木]]様",
            ("สมชาย", "เรียนคุณสมชาย"): "เรียนคุณ[[สมชาย]]",
            ("محمد", "إلى ومحمد"): "إلى و[[محمد]]",
            ("דוד", "שלום לדוד"): "שלום ל[[דוד]]",
            ("علي", "السلام عليكم، وعلي"): "السلام [[علي]]كم، و[[علي]]",
            # A digit before the house number runs on, and the whole printed
            # number comes out.
            ("123 王府井大街", "1123 王府井大街, 123 王府井大街"): (
                "[[1123 王府井大街]], [[123 王府井大街]]"
            ),
        },
    )


@needs_node
def test_a_change_of_script_is_the_edge_of_a_word(compiled):
    """A Latin name or an email in Chinese text has a Chinese letter right
    against it. Matching anywhere took it out, and the first version of
    whole words left it in."""
    assert_found(
        compiled,
        {
            ("Ann Doe", "患者Ann Doe的申请"): "患者[[Ann Doe]]的申请",
            ("ann@example.com", "我的电子邮箱是ann@example.com。"): (
                "我的电子邮箱是[[ann@example.com]]。"
            ),
            ("62701", "邮编62701号"): "邮编[[62701]]号",
            ("Ann", "Ann王 王Ann"): "[[Ann]]王 王[[Ann]]",
        },
    )


@needs_node
def test_a_lone_initial_is_found_as_a_whole_word(compiled):
    """Matching anywhere took a typed initial out of every word. Whole words
    take it out only where it stands alone, "A Smith" and "a plan" alike: it
    stands as a word there, so leaving it would leave in what matching
    anywhere took out. A typed "A." is looked for as "A.", with its period,
    as main looked for it: left off, it would match every "a" in the letter.
    A value with no letter or digit is not looked for."""
    assert_found(
        compiled,
        {
            ("A", "a plan, A Smith"): "[[a]] plan, [[A]] Smith",
            ("J.", "J. Doe"): "[[J.]] Doe",
            ("A.", "This is a denial of a claim... Part A. You have a right"): (
                "This is a denial of a claim... Part [[A.]] You have a right"
            ),
            ("A.", "Mesa. A."): "Mesa. [[A.]]",
            ("B", "Medicare Part B"): "Medicare Part [[B]]",
            ("M", "Example Health Plan"): "Example Health Plan",
            ("-", "a - b"): None,
            ("", "text"): None,
            ("   ", "text"): None,
            ("王", "Patient: 王, Wei. 王国"): "Patient: [[王]], Wei. [[王]]国",
        },
    )


@needs_node
def test_addresses_and_numbers(compiled):
    assert_found(
        compiled,
        {
            ("123 Sample Street Apt 4B", "123 Sample Street\nApt 4B\n"): (
                "[[123 Sample Street\nApt 4B]]\n"
            ),
            ("123 Sample Street", "1123 Sample Street; 123 Sample Streets"): (
                "[[1123 Sample Street]]; [[123 Sample Streets]]"
            ),
            # A comma typed on its own is a word of the street like any other.
            ("123 Main St , Apt 4B", "123 Main St , Apt 4B"): (
                "[[123 Main St , Apt 4B]]"
            ),
            ("4B", "Apt 4B, not 14B or 4BX"): "Apt [[4B]], not 14B or 4BX",
            ("#4B", "Unit #4B, Unit 4B"): "Unit [[#4B]], Unit 4B",
            # Punctuation at the ends of what was typed is matched as typed.
            ("123 Sample St.", "123 Sample St, Springfield; 123 Sample St. 2"): (
                "123 Sample St, Springfield; [[123 Sample St.]] 2"
            ),
            ("62701", "IL 62701-0000, claim 627012, 162701"): (
                "IL [[62701]]-0000, claim 627012, 162701"
            ),
            ("XYZ000000", "Member ID: XYZ000000. Ref XYZ0000001"): (
                "Member ID: [[XYZ000000]]. Ref XYZ0000001"
            ),
        },
    )


@needs_node
def test_a_zip_plus_four_is_found_however_it_is_spaced(compiled):
    assert_found(
        compiled,
        {
            ("62701 1234", "62701  1234; 62701\t1234; 62701 1234"): (
                "[[62701  1234]]; [[62701\t1234]]; [[62701 1234]]"
            ),
            ("62701-1234", "IL 62701-1234, IL 62701"): "IL [[62701-1234]], IL 62701",
        },
    )


@needs_node
def test_an_email_comes_out_with_the_words_it_runs_on_into(compiled):
    """An email is a value of several words. Matching anywhere took it out of
    "jann.doe@example.com" and "ann@example.comx" and left the "j" and the
    "x", so the whole printed word at either end comes out with it. A period
    is not a letter: "first." and ".au" stay, as on main."""
    assert_found(
        compiled,
        {
            ("ann.doe@example.com", "jann.doe@example.com or ann.doe@example.com."): (
                "[[jann.doe@example.com]] or [[ann.doe@example.com]]."
            ),
            ("ann@example.com", "first.ann@example.com, ann@example.com.au"): (
                "first.[[ann@example.com]], [[ann@example.com]].au"
            ),
            ("ann@example.com", "ann@example.comx"): "[[ann@example.comx]]",
        },
    )


@needs_node
def test_a_placeholder_of_the_site_is_never_written_into(compiled):
    """Only the site's own placeholders are passed over: any other text in
    double braces is text like the rest, and a name in it comes out."""
    assert_found(
        compiled,
        {
            ("Name", "{{PATIENT_NAME}} and {{LAST_NAME}}, Name."): (
                "{{PATIENT_NAME}} and {{LAST_NAME}}, [[Name]]."
            ),
            ("Your Email", "Write to {{Your Email Address}}."): (
                "Write to {{Your Email Address}}."
            ),
            ("Phone Number", "Call {{Your Phone Number}}."): (
                "Call {{Your Phone Number}}."
            ),
            ("Name", "Sincerely, {{Your Name}}"): "Sincerely, {{Your Name}}",
            ("Ann", "{{FIRST_NAME}}Ann"): "{{FIRST_NAME}}[[Ann]]",
            ("Ann Doe", "Ref {{Ann Doe}}"): "Ref {{[[Ann Doe]]}}",
            ("Ann", "{{ann}} {{Dear Ann}}"): "{{[[ann]]}} {{Dear [[Ann]]}}",
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
    # An email runs on into a longer one: the whole printed one comes out.
    (
        "Mesalamine was denied. Write to joann@example.com or ann@example.com.",
        CHAT_USER,
        "Mesalamine was denied. Write to {{Your Email Address}} or "
        "{{Your Email Address}}.",
    ),
    # So does a street into a longer house number. The ZIP code, one word,
    # stays inside a longer number.
    (
        "1123 Sample Street Apt 4B, claim 852011",
        CHAT_USER,
        "{{ADDRESS}}, claim 852011",
    ),
    (
        "Ann Doe\n283 24th Street\nSan Francisco, CA 94103",
        dict(CHAT_USER, address="283 24th St", city="San Francisco", zipCode="94103"),
        "{{PATIENT_NAME}}\n{{ADDRESS}}\n{{CITY}}, CA {{ZIP_CODE}}",
    ),
    # An initial is looked for with its period.
    (
        "This is a denial of a claim... Part A. You have a right",
        dict(CHAT_USER, firstName="A."),
        "This is a denial of a claim... Part {{FIRST_NAME}} You have a right",
    ),
    # Text in double braces that is not the site's is scrubbed like any.
    (
        "Ref {{Ann Doe}}",
        CHAT_USER,
        "Ref {{{{PATIENT_NAME}}}}",
    ),
    # Arabic joins prefixes and pronouns to a name, so a name in it is found
    # anywhere, as Remove personal details found it on main. Main's chat
    # (\b knew only A to Z) never found it at all, even standing alone.
    (
        "أنا علي. السلام عليكم، وعليه",
        dict(CHAT_USER, firstName="علي"),
        "أنا {{FIRST_NAME}}. السلام {{FIRST_NAME}}كم، و{{FIRST_NAME}}ه",
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
    (
        "患者王小明的申请",
        dict(CHAT_USER, firstName="小明", lastName="王"),
        "患者{{LAST_NAME}}{{FIRST_NAME}}的申请",
    ),
    (
        "我的电子邮箱是ann@example.com。",
        CHAT_USER,
        "我的电子邮箱是{{Your Email Address}}。",
    ),
    (
        "62701  1234 and 62701\t1234",
        dict(CHAT_USER, zipCode="62701 1234"),
        "{{ZIP_CODE}} and {{ZIP_CODE}}",
    ),
    # A placeholder already there is passed over whole, the one the full name
    # just became too.
    (
        "Joe Name applied. {{PATIENT_NAME}}",
        dict(CHAT_USER, firstName="Joe", lastName="Name"),
        "{{PATIENT_NAME}} applied. {{PATIENT_NAME}}",
    ),
    # "Same" is not the first name "Sam" written out longer.
    (
        "Same day services require prior authorization.",
        dict(CHAT_USER, firstName="Sam", lastName="Day"),
        "Same {{LAST_NAME}} services require prior authorization.",
    ),
    # Every value is looked for before any is taken out, so a first name
    # inside the street or the email does not break them up.
    (
        "I live at 77 Ann St. Write to ann.doe@example.com",
        dict(CHAT_USER, address="77 Ann St", email="ann.doe@example.com"),
        "I live at {{ADDRESS}}. Write to {{Your Email Address}}",
    ),
    # Where two overlap, all of both comes out.
    (
        "10 Main St Louis",
        dict(CHAT_USER, address="10 Main St", city="St Louis"),
        "{{ADDRESS}} {{CITY}}",
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
    the chat. So the edges are tested in code instead (typed_value_pattern.ts),
    and this runs both scrubbers where making a lookbehind throws."""
    message = "Dear José, your annual limit"
    chat = run(compiled, noLookbehind=True, chat=[[message, ACCENTED_USER]])
    assert chat["scrubbed"] == ["Dear {{FIRST_NAME}}, your annual limit"]
    found = run(compiled, noLookbehind=True, find=[["Ann", "Ann's annual"]])
    assert found["found"] == ["[[Ann]]'s annual"]


# What the intake page loads to take details out of a letter.
SHIPPED = (
    "typed_value_pattern.js",
    "scrub_scrub.js",
    "user_info_storage.js",
    "shared.js",
)


@needs_node
def test_no_shipped_scrubber_has_a_lookbehind_in_it(compiled):
    """The test above catches a lookbehind made with new RegExp. A regular
    expression literal is not made that way, so the compiled files are read
    too: every literal, and every string that could become a pattern."""
    result = run(
        compiled,
        lookbehinds={
            "typescript": str(TSC.parents[1]),
            "files": [str(compiled.parent / name) for name in SHIPPED],
        },
    )
    assert result["found"] == []


@needs_node
def test_the_lookbehind_check_finds_one_in_a_literal(compiled):
    """So the test above passing is the files having none, not the check
    missing them. A comment that mentions one is not code."""
    result = run(
        compiled,
        lookbehinds={
            "typescript": str(TSC.parents[1]),
            "sources": {
                "literal.js": "var a = /(?<=x)y/g; // (?<!z)\nvar b = 1;",
                "string.js": 'var c = new RegExp("(?<!x)y", "u");',
            },
        },
    )
    assert result["found"] == [
        ["literal.js", "/(?<=x)y/g"],
        ["string.js", '"(?<!x)y"'],
    ]


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
    "review, and Annette Doering's notes. Write to ann.doe@example.com."
)
LETTER_REMOVED = (
    "Acme Example Health Plan\nPO Box 0000\nAnytown, NY 00000\n\n"
    "{{FIRST_NAME}} {{LAST_NAME}}\n{{ADDRESS}}\nSpringfield, IL {{ZIP_CODE}}\n\n"
    "Dear {{FIRST_NAME}} {{LAST_NAME}},\n\n"
    "Your annual limit was reached. We checked the records for your "
    "review, and Annette Doering's notes. Write to {{Your Email Address}}."
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

    def assert_removed(self, cases: list) -> None:
        """Each (typed, letter, letter after Remove personal details)."""
        letters = self.remove(*((typed, letter) for typed, letter, _ in cases))
        for (_, letter, expected), removed in zip(cases, letters, strict=True):
            with self.subTest(letter=letter):
                self.assertEqual(removed, expected)

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
        the greeting, the email; "annual", "Annette", "Doering's" and
        "checked" left as they were."""
        self.assertEqual(self.remove((ANN, LETTER)), [LETTER_REMOVED])

    def test_short_names_leave_the_words_they_sit_inside(self):
        self.assert_removed(
            [
                (
                    {"store_fname": "Ed"},
                    "The claim was denied, Ed.",
                    "The claim was denied, {{FIRST_NAME}}.",
                ),
                (
                    {"store_fname": "Al"},
                    "Not medically necessary for Al.",
                    "Not medically necessary for {{FIRST_NAME}}.",
                ),
                (
                    {"store_fname": "Sam"},
                    "The same applies to Sam.",
                    "The same applies to {{FIRST_NAME}}.",
                ),
                # The live report: one typed letter took the "m" out of
                # Example.
                ({"store_fname": "M"}, "Example Health Plan", "Example Health Plan"),
            ]
        )

    def test_it_loads_and_runs_where_a_lookbehind_throws(self):
        """The label rules are made when the scrubber loads, and the intake
        script loads with it (see
        test_nothing_needs_a_lookbehind_that_older_safari_cannot_read)."""
        self.assertEqual(
            self.remove((ANN, LETTER), noLookbehind=True), [LETTER_REMOVED]
        )

    def test_a_name_in_running_text_without_spaces_comes_out(self):
        """Chinese and Japanese put no space between words, and Korean joins
        a particle or an honorific to the name. The last and first name run
        together are also what the page looks for, as one."""
        self.assert_removed(
            [
                (
                    {"store_fname": "小明", "store_lname": "王"},
                    "患者王小明的申请被拒绝",
                    "患者{{LAST_NAME}} {{FIRST_NAME}}的申请被拒绝",
                ),
                (
                    {"store_fname": "민수", "store_lname": "김"},
                    "김민수님께, 김민수의 청구",
                    "{{LAST_NAME}} {{FIRST_NAME}}님께, {{LAST_NAME}} {{FIRST_NAME}}의 청구",
                ),
                (
                    {"store_fname": "太郎", "store_lname": "田中"},
                    "田中太郎様、田中さん",
                    "{{LAST_NAME}} {{FIRST_NAME}}様、{{LAST_NAME}}さん",
                ),
            ]
        )

    def test_the_email_typed_comes_out_whole(self):
        """The Email box was never read, and only the name cut out of the
        middle of it broke it up. A name inside the typed email comes out
        with it."""
        self.assert_removed(
            [
                (
                    {
                        "store_fname": "Ann",
                        "store_lname": "Smith",
                        "email": "asmith@example.com",
                    },
                    "Write to asmith@example.com or ann.smith@example.org.",
                    "Write to {{Your Email Address}} or "
                    "{{FIRST_NAME}}.{{LAST_NAME}}@example.org.",
                ),
                (
                    {
                        "store_fname": "Ann",
                        "store_lname": "Doe",
                        "email": "ann.doe@example.com",
                    },
                    "Write to ann.doe@example.com.",
                    "Write to {{Your Email Address}}.",
                ),
            ]
        )

    def test_what_the_review_found_left_in_comes_out(self):
        """Each of these left a name, an email, a street or a ZIP code in
        the letter that matching anywhere took out. The greeting rule reads
        what main's did ("Dear Jos"), so the "é" it leaves stays, as on main;
        the last name after it comes out."""
        self.assert_removed(
            [
                (
                    {"store_fname": "Ann", "store_lname": "Doe"},
                    "患者Ann Doe的申请",
                    "患者{{FIRST_NAME}} {{LAST_NAME}}的申请",
                ),
                (
                    {"email": "ann@example.com"},
                    "我的电子邮箱是ann@example.com。",
                    "我的电子邮箱是{{Your Email Address}}。",
                ),
                (
                    {"store_street": "123 Main St , Apt 4B"},
                    "123 Main St , Apt 4B",
                    "{{ADDRESS}}",
                ),
                (
                    {"store_fname": "José", "store_lname": "O'Neill"},
                    "Dear José O'Neill",
                    "Dear {{FIRST_NAME}} {{LAST_NAME}}é {{LAST_NAME}}",
                ),
                (
                    {"store_fname": "José", "store_lname": "Smith-Jones"},
                    "Dear José Smith-Jones",
                    "Dear {{FIRST_NAME}} {{LAST_NAME}}é {{LAST_NAME}}",
                ),
                (
                    {"store_zip": "62701 1234"},
                    "IL 62701  1234, IL 62701\t1234, IL 62701 1234",
                    "IL {{ZIP_CODE}}, IL {{ZIP_CODE}}, IL {{ZIP_CODE}}",
                ),
                (
                    {"store_fname": "Chris", "store_lname": "Doe"},
                    "CHRISTOPHER DOE'S appeal",
                    "CHRISTOPHER {{LAST_NAME}}'S appeal",
                ),
                (
                    {"store_fname": "A", "store_lname": "Smith"},
                    "A Smith",
                    "{{FIRST_NAME}} {{LAST_NAME}}",
                ),
                (
                    {"store_fname": "Joe", "store_lname": "Name"},
                    "Joe Name applied. {{PATIENT_NAME}}",
                    "{{FIRST_NAME}} {{LAST_NAME}} applied. {{PATIENT_NAME}}",
                ),
            ]
        )

    def test_what_the_second_review_found_comes_out(self):
        """The street the page's own hint shows, printed longer, came out on
        main but for "reet" and was left whole; a typed "A." took every
        standalone "a" out; a name in double braces was left."""
        self.assert_removed(
            [
                (
                    {
                        "store_fname": "Ann",
                        "store_lname": "Doe",
                        "store_street": "283 24th St",
                        "store_zip": "94103",
                    },
                    "Ann Doe\n283 24th Street\nSan Francisco, CA 94103",
                    "{{FIRST_NAME}} {{LAST_NAME}}\n{{ADDRESS}}\n"
                    "San Francisco, CA {{ZIP_CODE}}",
                ),
                (
                    {"store_fname": "A."},
                    "This is a denial of a claim... Part A. You have a right",
                    "This is a denial of a claim... Part {{FIRST_NAME}} You have a right",
                ),
                (
                    {"store_fname": "Ann", "store_lname": "Doe"},
                    "Ref {{Ann Doe}}",
                    "Ref {{{{FIRST_NAME}} {{LAST_NAME}}}}",
                ),
            ]
        )

    def test_the_text_after_a_patient_label_stays(self):
        """The label rules are main's, which take one word made of A to Z
        after "Patient:", so Chinese running on after it stays."""
        self.assert_removed(
            [
                (
                    {"store_fname": "小明", "store_lname": "王"},
                    "Patient: 王小明患有2型糖尿病。",
                    "Patient: {{LAST_NAME}} {{FIRST_NAME}}患有2型糖尿病。",
                ),
            ]
        )

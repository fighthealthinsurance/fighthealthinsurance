"""Blanks left in a finished letter are caught before it leaves the site.

A letter drafted by a model comes with blanks for the person's details:
``[Your Name]``, ``{{SCSID}}``, ``XXX``, a line to sign on. One pattern list,
``fighthealthinsurance/letter_placeholders.json``, decides what counts. The fax
form checks it on the server; the print and fax buttons check it in the
browser through ``static/js/letter_placeholders.ts``.

These hold the list to two promises. It catches the shapes a model writes,
and it leaves alone the brackets and numbers a real appeal is full of
(citations, statute subsections, amounts, a karyotype), because a match stops
a fax. And the browser finds exactly what the server finds: the TypeScript is
compiled with the repo's own ``tsc`` and run in node over the same letters.

The notice itself runs over ``tests/js/fake_page.cjs``, through the regulator
letter review page's own script, so the Print button is the real wiring.
Skipped, not silently passed, where node or the front-end toolchain is not
installed: ``node_modules`` is gitignored.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

from fighthealthinsurance.letter_placeholders import (
    PATTERNS_FILE,
    describe_placeholders,
    find_unfilled_placeholders,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
JS = APP / "static" / "js"
TSC = JS / "node_modules" / "typescript" / "bin" / "tsc"
DRIVER = REPO_ROOT / "tests" / "js" / "letter_placeholders_behaviour.cjs"
TEMPLATES = APP / "templates"

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(
    NODE is None or not TSC.exists(),
    reason=(
        "needs node and the front-end toolchain: "
        "npm install in fighthealthinsurance/static/js"
    ),
)

SPEC = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))

# (what it is, a letter with it in, what is reported)
CAUGHT = [
    (
        "double braces",
        "Sincerely,\n{{FIRST_NAME}} {{LAST_NAME}}",
        ["{{FIRST_NAME}}", "{{LAST_NAME}}"],
    ),
    ("a bracketed name", "My name is [Your Name].", ["[Your Name]"]),
    ("a bracketed name in capitals", "Date: [INSERT DATE]", ["[INSERT DATE]"]),
    (
        "bracketed snake_case names",
        "my claim [CLAIM_NUMBER] with [insurance_company]",
        ["[CLAIM_NUMBER]", "[insurance_company]"],
    ),
    (
        "a bracketed instruction in lower case",
        "Denied on [insert date of denial, e.g. 01/02/2026].",
        ["[insert date of denial, e.g. 01/02/2026]"],
    ),
    (
        "bracketed address lines",
        "[Address Line 1]\n[City, State ZIP]",
        ["[Address Line 1]", "[City, State ZIP]"],
    ),
    (
        "a curly apostrophe in a bracketed name",
        "Re: [Patient’s Name]",
        ["[Patient’s Name]"],
    ),
    (
        "single braces",
        "for {Patient Name} and {diagnosis}",
        ["{Patient Name}", "{diagnosis}"],
    ),
    (
        "angle brackets",
        "Sent <Insert date> by <Patient Name>",
        ["<Insert date>", "<Patient Name>"],
    ),
    (
        "a parenthesised instruction",
        "Claim (Insert claim number) was denied.",
        ["(Insert claim number)"],
    ),
    ("INSERT in capitals", "Dated INSERT DATE HERE.", ["INSERT DATE HERE"]),
    ("a run of Xs", "Member ID XXXXXXX", ["XXXXXXX"]),
    (
        "numbers in groups of Xs",
        "SSN XXX-XX-XXXX, born XX/XX/XXXX",
        ["XXX-XX-XXXX", "XX/XX/XXXX"],
    ),
    ("an amount in Xs", "a bill of $X,XXX.XX", ["$X,XXX.XX"]),
    ("a date written as its format", "Date of service: MM/DD/YYYY", ["MM/DD/YYYY"]),
    ("a line to sign on", "Signature: ________________", ["___"]),
    (
        "dollar-sign template variables",
        "denied by $insurance_company on $DATE",
        ["$insurance_company", "$DATE"],
    ),
    (
        "the letter builder's stand-ins",
        "FirstName LastName, ID subscriber_id",
        ["FirstName", "LastName", "subscriber_id"],
    ),
]

# (what it is, a letter with it in): nothing is reported.
LEFT_ALONE = [
    ("numbered citations", "Studies [1], [2, 3] and [4-6] agree."),
    (
        "a statute with subsections",
        "42 U.S.C. § 300gg-19(a)(2) and 29 C.F.R. § 2560.503-1(h)(2)(iii)",
    ),
    ("a lettered list", "(a) the plan; (b) the denial; (i) the code."),
    ("amounts of money", "The treatment costs $500, or $1,250.00 a month."),
    ("a karyotype", "She has triple X syndrome (47,XXX); her sister is 46,XX."),
    (
        "the brackets of legal quoting",
        '"[T]he plan shall [sic] pay" [emphasis added] [internal citations omitted]',
    ),
    ("a bracketed link", "See the [CMS Guidance](https://www.cms.gov/guidance)."),
    (
        "email addresses",
        "Write to <pat.example@example.com> or pat_example@example.com.",
    ),
    ("Social Security Act titles", "Title XIX and Title XXI of the Act"),
    ("a package insert", "The package insert lists this use."),
    ("an author-year citation", "as shown [Smith et al., 2020]"),
    ("a PubMed id", "Cited as [PMID: 12345678]."),
    (
        "a complete letter",
        "Dear Example Health,\n\nI am writing to appeal the denial of claim "
        "CLM-2026-0042 for my MRI on 01/15/2026. My member ID is W123456789.\n\n"
        "Sincerely,\nPat Example",
    ),
]


def _examples(section: str) -> list[tuple[str, str]]:
    return [
        (entry.get("label") or example, example)
        for entry in SPEC[section]
        for example in entry["examples"]
    ]


@pytest.mark.parametrize(
    "text, expected", [c[1:] for c in CAUGHT], ids=[c[0] for c in CAUGHT]
)
def test_each_kind_of_blank_a_model_writes_is_caught(text, expected):
    assert find_unfilled_placeholders(text) == expected


@pytest.mark.parametrize(
    "text", [c[1] for c in LEFT_ALONE], ids=[c[0] for c in LEFT_ALONE]
)
def test_what_a_real_appeal_is_full_of_is_left_alone(text):
    assert find_unfilled_placeholders(text) == []


@pytest.mark.parametrize("shown, example", _examples("placeholders"))
def test_every_example_in_the_pattern_list_is_caught_whole(shown, example):
    assert find_unfilled_placeholders(example) == [shown]


@pytest.mark.parametrize("shown, example", _examples("ignore"))
def test_every_example_of_what_is_ignored_is_left_alone(shown, example):
    assert find_unfilled_placeholders(example) == []


def test_each_blank_is_listed_once_in_the_order_it_first_appears():
    text = "Ref XXX. I am [Your Name], member {{SCSID}}.\nSincerely,\n[Your Name]"
    assert find_unfilled_placeholders(text) == ["XXX", "[Your Name]", "{{SCSID}}"]


@pytest.mark.parametrize(
    "entry", SPEC["ignore"] + SPEC["placeholders"], ids=lambda entry: entry["name"]
)
def test_each_pattern_reads_the_same_in_python_and_javascript(entry):
    """No capturing groups (the browser reads the match's offset from the
    second argument of its replacer), no lookbehind (Safari before 16.4), no
    \\s (wider in JavaScript), and no flag but i."""
    pattern = entry["pattern"]
    assert re.compile(pattern).groups == 0
    assert "(?<" not in pattern
    assert "\\s" not in pattern
    assert set(entry.get("flags", "")) <= {"i"}


def test_a_long_list_of_blanks_is_cut_short_in_the_fax_message():
    found = [f"[Blank {letter}]" for letter in "ABCDEFGHIJKL"]
    assert describe_placeholders(found) == (", ".join(found[:10]) + " and 2 more")


# The browser side.


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """The real TypeScript, compiled the way the bundle is built.

    The flags mirror ``static/js/tsconfig.json``; ``module`` is the one that
    has to differ, because node has to ``require`` the output. The shared
    JSON sits outside ``static/js``, so the output keeps the layout from
    ``fighthealthinsurance/`` down: ``static/js/*.js`` and the JSON at the
    root.
    """
    out = tmp_path_factory.mktemp("letter-placeholders")
    result = subprocess.run(
        [
            NODE,
            str(TSC),
            "--target",
            "es5",
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--lib",
            "dom,dom.iterable,esnext",
            "--strict",
            "--esModuleInterop",
            "--allowSyntheticDefaultImports",
            "--forceConsistentCasingInFileNames",
            "--skipLibCheck",
            "--resolveJsonModule",
            "--outDir",
            str(out),
            str(JS / "letter_placeholders.ts"),
            str(JS / "escalation_packet_review.ts"),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    built = out / "static" / "js" / "escalation_packet_review.js"
    if not built.exists():
        pytest.fail(
            f"tsc did not emit escalation_packet_review.js\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def run_scenario(compiled: pathlib.Path, name: str, stdin: str = "") -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), str(compiled), name],
        cwd=str(REPO_ROOT),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ, NODE_ENV="test"),
    )
    if result.returncode != 0:
        pytest.fail(
            f"scenario {name} crashed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


@needs_node
def test_the_browser_finds_exactly_what_the_server_finds(compiled):
    letters = (
        [c[1] for c in CAUGHT]
        + [c[1] for c in LEFT_ALONE]
        + [example for _, example in _examples("placeholders")]
        + [example for _, example in _examples("ignore")]
        + ["Ref XXX. I am [Your Name], member {{SCSID}}.\nSincerely,\n[Your Name]"]
    )
    browser = run_scenario(compiled, "find", json.dumps(letters))["found"]
    assert browser == [find_unfilled_placeholders(text) for text in letters]


@needs_node
def test_print_goes_straight_to_the_print_window_when_the_letter_is_complete(
    compiled,
):
    page = run_scenario(compiled, "print-complete")
    assert (page["moved"], page["printNotice"]) == (["window.open()"], None)


@needs_node
def test_print_names_the_blanks_above_the_button_and_waits(compiled):
    page = run_scenario(compiled, "print-blanks")
    assert page["moved"] == [], "the print window opened with blanks in the letter"
    notice = page["printNotice"]
    assert (notice["items"], notice["sitsBefore"]) == (
        ["[Your Name]", "{{SCSID}}"],
        "#print_appeal",
    )


@needs_node
def test_the_print_notice_takes_the_focus(compiled):
    page = run_scenario(compiled, "print-blanks")
    notice = page["printNotice"]
    assert (page["focused"], notice["tabindex"], notice["labelledBy"]) == (
        "#print-placeholder-notice",
        "-1",
        "print-placeholder-notice-heading",
    )


@needs_node
def test_pressing_print_again_replaces_the_notice_rather_than_adding_another(
    compiled,
):
    assert run_scenario(compiled, "print-twice")["printNotices"] == 1


@needs_node
def test_print_anyway_opens_the_print_window_and_clears_the_notice(compiled):
    page = run_scenario(compiled, "print-anyway")
    assert (page["moved"], page["printNotice"], page["focused"]) == (
        ["window.open()"],
        None,
        "#print_appeal",
    )


@needs_node
def test_show_me_selects_the_first_blank_in_the_letter(compiled):
    page = run_scenario(compiled, "print-show-me")
    at = page["firstBlankAt"]
    assert (page["focused"], page["selection"]) == (
        "#id_completed_appeal_text",
        [at, at + len("[Your Name]")],
    )


@needs_node
def test_a_letter_fixed_since_the_notice_prints_and_clears_it(compiled):
    page = run_scenario(compiled, "print-fixed")
    assert (page["moved"], page["printNotice"]) == (["window.open()"], None)


@needs_node
def test_a_blank_with_markup_in_it_is_shown_as_text(compiled):
    page = run_scenario(compiled, "print-markup")
    assert (page["printNotice"]["items"], page["images"]) == (
        ["{{<img src=x onerror=alert(1)>}}"],
        0,
    )


@needs_node
def test_the_fax_waits_and_the_notice_offers_no_way_past(compiled):
    page = run_scenario(compiled, "fax-blanks")
    notice = page["faxNotice"]
    assert (page["waits"], notice["items"], notice["buttons"]) == (
        True,
        ["[Your Name]", "{{SCSID}}"],
        [{"text": "Show me in the letter", "type": "button"}],
    )


@needs_node
def test_the_notice_buttons_never_submit_the_fax_form(compiled):
    buttons = run_scenario(compiled, "fax-blanks")["faxNotice"]["buttons"]
    buttons += run_scenario(compiled, "print-blanks")["printNotice"]["buttons"]
    assert {b["type"] for b in buttons} == {"button"}


@needs_node
def test_a_fixed_letter_lets_the_fax_through_and_clears_the_notice(compiled):
    page = run_scenario(compiled, "fax-fixed")
    assert (page["first"], page["waits"], page["faxNotice"]) == (True, False, None)


def test_the_fake_page_uses_the_real_ids():
    review = (TEMPLATES / "escalation_packet_review.html").read_text()
    appeal = (TEMPLATES / "appeal.html").read_text()
    assert 'id="id_completed_appeal_text"' in review
    assert 'id="print_appeal"' in review
    assert "js/dist/escalation_packet_review.bundle.js" in review
    assert 'id="print_appeal"' in appeal
    assert 'id="fax_appeal"' in appeal

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
import yaml

from fighthealthinsurance.forms import FaxForm
from fighthealthinsurance.letter_placeholders import (
    PATTERNS_FILE,
    describe_placeholders,
    find_placeholder_spans,
    find_placeholders_as_written,
    find_unfilled_placeholders,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "fighthealthinsurance"
JS = APP / "static" / "js"
TSC = JS / "node_modules" / "typescript" / "bin" / "tsc"
DRIVER = REPO_ROOT / "tests" / "js" / "letter_placeholders_behaviour.cjs"
TEMPLATES = APP / "templates"
FIXTURES = APP / "fixtures"

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
    ("a bracketed ID, not the citation [Id.]", "Member ID: [ID]", ["[ID]"]),
    (
        "real fill-ins beside a quotation's bracketed words",
        '"[It] is not medically necessary" [emphasis added]. '
        "Seen on [Date of Service].\nSincerely,\n[Your Name]",
        ["[Date of Service]", "[Your Name]"],
    ),
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
        "bracketed details in lower case",
        "under the care of [doctor name] at [facility name] since [diagnosis date]",
        ["[doctor name]", "[facility name]", "[diagnosis date]"],
    ),
    ("a possessive in lower case", "Re: [patient's name]", ["[patient's name]"]),
    (
        "bracketed instructions to provide or quote",
        "dated [provide date] stating [quote the specific reason given in the denial letter]",
        ["[provide date]", "[quote the specific reason given in the denial letter]"],
    ),
    ("a bracketed count", "for [number of years] years", ["[number of years]"]),
    (
        "a bracketed choice of pronoun",
        "[his/her] doctor says [he or she] needs it",
        ["[his/her]", "[he or she]"],
    ),
    (
        "a bracketed choice of three pronouns",
        "[he/she/they] asked [his/her/their] doctor",
        ["[he/she/they]", "[his/her/their]"],
    ),
    (
        "a bracketed choice of pronoun in any order",
        "[they or she] told [them/him] about [themselves / herself]",
        ["[they or she]", "[them/him]", "[themselves / herself]"],
    ),
    (
        "a bracketed choice that includes neopronouns",
        "[xe/she/they] and [zir or his]",
        ["[xe/she/they]", "[zir or his]"],
    ),
    (
        "a reference link to an id the letter never defines",
        "Signed, [Your Name][1]",
        ["[Your Name]"],
    ),
    (
        "a blank written like a link definition, with no link in it",
        "[Member ID]: XXXXXXX",
        ["[Member ID]", "XXXXXXX"],
    ),
    (
        "a link definition that does not start its line",
        "Signed, [Your Name][1] [1]: https://example.com/policy",
        ["[Your Name]"],
    ),
    (
        "a long bracketed name",
        "[Brief Description of Medical History and Previous Treatments Tried and Failed]",
        [
            "[Brief Description of Medical History and Previous Treatments Tried and Failed]"
        ],
    ),
    (
        "a long bracketed instruction",
        "[insert a short paragraph on your medical history, the treatments you "
        "tried and why this one is necessary for you]",
        [
            "[insert a short paragraph on your medical history, the treatments you "
            "tried and why this one is necessary for you]"
        ],
    ),
    (
        "long blanks in braces and angle brackets",
        "{{A short paragraph about your medical history and why this treatment is "
        "necessary for you}}\n"
        "{a short paragraph about your medical history and why this treatment is "
        "necessary for you}\n"
        "<insert a short paragraph on your medical history, the treatments you "
        "tried and why this one is necessary>\n"
        "<Patient's detailed explanation of why this medication is needed for "
        "treatment>",
        [
            "{{A short paragraph about your medical history and why this treatment "
            "is necessary for you}}",
            "{a short paragraph about your medical history and why this treatment is "
            "necessary for you}",
            "<insert a short paragraph on your medical history, the treatments you "
            "tried and why this one is necessary>",
            "<Patient's detailed explanation of why this medication is needed for "
            "treatment>",
        ],
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
    (
        "ordinary words a quotation puts in brackets",
        'The plan wrote "[It] is not covered" and "[w]e will not pay"; '
        "[We] disagree, and [the] reviewer agreed with [her] doctor.",
    ),
    (
        "more notes of legal quoting",
        "[Ellipsis in original] [Brackets in original] [Alterations added] "
        "[Capitalization altered] [Cleaned up] [Emphasis ours] [Ibid] [Id.]",
    ),
    ("a bracketed link", "See the [CMS Guidance](https://www.cms.gov/guidance)."),
    (
        "bracketed links that read like blanks",
        "See [Your plan's coverage policy](https://example.com/policy), "
        "[List of covered services](https://example.com/list) and "
        "[member_handbook](https://example.com/handbook).",
    ),
    (
        "a reference link",
        "See the [Coverage Policy][1].\n\n[1]: https://example.com/policy",
    ),
    (
        "a collapsed reference link defined in other capitals",
        "See the [Coverage Policy][].\n\n[coverage  POLICY]: <https://example.com/policy>",
    ),
    (
        "reference links and definitions that read like blanks",
        "Read [Your plan's coverage policy][Policy] and [List of covered services][list].\n\n"
        '[policy]: https://example.com/policy "Coverage policy"\n'
        "   [List]:https://example.com/list\n"
        "[Member Handbook]: https://example.com/handbook",
    ),
    (
        "lower-case alterations in a quote",
        '"[t]he service [is] covered once [the plan is] updated"',
    ),
    (
        "numbers masked down to their last digits",
        "Card ending in XXXX-1234, SSN XXX-XX-1234, account XXXXXX1234",
    ),
    ("a package insert in capitals", "SEE PACKAGE INSERT FOR DOSING"),
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


def _appeal_templates() -> list[tuple[str, str]]:
    """(name, letter) for each appeal template the app loads and offers."""
    templates = []
    for fixture in ("initial.yaml", "followup.yaml"):
        rows = yaml.safe_load((FIXTURES / fixture).read_text(encoding="utf-8"))
        for row in rows:
            if row["model"] == "fighthealthinsurance.appealtemplates":
                templates.append((row["fields"]["name"], row["fields"]["appeal_text"]))
    return templates


APPEAL_TEMPLATES = _appeal_templates()


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


@pytest.mark.parametrize("letter", SPEC["reference_links"]["examples"])
def test_every_example_of_a_defined_reference_link_is_left_alone(letter):
    assert find_unfilled_placeholders(letter) == []


@pytest.mark.parametrize(
    "letter", [t[1] for t in APPEAL_TEMPLATES], ids=[t[0] for t in APPEAL_TEMPLATES]
)
def test_every_bracketed_blank_in_the_apps_own_appeal_templates_is_caught(letter):
    """The templates are offered as letters with only some blanks filled in
    on the server, so each bracket left in one is a blank to report."""
    blanks = set(re.findall(r"\[[^\[\]\n]*\]", letter))
    assert blanks
    assert blanks - set(find_unfilled_placeholders(letter)) == set()


def test_each_blank_is_listed_once_in_the_order_it_first_appears():
    text = "Ref XXX. I am [Your Name], member {{SCSID}}.\nSincerely,\n[Your Name]"
    assert find_unfilled_placeholders(text) == ["XXX", "[Your Name]", "{{SCSID}}"]


def test_each_blanks_place_is_where_the_letter_has_it_in_order():
    """Every blank, each time, at its place in the letter as written: a
    [sic] it ignores inside one is part of it there."""
    text = "Dear [Your [sic] Name], claim XXX-XX-XXXX of MM/DD/YYYY.\n[Your Name]"
    assert [
        text[start:end] for start, end in find_placeholder_spans(text)
    ] == ["[Your [sic] Name]", "XXX-XX-XXXX", "MM/DD/YYYY", "[Your Name]"]


# Two lines to write on, of different lengths, and a third as long as the
# first.
LINES = "Dated ________ by [Your Name].\nSigned: ______________\nWitness: ________"


def test_lines_to_write_on_are_named_once_as_a_line():
    assert find_unfilled_placeholders(LINES) == ["___", "[Your Name]"]


def test_lines_to_write_on_are_told_apart_by_length_as_they_are_written():
    """What a person says yes to: a line of another length is another blank."""
    assert find_placeholders_as_written(LINES) == [
        "________",
        "[Your Name]",
        "______________",
    ]


@pytest.mark.parametrize(
    "entry",
    [SPEC["reference_links"]["definition"], SPEC["reference_links"]["link"]]
    + SPEC["ignore"]
    + SPEC["placeholders"],
    ids=lambda entry: entry["name"],
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
        + SPEC["reference_links"]["examples"]
        + [letter for _, letter in APPEAL_TEMPLATES]
        + ["Ref XXX. I am [Your Name], member {{SCSID}}.\nSincerely,\n[Your Name]"]
        + [LINES]
    )
    browser = run_scenario(compiled, "find", json.dumps(letters))
    assert (browser["found"], browser["asWritten"]) == (
        [find_unfilled_placeholders(text) for text in letters],
        [find_placeholders_as_written(text) for text in letters],
    )


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
def test_show_me_skips_the_same_letters_inside_what_is_left_alone(compiled):
    page = run_scenario(compiled, "print-show-me-past-a-karyotype")
    at = page["firstBlankAt"]
    assert page["selection"] == [at, at + len("XXX")]


@needs_node
def test_show_me_selects_the_whole_line_to_write_on(compiled):
    page = run_scenario(compiled, "print-show-me-a-line")
    at = page["firstBlankAt"]
    assert page["selection"] == [at, at + page["lineLength"]]


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
def test_the_fax_waits_and_the_notice_offers_to_send_it_anyway(compiled):
    page = run_scenario(compiled, "fax-blanks")
    notice = page["faxNotice"]
    assert (page["waits"], notice["items"], notice["buttons"]) == (
        True,
        ["[Your Name]", "{{SCSID}}"],
        [
            {"text": "Show me in the letter", "type": "button"},
            {"text": "Send anyway", "type": "button"},
        ],
    )


def approved(*lists: list[str]) -> dict:
    """What the fax form posts as approved: each list posted, as the server
    reads it."""
    return {"approved_placeholders": [list(blanks) for blanks in lists]}


# "Send anyway" means "send these": the blanks its notice listed.
SENT_ANYWAY = approved(["[Your Name]", "{{SCSID}}"])


@needs_node
def test_send_anyway_sends_the_fax_with_the_blanks_it_listed(compiled):
    page = run_scenario(compiled, "fax-send-anyway")
    assert (page["submissions"], page["faxNotice"]) == (
        ["held", SENT_ANYWAY],
        None,
    )


@needs_node
def test_send_anyway_puts_the_focus_on_the_fax_button(compiled):
    page = run_scenario(compiled, "fax-send-anyway")
    assert page["focused"] == "#fax_appeal"


@needs_node
def test_after_send_anyway_the_same_letter_goes_again_in_one_press(compiled):
    page = run_scenario(compiled, "fax-send-anyway-then-again")
    assert (page["submissions"], page["faxNotice"]) == (
        ["held", SENT_ANYWAY, SENT_ANYWAY],
        None,
    )


@needs_node
def test_after_send_anyway_an_edit_that_adds_no_blank_is_faxed(compiled):
    page = run_scenario(compiled, "fax-send-anyway-then-an-edit")
    assert (page["submissions"], page["faxNotice"]) == (
        ["held", SENT_ANYWAY, SENT_ANYWAY],
        None,
    )


@needs_node
def test_after_send_anyway_a_new_blank_brings_the_notice_back(compiled):
    """The notice lists every blank the letter has, the new one first and
    marked, so "Send anyway" on it names all that goes."""
    page = run_scenario(compiled, "fax-send-anyway-then-a-new-blank")
    assert (
        page["submissions"],
        page["leftOnTheForm"],
        page["faxNotice"]["items"],
    ) == (
        ["held", SENT_ANYWAY, "held"],
        {},
        ["New: [Date of Service]", "[Your Name]", "{{SCSID}}"],
    )


@needs_node
def test_the_notice_that_came_back_says_what_new_means(compiled):
    notice = run_scenario(compiled, "fax-send-anyway-then-a-new-blank")["faxNotice"]
    assert (
        "The ones marked new weren't there when you said to send it as it is"
        in notice["text"]
    )


@needs_node
def test_after_send_anyway_an_edit_leaving_only_a_different_blank_marks_it_new(
    compiled,
):
    """None of the blanks said yes to is left, and the one there is new."""
    page = run_scenario(compiled, "fax-send-anyway-then-only-a-different-blank")
    notice = page["faxNotice"]
    assert (
        page["submissions"],
        notice["items"],
        "The ones marked new weren't there when you said to send it as it is"
        in notice["text"],
    ) == (["held", SENT_ANYWAY, "held"], ["New: [Date of Service]"], True)


@needs_node
def test_a_first_notice_marks_nothing_new(compiled):
    notice = run_scenario(compiled, "fax-blanks")["faxNotice"]
    assert ("New:" in notice["text"], "marked new" in notice["text"]) == (
        False,
        False,
    )


@needs_node
def test_show_me_on_the_notice_that_came_back_selects_the_new_blank(compiled):
    page = run_scenario(compiled, "fax-send-anyway-then-a-new-blank-show-me")
    at = page["newBlankAt"]
    assert (page["faxFocused"], page["faxSelection"]) == (
        True,
        [at, at + len("[Date of Service]")],
    )


@needs_node
def test_show_me_on_a_first_fax_notice_selects_the_first_blank(compiled):
    page = run_scenario(compiled, "fax-show-me")
    at = page["firstBlankAt"]
    assert page["faxSelection"] == [at, at + len("[Your Name]")]


@needs_node
def test_send_anyway_on_the_notice_that_came_back_sends_in_one_press(compiled):
    page = run_scenario(compiled, "fax-send-anyway-then-a-new-blank-sent-anyway")
    assert (page["submissions"], page["faxNotice"]) == (
        [
            "held",
            SENT_ANYWAY,
            "held",
            approved(["[Your Name]", "{{SCSID}}", "[Date of Service]"]),
        ],
        None,
    )


@needs_node
def test_send_anyway_covers_only_the_blanks_its_notice_listed(compiled):
    page = run_scenario(compiled, "fax-new-blank-typed-before-send-anyway")
    assert (page["submissions"], page["faxNotice"]["items"]) == (
        ["held", "held"],
        ["New: [Date of Service]", "[Your Name]", "{{SCSID}}"],
    )


@needs_node
def test_after_send_anyway_a_line_of_another_length_brings_the_notice_back(
    compiled,
):
    """Every line is listed as ___, but "Send anyway" says yes to the line
    the letter had, by its length, so a new, different line is caught, and
    Show me goes to it."""
    page = run_scenario(compiled, "fax-send-anyway-then-a-new-line")
    at = page["newLineAt"]
    assert (page["submissions"], page["faxNotice"]["items"], page["faxSelection"]) == (
        ["held", approved(["______________"]), "held"],
        ["New: ___"],
        [at, at + page["newLineLength"]],
    )


@needs_node
def test_after_send_anyway_the_same_line_goes_after_an_edit(compiled):
    page = run_scenario(compiled, "fax-send-anyway-then-the-same-line")
    assert page["submissions"] == [
        "held",
        approved(["______________"]),
        approved(["______________"]),
    ]


@needs_node
def test_a_ticked_send_it_as_it_is_box_lets_the_fax_through(compiled):
    """The box posts the list it holds, and the page posts the same blanks
    as the ones it lets go."""
    page = run_scenario(compiled, "fax-box-ticked")
    assert (page["submissions"], page["faxNotice"]) == (
        [approved(["[Your Name]", "{{SCSID}}"], ["[Your Name]", "{{SCSID}}"])],
        None,
    )


@needs_node
def test_an_unticked_send_it_as_it_is_box_still_holds_the_fax(compiled):
    page = run_scenario(compiled, "fax-box-unticked")
    assert (page["submissions"], page["faxNotice"]["items"]) == (
        ["held"],
        ["[Your Name]", "{{SCSID}}"],
    )


@needs_node
def test_a_ticked_box_without_a_list_holds_the_fax(compiled):
    page = run_scenario(compiled, "fax-box-ticked-without-a-list")
    assert (page["submissions"], page["faxNotice"]["items"]) == (
        ["held"],
        ["[Your Name]", "{{SCSID}}"],
    )


@needs_node
def test_a_ticked_box_does_not_cover_a_blank_typed_in_after(compiled):
    page = run_scenario(compiled, "fax-box-ticked-then-a-new-blank")
    at = page["newBlankAt"]
    assert (page["submissions"], page["faxNotice"]["items"], page["faxSelection"]) == (
        ["held"],
        ["New: [Date of Service]", "[Your Name]", "{{SCSID}}"],
        [at, at + len("[Date of Service]")],
    )


@needs_node
def test_a_ticked_box_then_only_a_different_blank_marks_it_new(compiled):
    page = run_scenario(compiled, "fax-box-ticked-then-only-a-different-blank")
    assert (page["submissions"], page["faxNotice"]["items"]) == (
        ["held"],
        ["New: [Date of Service]"],
    )


def test_send_anyway_uses_the_fax_forms_own_name_and_box():
    """The hidden field "Send anyway" adds, and the tick box the page reads,
    are the fax form's: the field the server checks, and the id Django gives
    it on the page that names the blanks."""
    source = (JS / "letter_placeholders.ts").read_text()
    form = FaxForm(data={"completed_appeal_text": "I am [Your Name]."})
    form.is_valid()
    box = form["approved_placeholders"]
    assert f'SEND_ANYWAY_FIELD = "{box.html_name}";' in source
    assert f'SEND_ANYWAY_BOX_ID = "{box.auto_id}";' in source


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
    assert '{% bundle "escalation_packet_review" %}' in review
    assert 'id="print_appeal"' in appeal
    assert 'id="fax_appeal"' in appeal

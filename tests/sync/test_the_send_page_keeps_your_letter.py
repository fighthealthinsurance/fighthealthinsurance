"""The send page hands the person their letter exactly as it was written.

The draft box on appeal.html sat on an indented line inside its textarea, so
every letter arrived with four spaces in front and a line break and two
spaces after, and appeal.ts copied them into the finished letter that is
printed and faxed. The page now puts the letter in the box as it is, and the
script trims what is around a letter before it builds the finished one.

The page also told people in red to save a copy RIGHT NOW and gave them no
way to do it but selecting the text by hand. It now says it in one calm
line, beside Copy and Download buttons that keep the finished letter as it
is, and a status line that says what happened and how many blanks are left.
And the fax form asked for a name the person had already given in the
details panel; it now takes it from there until they type one of their own.

The page is rendered by the real view and read the way a browser reads a
textarea. The script is the real TypeScript, compiled with the repo's own
tsc (the same flags as test_letter_placeholders.py) and loaded in node over
tests/js/fake_page.cjs through tests/js/appeal_send_page_behaviour.cjs.
Skipped, not silently passed, where node or the front-end toolchain is not
installed; the checks against the rendered page need neither.
"""

import json
import os
import pathlib
import re
import subprocess

import pytest
from bs4 import BeautifulSoup
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.models import Denial
from tests.sync.test_entity_fetcher_behaviour import NODE, TSC, needs_node

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = REPO_ROOT / "fighthealthinsurance" / "static" / "js"
DRIVER = REPO_ROOT / "tests" / "js" / "appeal_send_page_behaviour.cjs"

EMAIL = "patient@example.com"
# Made up, like every person and plan here.
LETTER = (
    "Dear Example Health,\n\n"
    "I am writing to appeal the denial of my MRI.\n\n"
    "Sincerely,\n"
    "Rebecca Lee Crumpler"
)


def textarea_value(textarea) -> str:
    """A textarea's value as a browser has it: the one line break straight
    after the opening tag is not part of it."""
    text = textarea.string or ""
    return text[1:] if text.startswith("\n") else text


class SendPageTestCase(TestCase):
    """The send page as ChooseAppeal renders it for a real case."""

    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="denied",
            semi_sekret="the-case-secret",
            hashed_email=Denial.get_hashed_email(EMAIL),
            insurance_company="Example Health",
            appeal_fax_number="15550000000",
        )

    def render(self, appeal_text: str = LETTER) -> BeautifulSoup:
        response = self.client.post(
            reverse("choose_appeal"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": self.denial.semi_sekret,
                "appeal_text": appeal_text,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeal.html")
        return BeautifulSoup(response.content.decode(), "html.parser")


class SendPageRendersTheLetterAsItIsTest(SendPageTestCase):
    def test_the_draft_box_holds_the_letter_and_nothing_around_it(self):
        draft = self.render().find("textarea", id="scrubbed_appeal_text")
        self.assertEqual(textarea_value(draft), LETTER)

    def test_markup_in_a_letter_stays_text_in_the_box(self):
        letter = "Dear Example Health, </textarea><b>not markup</b> & more."
        draft = self.render(letter).find("textarea", id="scrubbed_appeal_text")
        self.assertEqual(textarea_value(draft), letter)

    def test_the_fake_page_has_the_real_ids(self):
        """appeal_send_page_behaviour.cjs mirrors this page by id, so a
        rename cannot leave it testing a page that no longer exists."""
        page = self.render()
        fixture = DRIVER.read_text()
        ids = re.findall(r'id="([^"]+)"', fixture.split("const MARKUP = `")[1].split("`;")[0])
        own = {"share-form", "pii-panel", "fax-form"}
        missing = [i for i in ids if i not in own and page.find(id=i) is None]
        self.assertEqual(missing, [])


class SendPageKeepACopyTest(SendPageTestCase):
    """One calm line and two buttons where the red banner was."""

    def test_the_red_save_banner_is_gone(self):
        page = self.render()
        self.assertNotIn("RIGHT NOW", page.get_text())
        self.assertEqual(page.select(".alert-box-danger"), [])

    def test_one_calm_line_says_to_keep_a_copy(self):
        row = self.render().find(id="keep-a-copy")
        self.assertEqual(
            [p.get_text(strip=True) for p in row.find_all("p")],
            ["Keep a copy of your letter for your records."],
        )

    def test_copy_and_download_sit_beside_it_as_plain_buttons(self):
        row = self.render().find(id="keep-a-copy")
        buttons = {b["id"]: b for b in row.find_all("button")}
        self.assertEqual(sorted(buttons), ["copy_appeal", "download_appeal"])
        for button in buttons.values():
            self.assertEqual(button.get("type"), "button")
            self.assertIn("fhi-button-secondary", button["class"])

    def test_what_happened_is_announced(self):
        status = self.render().find(id="keep-a-copy-status")
        self.assertEqual(status.get("role"), "status")

    def test_the_buttons_come_after_the_letter_and_before_the_ways_to_send_it(self):
        html = str(self.render())
        self.assertLess(html.index('id="scrubbed_appeal_text"'), html.index('id="keep-a-copy"'))
        self.assertLess(html.index('id="keep-a-copy"'), html.index('id="fax_appeal"'))

    def test_the_page_has_no_em_dash(self):
        self.assertNotIn("\u2014", self.render().get_text())

    def test_the_fax_form_still_asks_for_a_name(self):
        """The script fills it in; the server still needs one."""
        name = self.render().find("input", id="id_name")
        self.assertIsNotNone(name.get("required"))


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> pathlib.Path:
    """The real appeal.ts and what it imports, compiled the way the bundle
    is built. The shared JSON sits outside static/js, so the output keeps the
    layout from fighthealthinsurance/ down."""
    out = tmp_path_factory.mktemp("appeal-send-page")
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
            str(JS / "appeal.ts"),
        ],
        cwd=str(JS),
        capture_output=True,
        text=True,
        timeout=300,
    )
    built = out / "static" / "js" / "appeal.js"
    if not built.exists():
        pytest.fail(
            f"tsc did not emit appeal.js\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def run_scenario(compiled: pathlib.Path, name: str, **given) -> dict:
    result = subprocess.run(
        [NODE, str(DRIVER), str(compiled), name],
        cwd=str(REPO_ROOT),
        input=json.dumps(given),
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


# What an older page put in the draft box, as the browser read it out.
INDENTED = "    " + LETTER + "\n  "


@needs_node
def test_the_finished_letter_starts_at_its_first_word(compiled):
    page = run_scenario(compiled, "load", draft=INDENTED)
    assert page["letter"] == LETTER


@needs_node
def test_the_finished_letter_stays_trimmed_as_the_draft_is_edited(compiled):
    page = run_scenario(
        compiled, "type-in-draft", draft=LETTER, typed="\n\n  " + LETTER + " Thank you.\n\n"
    )
    assert page["letter"] == LETTER + " Thank you."


@needs_node
def test_the_details_block_still_follows_a_trimmed_letter(compiled):
    page = run_scenario(
        compiled, "load", draft=INDENTED, stored={"store_fname": "Rebecca", "store_lname": "Crumpler"}
    )
    assert page["letter"] == LETTER + "\n\n---\nPatient Information:\nName: Rebecca Crumpler"


# The finished letter for a person whose details are stored, from LETTER.
STORED = {"store_fname": "Rebecca", "store_lname": "Crumpler"}
FINISHED = LETTER + "\n\n---\nPatient Information:\nName: Rebecca Crumpler"
COPIED = "Copied. Paste it somewhere safe, like an email to yourself."


@needs_node
def test_copy_puts_the_finished_letter_on_the_clipboard(compiled):
    page = run_scenario(compiled, "copy", draft=LETTER, stored=STORED)
    assert (page["clipboard"], page["status"]) == ([FINISHED], COPIED)


@needs_node
def test_copy_without_the_clipboard_api_copies_the_selected_letter(compiled):
    page = run_scenario(compiled, "copy", draft=LETTER, stored=STORED, clipboard="missing")
    assert (page["execCopies"], page["status"]) == (
        [{"command": "copy", "selected": FINISHED}],
        COPIED,
    )


@needs_node
def test_copy_outside_https_uses_the_older_way(compiled):
    page = run_scenario(compiled, "copy", draft=LETTER, stored=STORED, secure=False)
    assert (page["clipboard"], len(page["execCopies"])) == ([], 1)


@needs_node
def test_a_copy_the_browser_refuses_says_where_the_letter_is(compiled):
    page = run_scenario(
        compiled, "copy", draft=LETTER, stored=STORED, clipboard="refuses", execCopies=False
    )
    assert page["status"] == (
        "We couldn't copy it for you."
        " Select your letter in the fax form below and copy it yourself."
    )


@needs_node
def test_the_older_copy_leaves_the_person_by_the_button_and_its_message(compiled):
    """Selecting the letter in the fax form used to scroll the page down to
    it, away from the message beside the button (Codex)."""
    for clipboard, works in (("missing", True), ("refuses", False)):
        page = run_scenario(
            compiled, "copy", draft=LETTER, stored=STORED, clipboard=clipboard, execCopies=works
        )
        assert page["focused"] == "copy_appeal", clipboard


# The finished letter after the person pasted it in with space around it.
SPACED = "\n  " + FINISHED + "\n\n  "


@needs_node
def test_copy_leaves_out_the_space_around_a_letter(compiled):
    page = run_scenario(compiled, "copy", draft=LETTER, stored=STORED, letter=SPACED)
    assert page["clipboard"] == [FINISHED]


@needs_node
def test_the_older_copy_selects_the_letter_without_the_space_around_it(compiled):
    page = run_scenario(
        compiled, "copy", draft=LETTER, stored=STORED, letter=SPACED, clipboard="missing"
    )
    assert page["execCopies"] == [{"command": "copy", "selected": FINISHED}]


@needs_node
def test_download_leaves_out_the_space_around_a_letter(compiled):
    page = run_scenario(compiled, "download", draft=LETTER, stored=STORED, letter=SPACED)
    assert [f["text"] for f in page["files"]] == [FINISHED]


@needs_node
def test_print_leaves_out_the_space_around_a_letter(compiled):
    page = run_scenario(compiled, "print", draft=LETTER, stored=STORED, letter=SPACED)
    assert page["printed"] == [FINISHED]


@needs_node
def test_copy_keeps_a_letter_with_blanks_and_says_how_many_are_left(compiled):
    draft = LETTER.replace("my MRI.", "my MRI on [Date of Service].")
    page = run_scenario(compiled, "copy", draft=draft, stored=STORED)
    assert len(page["clipboard"]) == 1
    assert page["status"] == (
        COPIED + " It still has 1 blank to fill in before you send it, like [Date of Service]."
    )


@needs_node
def test_an_empty_letter_is_not_copied(compiled):
    page = run_scenario(compiled, "copy", draft=LETTER, letter="  \n ")
    assert (page["clipboard"], page["execCopies"], page["status"]) == (
        [],
        [],
        "There is no letter to copy yet.",
    )


@needs_node
def test_download_saves_the_finished_letter_as_a_text_file(compiled):
    page = run_scenario(compiled, "download", draft=LETTER, stored=STORED)
    assert page["files"] == [{"text": FINISHED, "type": "text/plain;charset=utf-8"}]
    assert (page["link"]["download"], page["moved"]) == ("appeal-letter.txt", ["a.click()"])
    assert page["status"] == "Your letter is downloading as appeal-letter.txt."


@needs_node
def test_the_download_leaves_nothing_behind(compiled):
    page = run_scenario(compiled, "download", draft=LETTER, stored=STORED)
    assert page["link"]["stillOnPage"] is False
    assert (page["revokedAtOnce"], page["revoked"]) == ([], [page["link"]["href"]])


@needs_node
def test_an_empty_letter_is_not_downloaded(compiled):
    page = run_scenario(compiled, "download", draft=LETTER, letter="")
    assert (page["files"], page["moved"], page["status"]) == (
        [],
        [],
        "There is no letter to download yet.",
    )


@needs_node
def test_the_fax_name_comes_from_the_details_the_person_gave(compiled):
    page = run_scenario(compiled, "fax-name", draft=LETTER, stored=STORED)
    assert page["seen"] == ["Rebecca Crumpler"]


@needs_node
def test_the_fax_name_follows_the_details_panel(compiled):
    page = run_scenario(
        compiled, "fax-name", draft=LETTER, stored=STORED, steps=[["pii_fname", "Rebecca Lee"]]
    )
    assert page["seen"] == ["Rebecca Crumpler", "Rebecca Lee Crumpler"]


@needs_node
def test_a_name_typed_in_the_fax_form_is_kept(compiled):
    page = run_scenario(
        compiled,
        "fax-name",
        draft=LETTER,
        stored=STORED,
        steps=[["id_name", "Ida B. Wells"], ["pii_lname", "Wells"]],
    )
    assert page["seen"] == ["Rebecca Crumpler", "Ida B. Wells", "Ida B. Wells"]


@needs_node
def test_clearing_the_fax_name_lets_the_panel_fill_it_again(compiled):
    page = run_scenario(
        compiled,
        "fax-name",
        draft=LETTER,
        stored=STORED,
        steps=[["id_name", ""], ["pii_fname", "Rebecca Lee"]],
    )
    assert page["seen"] == ["Rebecca Crumpler", "", "Rebecca Lee Crumpler"]


@needs_node
def test_a_name_the_server_sent_back_is_kept(compiled):
    page = run_scenario(
        compiled,
        "fax-name",
        draft=LETTER,
        stored=STORED,
        faxName="Ida B. Wells",
        steps=[["pii_fname", "Rebecca Lee"]],
    )
    assert page["seen"] == ["Ida B. Wells", "Ida B. Wells"]


@needs_node
def test_no_details_leaves_the_fax_name_for_the_person(compiled):
    page = run_scenario(compiled, "fax-name", draft=LETTER)
    assert page["seen"] == [""]

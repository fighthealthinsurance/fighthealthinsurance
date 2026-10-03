"""The letter the person edited is the letter that prints and faxes.

Two defects in the same file, both invisible to a happy-path walk.

The finished letter is a textarea the person can type in, and it is what the
fax form posts and what Print shows. It is also rebuilt from the draft plus
the details panel by ``descrub``, which used to run on every keystroke in the
draft, on every panel change, and once more inside the fax form's own submit
handler, one line before the text was read and sent. A correction typed into
the finished letter was therefore discarded on the way to the fax without a
word.

Print wrote that same text into a popup with ``document.write`` after turning
newlines into ``<br>``, so anything in the letter that looks like markup was
interpreted as markup in a window on this origin. A letter is text.
"""

import pathlib
import re

JS = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"


def _appeal_source() -> str:
    return (JS / "appeal.ts").read_text()


def _function(src: str, name: str) -> str:
    """The brace-matched body of one function declared at statement position."""
    occurrences = re.findall(r"\bfunction\s+" + re.escape(name) + r"\b", src)
    assert len(occurrences) == 1, f"expected exactly one `function {name}`, found {len(occurrences)}"
    head = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", src)
    assert head is not None
    open_at = src.index("{", src.index(")", head.end()))
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def test_print_puts_the_letter_in_as_text_not_as_markup():
    body = _function(_appeal_source(), "printAppeal")
    assert "letter.textContent = completedAppealText;" in body, (
        "the letter no longer reaches the print window as text"
    )
    assert 'doc.createElement("pre")' in body, "the line breaks of the letter are no longer preserved by a pre"
    assert not re.search(r"write\([^)]*completedAppealText", body), (
        "the letter is written into the print window as HTML again"
    )
    assert "<br>" not in body, "newlines are turned into markup again"
    # The only thing written as HTML is a fixed shell with no letter in it.
    for written in re.findall(r"doc\.write\(\s*([\s\S]*?)\);", body):
        assert "completedAppealText" not in written, "the shell write carries the letter"
    assert "white-space:pre-wrap" in body, "a printed letter would lose its line wrapping"


def test_a_hand_edited_letter_is_not_rebuilt_underneath_the_person():
    src = _appeal_source()
    assert re.search(
        r"function noteCompletedLetterEdit\(\): void \{\s*if \(!rebuildingCompletedLetter\) \{\s*completedLetterEdited = true;",
        src,
    ), "typing in the finished letter is no longer noticed, or a rebuild counts as typing"
    assert 'completed_text.addEventListener("input", noteCompletedLetterEdit)' in src, (
        "nothing listens for the person typing in the finished letter"
    )
    body = _function(src, "descrub")
    assert re.search(r"if \(completedLetterEdited && !force\) \{\s*return;", body), (
        "descrub no longer leaves a hand-edited letter alone"
    )
    assert "rebuildingCompletedLetter = true;" in body and "rebuildingCompletedLetter = false;" in body, (
        "descrub's own write would be counted as the person typing"
    )


def test_only_the_rebuild_button_and_the_details_panel_force_a_rebuild():
    src = _appeal_source()
    # The draft's own keystrokes must not force: as a bare handler the event
    # object arrives as `force` and every keystroke would rebuild.
    assert "appeal_text.oninput = () => descrub();" in src, (
        "the draft's input handler can force a rebuild through the event object"
    )
    assert "descrub_button.onclick = () => descrub(true);" in src, "the rebuild button no longer rebuilds"
    forced = re.findall(r"descrub\(true\)", src)
    assert len(forced) == 2, f"exactly two explicit rebuilds are expected, found {len(forced)}"


def test_the_page_does_not_rebuild_over_a_letter_the_server_sent_back():
    """A rejected fax re-renders with the submitted letter already in the box.

    Rebuilding on load would strip everything the person added after the
    details block, so the first build only fills an empty box (review).
    """
    setup = _function(_appeal_source(), "setupAppeal")
    assert 'const completedOnLoad = (completed_text as HTMLTextAreaElement | null)?.value ?? "";' in setup
    assert re.search(
        r'if \(completedOnLoad\.trim\(\) === ""\) \{\s*descrub\(\);\s*\} else \{\s*completedLetterEdited = true;',
        setup,
    ), "the page rebuilds on load over a letter that came back from the server"


def test_the_guard_does_not_lapse_when_the_letter_is_emptied():
    body = _function(_appeal_source(), "descrub")
    assert re.search(r"if \(completedLetterEdited && !force\) \{\s*return;", body), (
        "emptying or blanking the letter lets a rebuild back in"
    )
    assert "value.trim() !== \"\"" not in body, "the emptiness escape is back"


def test_a_letter_with_blanks_is_held_and_the_next_try_is_checked_afresh():
    """The check decides inside the one submission: it stops it only while
    blanks are left, never re-submits the form from inside its own event
    (requestSubmit() there does nothing), and keeps no flag that would wave
    a later press past it (review)."""
    src = _appeal_source()
    assert "skipCheck" not in src, "the skip flag is back; a later click would bypass the check"
    assert "faxForm.requestSubmit(" not in src, "the submit is re-entered from inside its own event again"
    submit = src.index('faxForm.addEventListener("submit"')
    handler = src[submit:]
    assert re.search(
        r"if \(faxMustWaitForPlaceholders\(faxButton, letter\)\) \{\s*e\.preventDefault\(\);",
        handler,
    ), "a letter with blanks left in it is no longer held back from the fax"
    # An empty letter is stopped before it becomes an empty fax.
    assert re.search(r'if \(appealText\.trim\(\) === ""\) \{\s*.*e\.preventDefault\(\);', handler, re.S)


def test_the_fax_names_the_blanks_on_the_page_not_in_a_dialog():
    assert "confirm(" not in _appeal_source(), "the fax asks about blanks in a dialog again"


def test_print_checks_the_letter_for_blanks_first():
    setup = _function(_appeal_source(), "setupAppeal")
    assert re.search(
        r"print_button\.onclick = \(\) => \{\s*printUnlessPlaceholders\(\s*print_button,\s*"
        r'document\.getElementById\("id_completed_appeal_text"\) as HTMLTextAreaElement \| null,\s*'
        r"printAppeal,\s*\);",
        setup,
    ), "Print opens the print window without checking the letter for blanks"


def test_the_appeal_page_uses_the_shared_pattern_list():
    """One list for the browser and the fax form on the server, so an amount
    like $500 or a citation like [1] is treated the same on both sides
    (tests/sync/test_letter_placeholders.py runs them side by side)."""
    src = _appeal_source()
    assert 'from "./letter_placeholders";' in src, "appeal.ts no longer uses the shared check"
    assert "checkForUnfilledPlaceholders" not in src, "appeal.ts has a pattern list of its own again"


def test_the_setup_actually_runs():
    src = _appeal_source()
    assert re.search(r"^setupAppeal\(\);", src, re.M), (
        "nothing calls setupAppeal, so none of these handlers would be installed"
    )


def test_the_fax_reads_the_letter_after_deciding_not_to_rebuild_it():
    src = _appeal_source()
    submit = src.index('faxForm.addEventListener("submit"')
    handler = src[submit : src.index("faxMustWaitForPlaceholders(faxButton, letter)", submit)]
    assert "descrub();" in handler, "the details panel is no longer applied for an untouched letter"
    assert handler.count("descrub()") == 1, "the fax handler rebuilds more than once"
    assert "descrub(true)" not in handler, (
        "the fax handler forces a rebuild, which is what discarded the person's corrections"
    )
    assert handler.index("descrub();") < handler.index("id_completed_appeal_text"), (
        "the letter is read before the rebuild decision"
    )

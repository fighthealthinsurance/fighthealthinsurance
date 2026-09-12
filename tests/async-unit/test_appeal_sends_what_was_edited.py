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
    assert re.search(
        r"if \(completedLetterEdited && !force && completed && completed\.value\.trim\(\) !== \"\"\) \{\s*return;",
        body,
    ), "descrub no longer leaves a hand-edited letter alone"
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


def test_the_fax_reads_the_letter_after_deciding_not_to_rebuild_it():
    src = _appeal_source()
    submit = src.index('faxForm.addEventListener("submit"')
    handler = src[submit : src.index("checkForUnfilledPlaceholders(appealText)", submit)]
    assert "descrub();" in handler, "the details panel is no longer applied for an untouched letter"
    assert "descrub(true)" not in handler, (
        "the fax handler forces a rebuild, which is what discarded the person's corrections"
    )
    assert handler.index("descrub();") < handler.index("id_completed_appeal_text"), (
        "the letter is read before the rebuild decision"
    )

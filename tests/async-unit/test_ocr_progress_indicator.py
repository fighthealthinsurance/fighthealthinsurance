"""The "we're reading your file" box must appear while reading, not only on submit.

Reading one photographed page takes several seconds. The box existed, but the
only thing that ever showed it was the submit gate, so a user who waited quietly
saw an idle page and no explanation. It now shows when reading starts and hides
when the latest selection finishes.

"Latest" matters. A user can pick files again while a batch is still running,
and scrub.ts drops every write from the superseded batch, so the indicator must
follow the batch whose text can actually land: a superseded batch finishing must
not hide it, and a superseded batch still running must not keep it up.

There is no JS test harness in this repo, so this asserts on the source, matching
``test_scrub_ocr_quality.py``. Structural checks brace-match; the plain
substring checks that review showed could be satisfied by a broken
implementation are gone.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"
CSS = REPO / "static" / "css"


def _form_source() -> str:
    return (JS / "scrub_client_side_form.ts").read_text()


def _scrub_source() -> str:
    return (JS / "scrub.ts").read_text()


def _brace_block(src: str, open_at: int) -> str:
    assert src[open_at] == "{", src[open_at : open_at + 20]
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def _rgb(value: str):
    """(r, g, b) for #rgb, #rrggbb, or rgb(r, g, b); None for anything else."""
    m = re.fullmatch(r"#([0-9a-f]{3})", value)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.fullmatch(r"#([0-9a-f]{6})", value)
    if m:
        h = m.group(1)
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    # Comma or space separated: rgb(44, 62, 80) and rgb(44 62 80) are the
    # same colour (review).
    m = re.fullmatch(r"rgb\(\s*(\d+)\s*[, ]\s*(\d+)\s*[, ]\s*(\d+)\s*\)", value)
    if m:
        return tuple(int(x) for x in m.groups())
    return None


def _js_function(src: str, declaration: str) -> str:
    """Body of the function declared exactly as ``declaration`` followed by ``(``."""
    return _js_function_at(src, re.escape(declaration) + r"\s*\(")


def _js_function_at(src: str, head: str) -> str:
    """Body of the function whose head matches ``head``, which must end at ``(``.

    Covers the assignment form too: ``const recognizeEvent = async function (``.
    """
    match = re.search(head, src)
    assert match is not None, f"{head} not found"
    depth = 0
    end_of_params = None
    for i in range(match.end() - 1, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end_of_params = i
                break
    assert end_of_params is not None, f"unbalanced parens reading {head}"
    return _brace_block(src, src.index("{", end_of_params))


def test_begin_ocr_records_the_selection_and_shows_the_progress_box():
    """Show it when reading starts, and record WHICH batch is being read.

    The recording is the central state transition: without it every read
    reports "not in flight", a mid-read submit hides the box, and a read that
    finishes without a submit leaves it up forever because endOcr returns
    early for a selection that was never recorded (review).
    """
    body = _js_function(_form_source(), "export function beginOcr")
    assert re.search(r"activeOcrSelection\s*=\s*selection\s*;", body), (
        "beginOcr no longer records the active selection"
    )
    assert 'showHiddenMessage("ocr_in_progress")' in body, (
        "beginOcr no longer shows the progress box; the user is back to "
        "staring at an idle page while their file is read"
    )


def test_end_ocr_hides_only_for_the_batch_that_is_still_current():
    """Hide after, but only when the batch ending is the latest one.

    The guard has to come BEFORE the hide, or a superseded batch finishing
    early would clear the indicator for the batch that replaced it.
    """
    body = _js_function(_form_source(), "export function endOcr")
    # Either operand order: `selection !== activeOcrSelection` or the reverse
    # is the same guard (review).
    guard = re.search(
        r"if\s*\(\s*(?:selection\s*!==\s*activeOcrSelection|activeOcrSelection\s*!==\s*selection)\s*\)\s*\{[^}]*\breturn\s*;",
        body,
        re.DOTALL,
    )
    assert guard is not None, (
        "endOcr no longer returns early for a superseded batch, so an old "
        "batch finishing would hide the indicator for the current one"
    )
    hide = body.find('rehideHiddenMessage("ocr_in_progress")')
    assert hide > guard.end(), "the hide runs before the superseded-batch guard"
    assert re.search(r"activeOcrSelection\s*=\s*null\s*;", body[guard.end() :]), (
        "endOcr no longer clears the active selection, so the submit gate "
        "would keep saying the file is being read"
    )


def test_in_flight_means_the_latest_selection_only():
    """A superseded batch that is still running must not count as in flight.

    Its text is dropped by scrub.ts, so telling the submit gate text is coming
    would be false, and keeping the indicator up alongside a failure message
    for the newer batch is a contradiction on screen (review).
    """
    body = _js_function(_form_source(), "export function isOcrInFlight")
    assert re.search(r"return\s+activeOcrSelection\s*!==\s*null\s*;", body), body


def test_the_uploader_passes_its_selection_through():
    """scrub.ts must hand the selection number to both ends of the read."""
    fn = _js_function_at(
        _scrub_source(), r"const recognizeEvent\s*=\s*async\s+function\s*\("
    )
    assert "beginOcr(selection);" in fn, fn
    assert re.search(r"finally\s*\{\s*endOcr\(selection\);", fn), fn


def test_submit_gate_keeps_the_box_up_while_reading_continues():
    """A blocked submit with page one already in the box used to hide it.

    Only clear it when nothing is being read; otherwise the remaining pages
    are read with no indicator (review).
    """
    validate = _js_function(_form_source(), "export function validateScrubForm")
    # `else if` or a standalone `if`: both guard the clear on nothing being in
    # flight, and the standalone form is a behaviour-preserving refactor
    # because the preceding branch requires a read in flight (review).
    m = re.search(
        r"(?:else\s+)?if\s*\(\s*!\s*isOcrInFlight\(\)\s*\)\s*\{", validate
    )
    assert m is not None, (
        "the submit gate clears ocr_in_progress unconditionally again"
    )
    block = _brace_block(validate, m.end() - 1)
    assert 'rehideHiddenMessage("ocr_in_progress")' in block, block
    # ...and it must not ALSO clear it on some unconditional path.
    outside = validate.replace(block, "")
    assert 'rehideHiddenMessage("ocr_in_progress")' not in outside, (
        "ocr_in_progress is still cleared outside the !isOcrInFlight() branch"
    )


def test_progress_box_is_not_styled_as_an_error():
    """A normal wait must not look like a failure.

    The declaration is checked, not just the selector: a rule that exists but
    sets red would pass a selector-only check (review).
    """
    html = (TEMPLATES / "scrub.html").read_text()
    tag = re.search(r'<div[^>]*id="ocr_in_progress"[^>]*>', html)
    assert tag is not None, "ocr_in_progress box not found in scrub.html"
    assert "hidden-progress-message" in tag.group(0), (
        "the progress box lost its neutral styling class and inherits the red "
        "of .hidden-error-message again"
    )
    css = (CSS / "custom.css").read_text()
    rule = re.search(
        r"\.hidden-error-message\.hidden-progress-message\s*\{([^}]*)\}", css
    )
    assert rule is not None, "the neutral colour rule is gone"
    color = re.search(r"color\s*:\s*([^;]+);", rule.group(1))
    assert color is not None, "the rule no longer sets a colour"
    # Pin the chosen neutral by its parsed RGB, with any !important stripped:
    # a deny-list of reds accepted "red !important", and an exact-string pin
    # rejected rgb(44, 62, 80), which is the same colour (review). Change
    # this and the CSS together.
    value = re.sub(r"\s*!important\s*$", "", color.group(1).strip().lower())
    assert _rgb(value) == (44, 62, 80), f"the progress box colour changed: {value}"


def test_progress_box_keeps_the_sibling_live_region_role():
    """role=alert, like every other message box on this form.

    A first cut changed it to role=status. Review pointed out that these boxes
    are toggled by visibility on already-populated markup, which live regions
    do not reliably treat as a change; alert has the special handling that
    made the existing boxes announce at all. Fixing that properly (an exposed,
    empty region whose text is set on show) is a change to the shared pattern
    and belongs in its own PR; until then this box must not be the one that
    announces less than its siblings.
    """
    html = (TEMPLATES / "scrub.html").read_text()
    tag = re.search(r'<div[^>]*id="ocr_in_progress"[^>]*>', html)
    assert tag is not None
    assert 'role="alert"' in tag.group(0), tag.group(0)


def test_progress_box_sits_by_the_uploader():
    """Under the file picker, above the textarea, not a screen away.

    The box used to live with the submit-gate messages below a 20-row
    textarea, so on a phone the person who had just picked a file saw
    nothing change (review). It must come after the uploader and before the
    denial textarea, which also makes its "below" wording true.
    """
    html = (TEMPLATES / "scrub.html").read_text()
    uploader = html.index('id="uploader"')
    box = html.index('id="ocr_in_progress"')
    textarea = html.index('id="denial_text"')
    assert uploader < box < textarea, (
        "the progress box is no longer between the uploader and the textarea"
    )


def test_progress_copy_has_no_em_dash():
    """House style: no em dashes in user-facing copy."""
    html = (TEMPLATES / "scrub.html").read_text()
    start = html.index('id="ocr_in_progress"')
    block = html[start : html.index("</div>", start)]
    assert "&mdash;" not in block and "—" not in block, (
        "an em dash is back in the OCR progress copy"
    )

"""The client-side submit gate must require every field it validates.

`validateScrubForm` displayed a "need_denial" error and then submitted the
form anyway, because the gate condition only checked pii/privacy/email. The
server then rejected it with `denial_text: This field is required`, so the
user saw the page complain and submit at the same time. An in-flight OCR run
made it trivial to hit: a scanned PDF is rendered and OCR'd a page at a time,
and nothing stopped submission during that window.

There is no JS test harness in this repo, so this asserts on the source.
"""

import pathlib
import re

JS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
)


def _form_source() -> str:
    return (JS / "scrub_client_side_form.ts").read_text()


def _submit_gate(src: str) -> str:
    """The multi-field `if (...)` that decides whether the form submits.

    Anchored on denialTextReady so it cannot accidentally match one of the
    single-field `if (form.pii.checked)` validations earlier in the file.
    """
    m = re.search(
        r"if \([^{}]*form\.pii\.checked[^{}]*denialTextReady[^{}]*\)\s*\{", src, re.S
    )
    assert m, "could not find the submit gate condition"
    return m.group(0)


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


def test_denial_text_ready_is_derived_from_the_trimmed_field():
    """Assert the boolean CONTRACT, not that the expression appears
    somewhere in the file: a regression could define denialTextReady from
    something else entirely and a substring search would still pass
    (external review)."""
    src = _form_source()
    m = re.search(r"const\s+denialTextReady\s*=\s*([^;]+);", src)
    assert m, "denialTextReady is not assigned"
    assert m.group(1).strip() == "form.denial_text.value.trim().length > 0", m.group(1)


def test_submit_gate_ANDs_every_field_it_validates():
    """Every field validated above the gate must also gate it, combined with
    AND. Previously the gate checked only pii/privacy/email, so the form
    showed "need_denial" and submitted anyway."""
    gate = _submit_gate(_form_source())
    # Exactly the server-required set (forms/__init__.py: pii, tos, privacy
    # required=True) plus email and denial_text.
    for field in ("pii", "privacy", "tos"):
        assert f"form.{field}.checked" in gate, f"{field} not gated: {gate}"
    assert "form.email.value.length > 0" in gate, gate
    assert "denialTextReady" in gate, gate
    # Combined with AND -- an OR would let any single field satisfy the gate.
    assert "||" not in gate, gate
    assert gate.count("&&") >= 4, gate


def test_the_gate_is_not_stricter_than_the_server():
    """personalonly is NOT required=True server-side. Gating on it made the
    client refuse a submission the server would have accepted, which the
    Selenium suite caught -- no test clicks that box because nothing requires
    it."""
    gate = _submit_gate(_form_source())
    assert "personalonly" not in gate, gate
    forms_src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "forms"
        / "__init__.py"
    ).read_text()
    assert "personalonly = forms.BooleanField(required=True)" not in forms_src


def test_whitespace_only_denial_text_is_treated_as_missing_everywhere():
    """The gate uses a trimmed predicate. If the validation branch does not,
    whitespace-only text blocks submission while showing no message at all --
    a silent refusal (external review)."""
    src = _form_source()
    assert "form.denial_text.value.trim().length < 1" in src
    # ...and no untrimmed length test on the field is left behind.
    assert not re.search(r"form\.denial_text\.value\.length\s*[<>]", src), src


def test_ocr_progress_message_is_cleared_when_the_last_run_finishes():
    """The gate only re-evaluates on submit, so endOcr must clear the message
    itself or it stays on screen after the file has been read."""
    end_ocr = _js_function(_form_source(), "export function endOcr")
    assert "ocrInFlight === 0" in end_ocr, end_ocr
    assert 'rehideHiddenMessage("ocr_in_progress")' in end_ocr, end_ocr


def test_the_uploader_releases_ocr_state_even_on_error():
    """Bind to the production path: recognizeEvent itself must wrap its
    recognize() loop and release in a finally, or a failed parse leaves the
    form permanently unsubmittable."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    assert "beginOcr();" in fn, fn
    assert "await recognize(" in fn, fn
    assert re.search(r"finally\s*\{\s*endOcr\(\);", fn), fn
    # ...and the release is inside the same function, after the loop.
    assert (
        fn.index("beginOcr();") < fn.index("await recognize(") < fn.index("endOcr();")
    )


def test_the_gate_actually_consults_ocr_state():
    src = _form_source()
    for fn in ("beginOcr", "endOcr", "isOcrInFlight"):
        assert f"export function {fn}" in src, fn
    validate = _js_function(src, "export function validateScrubForm")
    assert "isOcrInFlight()" in validate, validate
    assert 'showHiddenMessage("ocr_in_progress")' in validate, validate


def test_progress_message_element_exists():
    """The gate shows #ocr_in_progress; the template must define it or the
    user gets a silent refusal."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    assert 'id="ocr_in_progress"' in tpl


def test_a_failed_read_is_reported_instead_of_failing_silently():
    """recognize() throwing used to land in a handler with a finally and no
    catch, so every OCR engine failing left the textarea empty with no
    explanation -- under copy telling the user a file was enough. The
    production path must notice and say so."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    assert "noteOcrFailure()" in fn, fn
    # The failure branch has to be reachable: a catch around the recognize
    # call, not merely the pre-existing finally.
    assert re.search(r"catch\s*\([^)]*\)\s*\{[^}]*failures", fn), fn


def test_one_unreadable_page_does_not_abandon_the_rest():
    """The uploader is multiple="true" and people attach a denial one page
    per image. A single catch outside the loop stopped at the first bad page
    and silently dropped every page after it, so the catch must be INSIDE."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    loop = fn.index("for (const file of filesArray)")
    catch = fn.index("catch", loop)
    close = fn.index("finally", loop)
    assert loop < catch < close, fn


def test_producing_no_text_counts_as_a_failure_too():
    """Engines can return cleanly and still yield nothing for a photo too
    blurry to read. To the user that is identical to a crash: an empty box.
    Catching is therefore not enough -- what OCR produced has to be counted."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    # Counted from what OCR actually handed back, NOT by measuring the
    # textarea: measuring counts the user's own typing, so someone typing
    # while a doomed batch ran looked like OCR had produced something.
    assert "ocrChars" in fn, fn
    assert re.search(r"producedText\s*=\s*ocrChars\s*>\s*0", fn), fn
    assert re.search(r"!producedText", fn), fn
    assert "denialTextLength" not in fn, fn


def test_failure_message_element_exists():
    """The handler shows #ocr_failed; the template must define it or the
    report is a no-op and the silence returns."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    assert 'id="ocr_failed"' in tpl


def test_failure_message_clears_once_the_text_arrives():
    """However the text turns up -- a later file that read fine, or the user
    pasting it -- a stale "we couldn't read your file" is just noise."""
    src = _form_source()
    for fn in ("noteOcrFailure", "notePartialOcrFailure", "clearOcrFailure"):
        assert f"export function {fn}" in src, fn
    validate = _js_function(src, "export function validateScrubForm")
    assert 'rehideHiddenMessage("ocr_failed")' in validate, validate


def test_the_need_denial_message_stays_short():
    """It is the message a blocked user reads. It had grown to four
    sentences of editorial about insurers, which buries the one thing they
    have to do."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    block = tpl[tpl.index('id="need_denial"') :]
    block = block[: block.index("</div>")]
    words = len(re.sub(r"<[^>]+>", " ", block).split())
    assert words < 45, f"need_denial is {words} words:\n{block}"


def test_the_denial_file_is_never_posted_to_the_server():
    """The whole OCR design keeps the denial document in the browser:
    BaseDenialForm has no file field and nothing on /process reads one. But
    the uploader sits INSIDE the /process form, and a file input with a name
    is included in the multipart body regardless of whether Django binds it
    -- so every attached denial was uploaded and buffered for nothing. The
    input must stay nameless, and the copy promises exactly that."""
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    tag = re.search(r"<input[^>]*id=\"uploader\"[^>]*>", tpl)
    assert tag, "uploader input not found"
    assert "name=" not in tag.group(0), tag.group(0)
    # No other named file input may sneak into the same form either.
    for other in re.findall(r"<input[^>]*type=\"file\"[^>]*>", tpl):
        assert "name=" not in other, other


def test_a_superseded_selection_does_not_post_a_verdict():
    """Reading is slow enough that a user can pick again mid-run. Without a
    selection sequence a slow FAILING batch finishing after a newer
    successful one re-posts "we couldn't read your file" over text that had
    just arrived (CodeRabbit and Codex both, PR 988)."""
    src = (JS / "scrub.ts").read_text()
    assert "latestOcrSelection" in src, src
    fn = _js_function(src, "const recognizeEvent")
    assert "++latestOcrSelection" in fn, fn
    # The guard must sit BEFORE the reporting, or it guards nothing.
    guard = fn.index("selection !== latestOcrSelection")
    assert guard < fn.index("noteOcrFailure()"), fn


def test_partial_and_total_failure_are_different_messages():
    """One page failing while another succeeded is not "we couldn't read your
    file, the box is still empty" -- that is false with their text directly
    beneath it."""
    src = (JS / "scrub.ts").read_text()
    fn = _js_function(src, "const recognizeEvent")
    assert "notePartialOcrFailure()" in fn, fn
    assert "producedText" in fn, fn
    form = _form_source()
    for name in ("noteOcrFailure", "notePartialOcrFailure", "clearOcrFailure"):
        assert f"export function {name}" in form, name
    # The two states are mutually exclusive on screen.
    note_total = _js_function(form, "export function noteOcrFailure")
    note_partial = _js_function(form, "export function notePartialOcrFailure")
    assert 'rehideHiddenMessage("ocr_partial")' in note_total, note_total
    assert 'rehideHiddenMessage("ocr_failed")' in note_partial, note_partial


def test_typing_clears_the_failure_message():
    """hideErrorMessages runs on every keystroke in denial_text and cleared
    need_denial but not ocr_failed, so "the box below is still empty" stayed
    on screen while the user typed into that very box. The submit gate does
    not help: it only re-evaluates on submit."""
    hide = _js_function(_form_source(), "export function hideErrorMessages")
    assert 'rehideHiddenMessage("ocr_failed")' in hide, hide
    assert 'rehideHiddenMessage("ocr_partial")' in hide, hide


def test_partial_failure_message_element_exists():
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    assert 'id="ocr_partial"' in tpl


def test_a_superseded_batch_cannot_append_its_text():
    """Guarding only the verdict is not enough. recognize() invokes its
    callback after async OCR work, so passing addText straight through let a
    superseded batch write the OLD document's text into denial_text long
    after the user picked different files, with nothing on screen explaining
    where it came from (CodeRabbit, PR 988)."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    # The raw appender must not be handed to recognize().
    assert not re.search(r"recognize\(\s*file\s*,\s*addText\s*\)", fn), fn
    assert re.search(r"recognize\(\s*file\s*,\s*addTextForThisSelection\s*\)", fn), fn
    # ...and that wrapper drops writes once it is no longer the current pick.
    wrapper = fn[fn.index("addTextForThisSelection = ") :]
    wrapper = wrapper[: wrapper.index("beginOcr()")]
    assert "selection !== latestOcrSelection" in wrapper, wrapper
    assert wrapper.index("selection !== latestOcrSelection") < wrapper.index(
        "addText(text)"
    ), wrapper


def test_ocr_output_is_counted_separately_from_user_typing():
    """Measuring the textarea before and after counts the USER's keystrokes.
    Someone typing while a doomed batch ran therefore looked like OCR had
    produced something: it reported PARTIAL failure -- wrong -- and re-posted
    a message their typing had just cleared (CodeRabbit, PR 988)."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    # Counting happens where OCR hands text over, not by reading the DOM.
    assert re.search(r"ocrChars\s*\+=", fn), fn
    assert "denialTextLength" not in fn, fn
    # And nothing reads the textarea to decide the verdict.
    assert not re.search(r"denial_text[^\n]*\.value", fn), fn

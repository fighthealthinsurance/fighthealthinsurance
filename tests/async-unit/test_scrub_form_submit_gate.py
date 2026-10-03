"""The intake form's client-side checks, and the submit gate they decide.

Each check is one message under one field (or one group of tick boxes):
the letter, the email, the personal-details box, and the other three
agreements. The same list of checks shows the messages and decides whether
the form is sent, so a message never shows on a form that goes anyway, and
the list is exactly what the server's DenialForm requires. A blocked submit
moves focus to the first field with a problem, in page order.

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
TEMPLATES = (
    pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"
)


def _form_source() -> str:
    return (JS / "scrub_client_side_form.ts").read_text()


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


def _checks() -> "dict[str, str]":
    """intakeChecks' list, one entry per message id: the check's source."""
    body = _js_function(_form_source(), "function intakeChecks")
    found = dict(re.findall(r'check\(\s*"(\w+)",\s*(\[[^\]]*\][^\n]*)\)', body))
    assert found, body
    return found


def test_the_letter_counts_only_when_it_has_more_than_whitespace():
    """One trimmed predicate, used by the check and by the reading messages
    alike: whitespace on its own is a missing letter everywhere."""
    src = _form_source()
    has = _js_function(src, "function hasDenialText")
    assert "return form.denial_text.value.trim().length > 0;" in has, has
    assert "!hasDenialText(form)" in _checks()["need_denial"]
    validate = _js_function(src, "export function validateScrubForm")
    assert "const denialTextReady = hasDenialText(form);" in validate, validate
    # No untrimmed test of the letter's length anywhere.
    assert not re.search(r"form\.denial_text\.value\.length", src), src


def test_each_check_is_one_message_under_its_own_fields_in_page_order():
    assert list(_checks()) == [
        "need_denial",
        "email_error",
        "pii_error",
        "agree_chk_error",
    ]
    checks = _checks()
    assert checks["need_denial"].startswith("[form.denial_text]")
    assert checks["email_error"].startswith("[form.email]")
    assert checks["pii_error"].startswith("[form.pii]")
    assert checks["agree_chk_error"].startswith(
        "[form.privacy, form.tos, form.personalonly]"
    )


def test_the_checks_ask_for_exactly_what_the_server_requires():
    """The personal-use box included: DenialForm requires it, so the client
    does too, and nothing the server would accept is refused here."""
    from fighthealthinsurance.forms import DenialForm

    required = {name for name, field in DenialForm().fields.items() if field.required}
    assert "personalonly" in required
    checked = set(
        re.findall(
            r"form\.(\w+)",
            " ".join(fields.split("]")[0] for fields in _checks().values()),
        )
    )
    assert checked == required, (checked, required)


def test_every_check_that_shows_a_message_also_stops_the_form():
    """The checks that show the messages are the ones the gate reads, and a
    form with any field missing is not sent."""
    validate = _js_function(_form_source(), "export function validateScrubForm")
    assert "const checks = intakeChecks(form);" in validate, validate
    shown = validate.index("showFieldMessage(check)")
    gate = re.search(
        r"const missing = checks\.flatMap\(\(check\) => check\.missing\);\s*"
        r"if \(missing\.length > 0\) \{\s*(?://[^\n]*\s*)*event\.preventDefault\(\);\s*"
        r"focusFirst\(missing\);\s*return;",
        validate,
    )
    assert gate, validate
    assert shown < gate.start()
    # Nothing is added to the form for sending before the gate has passed.
    assert gate.end() < validate.index('hiddenFname.name = "fname"')
    # No second gate with its own idea of what is required.
    assert validate.count("preventDefault") == 1, validate


def test_a_blocked_submit_moves_focus_to_the_first_problem_in_page_order():
    focus = _js_function(_form_source(), "function focusFirst")
    # The earliest in the document, whatever order the checks list them in.
    assert "compareDocumentPosition(field) & Node.DOCUMENT_POSITION_PRECEDING" in focus
    assert "first.focus({ preventScroll: true });" in focus, focus
    assert 'first.scrollIntoView({ block: "center" });' in focus, focus


def test_a_page_the_server_sent_back_puts_focus_on_the_first_refused_field():
    src = _form_source()
    refused = _js_function(src, "export function focusFirstRefusedField")
    assert "querySelectorAll<HTMLElement>('[aria-invalid=\"true\"]')" in refused
    assert "focusFirst(" in refused
    setup = _js_function((JS / "scrub.ts").read_text(), "function setupScrub")
    assert "focusFirstRefusedField(form);" in setup, setup


def test_a_message_is_filled_when_shown_and_emptied_when_not():
    """Each message is an empty live region. Showing one puts its words in,
    which is what is read out; taking it down empties it again."""
    src = _form_source()
    show = _js_function(src, "function showFieldMessage")
    assert "message.dataset.message" in show, show
    assert "message.textContent = words;" in show, show
    assert "markFields(check, true);" in show, show
    clear = _js_function(src, "function clearFieldMessage")
    assert 'message.textContent = "";' in clear, clear
    assert "markFields(check, false);" in clear, clear
    # Nothing toggles a class to show these: their words are the state.
    for fn in (show, clear):
        assert "classList" not in fn, fn


def test_only_a_missing_field_is_marked_invalid_and_described_by_its_message():
    mark = _js_function(_form_source(), "function markFields")
    assert "const invalid = shown && check.missing.includes(field);" in mark, mark
    assert 'field.setAttribute("aria-invalid", "true");' in mark, mark
    assert 'field.removeAttribute("aria-invalid");' in mark, mark
    assert "describeBy(field, check.message, invalid);" in mark, mark
    describe = _js_function(_form_source(), "function describeBy")
    # The field's other descriptions stay.
    assert "token !== id" in describe, describe


def test_typing_or_ticking_never_raises_a_new_message():
    """hideErrorMessages runs on every keystroke and tick. It takes messages
    down, or narrows one to the boxes still unticked, and leaves new ones
    for the next submit."""
    hide = _js_function(_form_source(), "export function hideErrorMessages")
    assert "showFieldMessage" not in hide, hide
    assert "if (!isShowing(check))" in hide, hide
    assert "clearFieldMessage(check);" in hide, hide


def test_ocr_progress_message_is_cleared_when_the_last_run_finishes():
    """The gate only re-evaluates on submit, so endOcr must clear the message
    itself or it stays on screen after the file has been read."""
    end_ocr = _js_function(_form_source(), "export function endOcr")
    # Only the batch that is still current may clear it; a superseded batch
    # ending must not hide the indicator for the batch that replaced it.
    assert re.search(
        r"selection\s*!==\s*activeOcrSelection|activeOcrSelection\s*!==\s*selection",
        end_ocr,
    ), end_ocr
    assert 'rehideHiddenMessage("ocr_in_progress")' in end_ocr, end_ocr


def test_the_uploader_releases_ocr_state_even_on_error():
    """Bind to the production path: recognizeEvent itself must wrap its
    recognize() loop and release in a finally, or a failed parse leaves the
    form permanently unsubmittable."""
    fn = _js_function((JS / "scrub.ts").read_text(), "const recognizeEvent")
    assert "beginOcr(selection);" in fn, fn
    assert "await recognize(" in fn, fn
    assert re.search(r"finally\s*\{\s*endOcr\(selection\);", fn), fn
    # ...and the release is inside the same function, after the loop.
    assert (
        fn.index("beginOcr(selection);")
        < fn.index("await recognize(")
        < fn.index("endOcr(selection);")
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
    """It is the message a blocked user reads, so it says the one thing they
    have to do and little else."""
    tpl = (TEMPLATES / "scrub.html").read_text()
    include = re.search(r'with id="need_denial" message="([^"]+)"', tpl)
    assert include, "need_denial no longer carries its words in the template"
    words = len(include.group(1).split())
    assert words < 45, f"need_denial is {words} words:\n{include.group(1)}"


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


def test_a_dropped_denial_file_is_never_posted_to_the_server():
    """The letter box takes a dropped file too, and that must not open a
    second way out for the document. The drop handler hands the files to the
    nameless #uploader (the test above keeps it nameless) and fires its
    change event, so a dropped file is read by the same on-device listener
    as a chosen one. Nothing in the handler may send the file, give it a
    name, or build an input of its own to carry it."""
    src = (JS / "scrub.ts").read_text()
    handler = _js_function(src, "function acceptDroppedFiles")
    assert "uploader.files = event.dataTransfer.files;" in handler, handler
    assert 'uploader.dispatchEvent(new Event("change"' in handler, handler
    for way_out in (
        "fetch(",
        "XMLHttpRequest",
        "FormData",
        "sendBeacon",
        "WebSocket",
        ".submit(",
        ".name",
        "createElement",
        "setAttribute",
        "appendChild",
    ):
        assert way_out not in handler, f"the drop handler uses {way_out}:\n{handler}"
    # The input it is handed is the uploader, the one the change listener
    # that reads a chosen file is bound to.
    setup = _js_function(src, "function setupScrub")
    assert 'const elm = document.getElementById("uploader");' in setup, setup
    assert 'elm.addEventListener("change", recognizeEvent);' in setup, setup
    assert (
        'acceptDroppedFiles(box.closest("section") ?? box, box, elm as HTMLInputElement);'
        in setup
    ), setup


def _drop_listeners() -> "list[str]":
    """acceptDroppedFiles cut into its listeners, each from where it is
    declared or attached up to the next one."""
    handler = _js_function((JS / "scrub.ts").read_text(), "function acceptDroppedFiles")
    return re.split(
        r"(?=\b\w+\.addEventListener\()|(?=const offerDrop)|(?=const refuseDrop)",
        handler,
    )


def test_a_dropped_file_is_kept_from_the_browser():
    """Cancelling the browser's own handling is what stops it opening the
    file in place of the page, as well as reading it. Without it on the
    drag-over, the browser refuses the drop and no drop event comes at all."""
    listeners = _drop_listeners()
    offer = next(chunk for chunk in listeners if chunk.startswith("const offerDrop"))
    assert "event.preventDefault();" in offer, offer
    drop = next(
        chunk for chunk in listeners if chunk.startswith('zone.addEventListener("drop"')
    )
    assert "event.preventDefault();" in drop, drop
    assert drop.index("event.preventDefault();") < drop.index("uploader.files ="), drop


def test_a_file_dropped_on_the_file_button_is_read_too():
    """The file button is where most people drop a file, and the input behind
    it is hidden, so it takes no drop of its own. The handler listens on the
    letter's whole step, and that step holds the button as well as the box."""
    listeners = _drop_listeners()
    for event in ("dragenter", "dragover", "dragleave", "drop"):
        assert any(
            chunk.startswith(f'zone.addEventListener("{event}"') for chunk in listeners
        ), f"the step does not listen for {event}"
    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "fighthealthinsurance"
        / "templates"
        / "scrub.html"
    ).read_text()
    box = tpl.index('id="denial_text"')
    step = tpl[tpl.rindex("<section", 0, box) : tpl.index("</section>", box)]
    assert 'for="uploader"' in step, "the file button is outside the letter's step"


def test_a_file_dropped_anywhere_else_is_refused_not_opened():
    """A file let go outside the step would otherwise be opened by the
    browser in place of the page, taking everything typed so far with it."""
    listeners = _drop_listeners()
    refuse = next(chunk for chunk in listeners if chunk.startswith("const refuseDrop"))
    assert "zone.contains(event.target" in refuse, refuse
    assert "event.preventDefault();" in refuse, refuse
    assert 'dropEffect = "none"' in refuse, refuse
    for event in ("dragover", "drop"):
        assert f'window.addEventListener("{event}", refuseDrop);' in "".join(listeners)


def test_dragging_text_into_the_letter_box_is_left_to_the_browser():
    """Only a drag that carries files is taken over. Dragging a selection of
    text into or within the box must still drop the text, so each listener
    asks before it cancels the browser's own handling."""
    for listener in _drop_listeners():
        if "preventDefault()" not in listener:
            continue
        assert "carriesFiles(event)" in listener, listener
        assert listener.index("carriesFiles(event)") < listener.index(
            "preventDefault()"
        ), listener


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
    wrapper = wrapper[: wrapper.index("beginOcr(")]
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

"""Advanced OCR must ship OFF, on every surface, until the engine actually works.

The engine has never produced a character on prod: ``pipeline("image-to-text")``
resolves to ``AutoModelForVision2Seq``, whose registry does not contain
``qwen3_5``, so init throws ``Unsupported model type: qwen3_5`` and every caller
falls back to tesseract. Meanwhile the box was checked by default, so anyone
with WebGPU was offered a large download for a feature that could not run.

These pin the off-by-default state in every place it is decided, because the
default is one attribute and one boolean and both drift easily. There is no JS
test harness in this repo, so the client half asserts on the source, matching
``test_scrub_ocr_quality.py``.

Source is tokenized, not regexed, before asserting. Two views come out of it:
one with comments removed and string literals kept, for "no version pin
anywhere in executable code" (a pin inside a URL literal must still be caught);
and one with string literals blanked as well, for every structural check, so a
string cannot impersonate syntax. Template interpolations are code and stay
code in both views. Function declarations must be unique and at statement
position, so a decoy expression cannot be inspected in place of the real thing.

What these guards are for, and what they are not: they catch DRIFT, a future
edit that flips a default or reintroduces a pin without meaning to. They are
not a defence against someone deliberately writing evasive TypeScript in the
same file to defeat them; three review rounds have shown that game has no
floor. The defence against that is a reviewer reading the diff.

Delete these only together with a measurement showing the engine beats tesseract
on real denial scans. See ``qwen_webgpu_ocr.ts`` for what a working version needs.
"""

import html as html_lib
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
JS = REPO / "static" / "js"
TEMPLATES = REPO / "templates"


def _simple_string_end(src: str, i: int, quote: str) -> int:
    j = i + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == quote:
            return j + 1
        j += 1
    return len(src)


def _tokenize_ts(src: str, start: int = 0, until_close_brace: bool = False):
    """Split TypeScript into ('code' | 'string' | 'comment', text) segments.

    Returns ``(tokens, index)``. A template literal's quoted text is 'string';
    each ``${...}`` interpolation is tokenized recursively as code (with its
    own nested strings and comments), because an interpolation is executable
    and blanking it would hide ``${(checkbox.checked = true)}``. With
    ``until_close_brace`` the scan stops at the ``}`` that closes the
    interpolation it was called for. Regex literals are not recognized; none
    of the files asserted on contain one with ``//`` in it.
    """
    tokens = []
    buf = []
    i = start
    n = len(src)
    depth = 0

    def flush():
        if buf:
            tokens.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        c = src[i]
        if c in "\"'":
            flush()
            j = _simple_string_end(src, i, c)
            tokens.append(("string", src[i:j]))
            i = j
        elif c == "`":
            flush()
            sub, j = _template_tokens(src, i)
            tokens.extend(sub)
            i = j
        elif src.startswith("//", i):
            flush()
            k = src.find("\n", i)
            j = n if k < 0 else k
            tokens.append(("comment", src[i:j]))
            i = j
        elif src.startswith("/*", i):
            flush()
            k = src.find("*/", i + 2)
            j = n if k < 0 else k + 2
            tokens.append(("comment", src[i:j]))
            i = j
        elif until_close_brace and c == "}" and depth == 0:
            flush()
            return tokens, i + 1
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            buf.append(c)
            i += 1
    flush()
    return tokens, n


def _template_tokens(src: str, i: int):
    """Tokens for the template literal opening at ``src[i]``, and the end index."""
    assert src[i] == "`"
    tokens = [("string", "`")]
    chunk = []
    j = i + 1
    n = len(src)
    while j < n:
        c = src[j]
        if c == "\\":
            chunk.append(src[j : j + 2])
            j += 2
            continue
        if c == "`":
            tokens.append(("string", "".join(chunk) + "`"))
            return tokens, j + 1
        if src.startswith("${", j):
            tokens.append(("string", "".join(chunk)))
            chunk = []
            tokens.append(("code", "${"))
            inner, j = _tokenize_ts(src, j + 2, until_close_brace=True)
            tokens.extend(inner)
            tokens.append(("code", "}"))
            continue
        chunk.append(c)
        j += 1
    tokens.append(("string", "".join(chunk)))
    return tokens, n


def _executable(src: str) -> str:
    """Comments removed, string literals kept verbatim."""
    return "".join(t for k, t in _tokenize_ts(src)[0] if k != "comment")


def _structural(src: str) -> str:
    """Comments removed AND every string literal replaced by an empty one."""
    return "".join(
        t if k == "code" else ('""' if k == "string" else "")
        for k, t in _tokenize_ts(src)[0]
    )


def _strip_template_comments(html: str) -> str:
    """Django ``{# #}`` and HTML ``<!-- -->`` comments removed."""
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _brace_block(src: str, open_at: int) -> str:
    """``src[open_at:]`` up to and including the brace that closes ``open_at``."""
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


def _js_function(src: str, name: str) -> str:
    """Body of the one function DECLARED as ``name``.

    Two rules, each because review defeated the previous version:
    the name must appear as a function name exactly once in the file, so a
    decoy named function expression (``const d = function name() {...}``)
    cannot be inspected in place of the real one; and the declaration must sit
    at statement position (optionally ``export``/``async``), not after ``=``.
    """
    occurrences = re.findall(r"\bfunction\s+" + re.escape(name) + r"\b", src)
    assert len(occurrences) == 1, (
        f"expected exactly one `function {name}` in the file, found "
        f"{len(occurrences)}; a second one is a decoy the guard would inspect "
        "instead of the real function"
    )
    match = re.search(
        r"(?m)^[ \t]*(?:export\s+)?(?:async\s+)?function\s+"
        + re.escape(name)
        + r"\s*\(",
        src,
    )
    assert match is not None, f"function {name} is not declared at statement position"
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
    assert end_of_params is not None, f"unbalanced parens reading {name}"
    return _brace_block(src, src.index("{", end_of_params))


def _scrub_template() -> str:
    return _strip_template_comments((TEMPLATES / "scrub.html").read_text())


def _ocr_structural() -> str:
    return _structural((JS / "scrub_ocr.ts").read_text())


def _scrub_structural() -> str:
    return _structural((JS / "scrub.ts").read_text())


def _qwen_executable() -> str:
    return _executable((JS / "qwen_webgpu_ocr.ts").read_text())


def _qwen_structural() -> str:
    return _structural((JS / "qwen_webgpu_ocr.ts").read_text())


def _checkbox_tag(html: str) -> str:
    """The live advanced-OCR <input> tag, whole. Case-insensitive: HTML is."""
    match = re.search(r"<input[^>]*id=\"advanced_ocr_enabled\"[^>]*>", html, re.I)
    assert match is not None, "advanced_ocr_enabled checkbox not found in scrub.html"
    return match.group(0)


def test_tokenizer_keeps_a_pin_inside_a_nested_template_literal():
    """Guard the guard: the exact construct that beat an earlier stripper."""
    sample = "const u = `${`https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/`}`; // x\n"
    assert "onnxruntime-web@1.22.0" in _executable(sample)
    assert "onnxruntime-web@1.22.0" not in _structural(sample)
    assert "// x" not in _executable(sample)


def test_tokenizer_keeps_interpolation_code_in_the_structural_view():
    """And the construct that beat the next one: code hidden in ``${...}``."""
    sample = 'void `${(checkbox.checked = true)}`; const s = "if (x) {";\n'
    structural = _structural(sample)
    assert "checkbox.checked = true" in structural
    assert "if (x) {" not in structural


def test_scrub_checkbox_markup_is_not_checked():
    """The rendered box is what a user sees before they touch anything."""
    tag = _checkbox_tag(_scrub_template())
    assert not re.search(r"\bchecked\b", tag, re.I), (
        "advanced OCR is checked by default again; the engine is still broken "
        f"and this offers a large download for nothing. Tag: {tag}"
    )


def test_checkbox_init_never_turns_it_on():
    """scrub.ts may screen the box DOWN, never up.

    initAdvancedOCRCheckbox only ever sets checked=false. A single
    ``checkbox.checked = true`` there, including inside a template
    interpolation, would restore the download for every WebGPU visitor while
    the markup test above still passed.
    """
    assert not re.search(r"\.checked\s*=\s*true\b", _scrub_structural()), (
        "scrub.ts sets a checkbox to true; the advanced OCR box must never be "
        "turned on by initialization, only by the user"
    )


def test_a_browser_that_cannot_run_it_cannot_turn_it_on():
    """Unavailable is not just unchecked: the box is disabled, so a person on
    Safari (or without WebGPU) cannot tick an option whose pass always fails."""
    init = _js_function(_scrub_source(), "initAdvancedOCRCheckbox")
    unavailable = re.search(r"if \(!webGpu\.available\) \{", init)
    assert unavailable is not None
    block = _brace_block(init, unavailable.end() - 1)
    assert "checkbox.checked = false;" in block and "checkbox.disabled = true;" in block
    # Capability is screened before the defaults that return early.
    assert unavailable.start() < init.index("userAskedToSaveData()"), "the data-saver screen skips the capability check"
    assert unavailable.start() < init.index("deviceMemory"), "the small-device screen skips the capability check"


def test_pages_without_the_checkbox_default_off():
    """explain_denial and chat_interface render no checkbox, so they cannot opt out.

    ``isAdvancedOCREnabled`` decides for them, and defaulting true there started
    the download on pages that never showed the user a choice. Pin the exact
    return, and that it is the only one, so a second early ``return true`` above
    it is caught too. Strings are blanked first, so a string cannot supply the
    expected text.
    """
    body = _js_function(_ocr_structural(), "isAdvancedOCREnabled")
    returns = re.findall(r"\breturn\b[^;]*;", body)
    assert len(returns) == 1, f"expected exactly one return, found {returns}"
    # Either spelling of "the box if present, else false": the ternary, or
    # optional chaining with a nullish default (review).
    assert re.fullmatch(
        r"return\s+checkbox\s*\?\s*checkbox\.checked\s*:\s*false\s*;"
        r"|return\s+checkbox\?\.checked\s*\?\?\s*false\s*;",
        returns[0],
    ), (
        "isAdvancedOCREnabled no longer returns `checkbox ? checkbox.checked : "
        f"false`; the no-opt-out pages would start downloading again. Got: "
        f"{returns[0]}"
    )


def test_qwen_engine_only_runs_inside_the_checkbox_gate():
    """The gate is worthless if the engine is launched outside it.

    Brace-match the ``if (isAdvancedOCREnabled())`` block on string-blanked
    source and require the one launch to sit inside it. A string containing the
    gate's text, a ``}`` inside a string, or a launch hidden in a template
    interpolation cannot fake or evade the gate here.
    """
    body = _js_function(_ocr_structural(), "recognizeImageText")
    launch = r"recognizeWithQwenWebGPU\s*\("
    assert len(re.findall(launch, body)) == 1, (
        "recognizeWithQwenWebGPU is launched more than once, or not at all, in "
        "recognizeImageText"
    )
    gate = re.search(r"if\s*\(\s*isAdvancedOCREnabled\s*\(\s*\)\s*\)\s*\{", body)
    assert gate is not None, "no `if (isAdvancedOCREnabled())` block in recognizeImageText"
    block = _brace_block(body, gate.end() - 1)
    assert re.search(launch, block), (
        "the qwen engine is launched outside the `if (isAdvancedOCREnabled())` "
        "block; the checkbox would stop meaning anything"
    )


def test_no_hardcoded_onnxruntime_version_pin():
    """The runtime files must match the build inside the library, never a
    version someone typed.

    A hardcoded ``onnxruntime-web@1.22.0`` CDN path shipped for months while
    the library carried 1.24.2, so the runtime asked for ``asyncify`` files
    that 1.22.0's dist does not contain and the fetch 404'd. The runtime now
    ships from this origin, copied from the library's OWN dependency by the
    build (webpack.config.js resolves it from the library's directory), so
    the only wasmPaths assignment allowed is the one pointing there, and no
    version string may appear on either side.
    """
    structural = _qwen_structural()
    assignments = re.findall(r"\bwasmPaths\s*=(?!=)[^;]*;", structural)
    assert assignments == ["wasmPaths = ortRuntimePaths();"], (
        f"wasmPaths is set to something other than the first-party runtime: {assignments}"
    )
    assert not re.search(r"onnxruntime-web@\d", _qwen_executable()), (
        "a hardcoded onnxruntime-web version is back in qwen_webgpu_ocr.ts"
    )
    webpack = (JS / "webpack.config.js").read_text()
    assert not re.search(r"onnxruntime-web[@/]\d", webpack), "a runtime version is typed into the build"


def test_the_option_is_offered_only_behind_the_setting():
    """A person uploading a letter must not be offered a broken option.

    The whole section sits behind ADVANCED_OCR_OFFERED, which defaults to
    off; when the engine works again the environment flips it, and the
    checkbox still starts unchecked (the test above).
    """
    html = _scrub_template()
    start = html.index('id="advanced_ocr_section"')
    gate = html.rfind("{% if advanced_ocr_offered %}", 0, start)
    assert gate != -1, "the advanced OCR section is no longer gated by advanced_ocr_offered"
    # The gate closes after the whole section (label, status line, remove
    # control) and before the uploader that follows it.
    closing = html.index("{% endif %}", start)
    assert closing < html.index('id="image_select_magic"', start), "the section's {% endif %} is not where the section ends"
    settings_src = (REPO / "settings.py").read_text()
    assert re.search(
        r'ADVANCED_OCR_OFFERED\s*=\s*os\.getenv\("ADVANCED_OCR_OFFERED",\s*"false"\)',
        settings_src,
    ), "ADVANCED_OCR_OFFERED no longer defaults to off"
    assert "fighthealthinsurance.context_processors.advanced_ocr_context" in settings_src, (
        "the context processor that exposes advanced_ocr_offered is not registered"
    )


def test_the_label_speaks_to_a_person():
    """When the option is shown, the label says what a person needs and
    nothing a reviewer needs: what it is, that it is experimental, the
    download size with its unit, that the file stays on the device, and
    what standard recognition is like. No hosting site, no failure
    narrative, no partial-download figure, no engine names.
    """
    html = _scrub_template()
    start = html.index('id="advanced_ocr_section"')
    label = html_lib.unescape(html[start : html.index("</label>", start)])
    label = re.sub(r"\s+", " ", label)
    assert "experimental" in label.lower()
    assert re.search(r"\babout \d{3} MB\b", label), "the label no longer states the download size in MB"
    assert "stays on your device" in label
    assert "Standard recognition" in label
    for leak in ("huggingface", "not working", "fails to load", "20 MB", "19 MB", "WebGPU", "wasm", "Qwen", "model files"):
        assert leak.lower() not in label.lower(), f"the label still says {leak!r}"
    assert not re.search(r"needs\s+no\s+download", label, re.I), (
        "the label claims standard OCR needs no download; tesseract downloads "
        "a language file on first use"
    )


# ---------------------------------------------------------------------------
# The on-device model's own pass. It never races the standard engines (it is a
# minute or more per page); it reads after them, replaces the text only if the
# person has not touched it, offers a button otherwise, and can be removed
# from the device. The library ships with the build and loads as a real module
# from this origin.
# ---------------------------------------------------------------------------


def _scrub_source() -> str:
    return (JS / "scrub.ts").read_text()


def test_the_model_is_not_in_the_race():
    body = _js_function(_ocr_structural(), "recognizeImageText")
    gate = re.search(r"if\s*\(\s*isAdvancedOCREnabled\s*\(\s*\)\s*\)\s*\{", body)
    assert gate is not None
    block = _brace_block(body, gate.end() - 1)
    assert "engines.push" not in block, "the model is back in the race it cannot win"
    assert re.search(r"onDeviceRead\s*=\s*\(\s*\)\s*=>\s*recognizeWithQwenWebGPU\s*\(", block), (
        "the model's read is not kept as a thunk for after the standard pass"
    )


def test_each_read_travels_with_the_text_it_stands_for():
    """The read goes out WITH the page's text, through the caller's own
    callback, so the caller knows the order and nothing is kept in this
    module between pages (review: a module-level queue let a superseded
    upload's pages into the next upload's replacement)."""
    src = _ocr_structural()
    for gone in ("beginOnDeviceReads", "takeOnDeviceReads", "queueOnDeviceRead", "onDeviceSelection"):
        assert gone not in src, f"a module-level read queue is back ({gone})"
    assert "addText(text, onDeviceReadFor(text, results.onDeviceRead));" in src, (
        "the image route no longer hands its read out with its text"
    )
    pdf = _js_function(src, "recognizePDFPage")
    # Structural view: string literals are emptied.
    emit = pdf.index('addText(parts.join("") + "", onDeviceReadFor(ocrText, pageRead, pageText));')
    assert "onDeviceReadFor" not in pdf[:emit], "a page that used its own text layer gets a model read"
    helper = _js_function(src, "onDeviceReadFor")
    assert "standard.trim().length === 0" in helper, "a page with no standard text is handed to the model"
    assert re.search(r"export type AddText = \(text: string, read\?: OnDeviceRead\) => void;", src)
    # The wrapper in recognize() sits between every emit site and the caller;
    # a one-argument version dropped the reads and the model never ran.
    entry = re.search(r"export const recognize = async function \(", src)
    assert entry is not None
    body = _brace_block(src, src.index("{", src.index(")", entry.end())))
    assert "const emit: AddText = (text, read): void => {" in body and "addText(text, read);" in body, (
        "recognize()'s wrapper no longer forwards the page's read"
    )


def _recognize_event_body() -> str:
    """recognizeEvent is declared as `const recognizeEvent = async function (`,
    so the plain `function name(` locator does not find it."""
    src = _scrub_source()
    head = re.search(r"const recognizeEvent\s*=\s*async function\s*\(", src)
    assert head is not None, "recognizeEvent is no longer declared as an async function expression"
    return _brace_block(src, src.index("{", src.index(")", head.end())))


def test_the_on_device_pass_runs_after_the_standard_pass_and_only_when_enabled():
    body = _recognize_event_body()
    kick = re.search(r"if\s*\(([^{]*)\)\s*\{\s*void improveWithOnDeviceModel\(", body)
    assert kick is not None, "no kick-off of improveWithOnDeviceModel"
    assert body.index("endOcr(selection)") < kick.start()
    cond = kick.group(1)
    for must in ("isAdvancedOCREnabled()", "selection === latestOcrSelection", "ocrChars > 0", "pagesToRead > 0"):
        assert must in cond, f"the kick-off no longer requires {must}"
    # Pages are recorded per selection, in order, AFTER the selection guard,
    # so a superseded upload's late page never enters the newer upload's
    # layout.
    callback = re.search(r"const addTextForThisSelection = \(text: string, read\?: OnDeviceRead\): void => \{", body)
    assert callback is not None, "the callback no longer takes the page's read"
    cb = _brace_block(body, callback.end() - 1)
    assert re.search(r"if \(selection !== latestOcrSelection\) \{\s*return;\s*\}", cb), (
        "a superseded upload's page is no longer dropped at the guard"
    )
    assert cb.index("if (selection !== latestOcrSelection)") < cb.index("pieces.push({ text, read })")
    # The baseline is taken from the box as it really is, before the chunk.
    assert body.index("syncTracker(textarea.value);") < body.index("const editsAtStart = userEditsSeen;") < body.index("const chunk = trackChunk(")
    assert cb.index("addText(text);") < cb.index("noteProgrammaticAppend(chunk, wasLength, textarea.value)") < cb.index("pieces.push")
    # A new selection drops every earlier one's controls and tracked text.
    assert body.index("clearOnDeviceStatus()") < body.index("beginOcr(selection)")
    assert body.index("trackedChunks.clear()") < body.index("const chunk = trackChunk(textarea.value.length)") < body.index("beginOcr(selection)")


def test_text_is_followed_by_position_never_found_by_search():
    src = _scrub_source()
    follow = _js_function(src, "syncTracker")
    assert "userEditsSeen += 1;" in follow
    # Before the range: shift. Inside it: dirty. After it: nothing.
    assert re.search(r"if \(editEnd <= chunk\.start\) \{\s*chunk\.start \+= delta;\s*chunk\.end \+= delta;", follow)
    assert re.search(r"else if \(editStart < chunk\.end\) \{\s*chunk\.dirty = true;", follow)
    # The suffix is bounded by the prefix, so an ambiguous edit resolves to
    # the later position; the content check in apply makes that safe, and
    # widening the region marked the commonest case dirty (page check).
    assert re.search(r"while \(\s*suffix < shortest - prefix &&", follow), "the suffix is no longer bounded by the prefix"
    assert "const editStart = prefix;" in follow and "const editEnd = previous.length - suffix;" in follow
    assert "overlap" not in follow
    append = _js_function(src, "noteProgrammaticAppend")
    assert re.search(r"if \(chunk\.end === wasLength\) \{\s*chunk\.end = value\.length;\s*\} else \{\s*chunk\.dirty = true;", append), (
        "a page appended after the person typed at the end is treated as contiguous"
    )
    assert "lastKnownValue = value;" in append
    assert 'textarea.addEventListener("input", () => noteUserInput(textarea.value))' in src
    # An input event counts even when the value comes out the same.
    assert re.search(r"function noteUserInput\(value: string\): void \{\s*userEditsSeen \+= 1;\s*syncTracker\(value\);", src)
    setup = _js_function(src, "setupScrub")
    assert setup.index("getLocalStorageItemWithTTL(textarea.id)") < setup.index("followDenialText(textarea)"), (
        "the restored draft is not the baseline the first keystroke is diffed against"
    )
    improve = _js_function(src, "improveWithOnDeviceModel")
    for search in ("indexOf", "lastIndexOf", ".search(", ".includes("):
        assert search not in improve, f"the swap locates text by searching again ({search})"


def test_the_tracker_is_synced_before_every_decision():
    """Not every write to the box is an input event (the Remove PII button
    assigns the value directly), so the tracker looks again before it trusts
    a position: before each page is appended, before the untouched verdict,
    in apply, and in Undo."""
    src = _scrub_source()
    body = _recognize_event_body()
    callback = re.search(r"const addTextForThisSelection = \(text: string, read\?: OnDeviceRead\): void => \{", body)
    cb = _brace_block(body, callback.end() - 1)
    assert cb.index("syncTracker(textarea.value);") < cb.index("const wasLength = textarea.value.length;") < cb.index("addText(text);")
    improve = _js_function(src, "improveWithOnDeviceModel")
    assert re.search(r"syncTracker\(textarea\.value\);\s*const untouched =", improve), "the verdict is reached on stale positions"
    apply = re.search(r"const apply = \(\): void => \{", improve)
    apply_block = _brace_block(improve, apply.end() - 1)
    assert apply_block.index("syncTracker(textarea.value);") < apply_block.index("const previous = textarea.value;")
    undo = re.search(r"const undoable = \(start: number, applied: string, restore: string\): void => \{", improve)
    assert undo is not None, "Undo no longer tracks where the reading went"
    undo_block = _brace_block(improve, undo.end() - 1)
    assert "const placed = trackRange(start, start + applied.length);" in undo_block
    assert undo_block.index("syncTracker(textarea.value);") < undo_block.index("if (placed.dirty || current.slice(placed.start, placed.end) !== applied) {")
    assert re.search(r'onDeviceStatus\("You changed the text since, so it was left as it is\."\);\s*return;', undo_block), (
        "Undo goes on to restore after saying it was skipped, taking later typing with it"
    )
    assert "current.slice(0, placed.start) + restore + current.slice(placed.end)" in undo_block


def test_the_model_replaces_the_text_only_if_untouched_and_offers_undo():
    body = _js_function(_scrub_source(), "improveWithOnDeviceModel")
    # Untouched: no typing since the pass began, the chunk never edited
    # inside, and the text at the tracked position is exactly what went out.
    untouched = re.search(r"const untouched =\s*userEditsSeen === editsAtStart &&\s*!chunk\.dirty &&\s*textarea\.value\.slice\(chunk\.start, chunk\.end\) === standardText;", body)
    assert untouched is not None, "the untouched rule lost a part"
    guard = re.search(r"if\s*\(\s*untouched\s*\)\s*\{", body)
    assert guard is not None
    block = _brace_block(body, guard.end() - 1)
    assert "apply();" in block
    assert "Use its reading instead" in body[guard.end() + len(block) :], "an edited box no longer gets the offer"
    # Page by page, by position: a page's reading stands in for the standard
    # text at the START of its piece, and a page without a reading keeps its
    # text.
    assert re.search(r"if \(!reading \|\| !piece\.read \|\| !piece\.text\.startsWith\(piece\.read\.standard\)\) \{\s*return piece\.text;", body), (
        "a page without a reading, or a digital page, no longer keeps its text verbatim"
    )
    # A PDF page's exact text layer survives a reading that did not
    # reproduce it, unless it already follows the standard text.
    assert "const keepExact = exact.length > 0 && !containsNormalised(reading, exact) && !containsNormalised(tail, exact);" in body
    assert 'return reading + (keepExact ? "\\n" + exact : "") + tail;' in body
    apply = re.search(r"const apply = \(\): void => \{", body)
    assert apply is not None
    apply_block = _brace_block(body, apply.end() - 1)
    assert "if (chunk.dirty || previous.slice(chunk.start, chunk.end) !== standardText) {" in apply_block, (
        "the swap no longer checks the text at the tracked position is what went out"
    )
    # The offer is INSTEAD of the swap: without the return that follows it,
    # the dirty chunk would be overwritten right after being offered.
    assert re.search(r"offerAppend\(\);\s*return;", apply_block), "apply goes on to swap after offering the append"
    assert "previous.slice(0, chunk.start) + improvedText + previous.slice(chunk.end)" in apply_block
    assert "undoable(chunk.start, improvedText, standardText);" in apply_block
    # Stale controls do nothing once a newer selection exists: each of the
    # apply, append and Undo callbacks clears the status AND returns.
    assert len(re.findall(r"if \(selection !== latestOcrSelection\) \{\s*clearOnDeviceStatus\(\);\s*return;\s*\}", body)) >= 3, (
        "a stale control goes on to change the box after clearing the status"
    )
    # After the pass, a superseded selection RETURNS (its status and controls
    # belong to the newer one), not just checks.
    assert re.search(r"if \(selection !== latestOcrSelection\) \{\s*untrackChunk\(chunk\);\s*return;\s*\}", body), (
        "a superseded pass goes on to write status and controls over the newer selection's"
    )
    assert body.count("if (selection !== latestOcrSelection) return;") == 1, "a superseded selection is not dropped mid-pass"


def test_programmatic_writes_do_not_look_like_typing_or_hide_warnings():
    body = _js_function(_scrub_source(), "improveWithOnDeviceModel")
    assert "dispatchEvent" not in body, (
        "a synthetic input event hides the partial-read warning the model's reading does not resolve"
    )
    set_value = re.search(r"const setValue = \(value: string\): void => \{", body)
    assert set_value is not None
    sv = _brace_block(body, set_value.end() - 1)
    assert "lastKnownValue = value;" in sv, "a programmatic write is diffed as typing at the next keystroke"
    assert re.search(r"try \{\s*setLocalStorageItemWithTTL\(textarea\.id, value\);\s*\} catch", sv), (
        "a full or blocked storage throws before Undo is installed"
    )
    # The remove control shows after the pass whether or not the selection
    # was superseded: the model was downloaded either way.
    assert body.index("void showRemoveModelControl();") < body.index("if (selection !== latestOcrSelection) {\n    untrackChunk(chunk);")


def test_passes_and_removal_share_one_lock():
    src = _scrub_source()
    lock = _js_function(src, "withOnDeviceModel")
    assert re.search(r"try \{\s*await previous;\s*await work\(\);\s*\} finally \{\s*release\(\);\s*\}", lock), (
        "the lock is not released in a finally; every later pass would wait forever"
    )
    improve = _js_function(src, "improveWithOnDeviceModel")
    assert improve.index("onDevicePassesActive += 1;") < improve.index("await withOnDeviceModel(async () => {")
    assert re.search(r"\} finally \{\s*onDevicePassesActive -= 1;", improve)
    init = _js_function(src, "initRemoveModelControl")
    assert re.search(r"if \(onDevicePassesActive > 0\) \{\s*button\.textContent = \"[^\"]*\";\s*return;\s*\}", init), (
        "removal no longer refuses while a pass is queued or reading; it would queue behind it instead"
    )
    assert "const held = await tryWithOnDeviceModel(async () => {" in init, "removal is not under the passes' lock"
    assert "in use in another tab" in init
    # Cross-tab where the browser has Web Locks: the bucket is shared by
    # every tab of the origin.
    lock_fn = _js_function(src, "withOnDeviceModel")
    assert "await locks.request(ON_DEVICE_MODEL_LOCK, async () => {" in lock_fn
    try_fn = _js_function(src, "tryWithOnDeviceModel")
    assert "locks.request(ON_DEVICE_MODEL_LOCK, { ifAvailable: true }, async (lock) => {" in try_fn
    assert re.search(r"if \(!lock\) \{\s*return false;", try_fn)
    assert init.index("caches.delete(ON_DEVICE_MODEL_CACHE)") < init.index("await caches.has(ON_DEVICE_MODEL_CACHE)"), (
        "removal no longer checks the bucket is really gone before saying so"
    )
    # After a failed load the library's own downloads may still be running
    # and land in the bucket later; the control says so instead of promising
    # a clean removal.
    # ... in this tab or another: the caution is read through local storage
    # so a load abandoned in another tab is not promised away here.
    assert "if (gone && onDeviceLoadMayStillBeRunning()) {" in init
    qwen_src = (JS / "qwen_webgpu_ocr.ts").read_text()
    may = _js_function(qwen_src, "onDeviceLoadMayStillBeRunning")
    # One storage key per tab (no shared map to race on), cleared only when
    # that tab's load fully succeeded or the tab goes away; a day-old entry
    # is a crash.
    assert "key.startsWith(ABANDONED_LOAD_KEY_PREFIX)" in may and "ABANDONED_LOAD_STALE_MS" in may
    assert "JSON.parse" not in qwen_src, "a shared JSON map is back; two tabs' writes erase each other"
    assert re.search(r"window\.localStorage\.setItem\(ownAbandonedLoadKey, String\(Date\.now\(\)\)\);", qwen_src)
    assert re.search(r"window\.localStorage\.removeItem\(ownAbandonedLoadKey\);", qwen_src)
    raw_load = _js_function(qwen_src, "loadQwenOCRRuntimeRaw")
    assert re.search(r"loadPending = false;\s*if \(processorSettled\.status === \"fulfilled\" && modelSettled\.status === \"fulfilled\"\) \{\s*clearAbandonedLoad\(\);", raw_load), (
        "the caution is withdrawn on a load that settled by failing, while sibling downloads run on"
    )
    assert re.search(r'window\.addEventListener\("pagehide", \(event: PageTransitionEvent\) => \{\s*if \(!event\.persisted\) \{\s*clearAbandonedLoad\(\);', qwen_src), (
        "a page going into the back/forward cache clears its caution although it can come back with its downloads"
    )
    assert re.search(r"if \(abandonedInThisTab\) \{\s*return true;", may), "with storage full, this tab forgets its own abandoned load"
    assert re.search(r"function noteAbandonedLoad\(\): void \{\s*abandonedInThisTab = true;", qwen_src)
    assert re.search(r"const ABANDONED_LOAD_STALE_MS = 24 \* 60 \* 60_000;", qwen_src)
    give_up_note = _js_function(qwen_src, "giveUpOnDeviceModel")
    assert re.search(r"if \(loadPending\) \{\s*noteAbandonedLoad\(\);", give_up_note)
    # Downloaded again after a removal: the control comes back enabled with
    # its own label, not stuck disabled saying Removed.
    show = _js_function(src, "showRemoveModelControl")
    assert re.search(r"button\.disabled = false;\s*button\.textContent = removeModelLabel \|\| button\.textContent;\s*button\.hidden = false;", show)
    assert 'removeModelLabel = button.textContent ?? "";' in init
    assert "here or in another tab, may leave files behind" in init
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    assert re.search(r"\} catch \(error\) \{\s*loadFailed = true;", _js_function(qwen, "loadQwenOCRRuntimeRaw")), (
        "a failed load is no longer remembered"
    )


def test_every_page_read_is_bounded_and_a_timeout_switches_the_model_off():
    """A download interrupted by a network change left the library's load
    pending forever, and with it the pass, the lock and the remove control
    (page check). Each read races a budget; past it the model is switched off
    for the rest of the visit so no later read waits on the same load."""
    src = _scrub_source()
    assert re.search(r"const ON_DEVICE_PAGE_TIMEOUT_MS = \d+ \* 60_000;", src)
    improve = _js_function(src, "improveWithOnDeviceModel")
    assert "await readWithTimeout(piece.read.run, () => {" in improve, "a page read is awaited without a time budget"
    assert "piece.read.run()" not in improve
    timeout = _js_function(src, "readWithTimeout")
    assert re.search(r"window\.setTimeout\(\(\) => \{\s*giveUpOnDeviceModel\(\);", timeout), (
        "a timed-out read no longer switches the model off; the next read waits on the same load"
    )
    # The person is told at once; the pass (and with it the lock) ends only
    # once the interrupted generation has actually stopped, with no grace
    # that would hand the lock over while the GPU is still busy.
    assert re.search(r"giveUpOnDeviceModel\(\);\s*onTimeout\(\);\s*void onDeviceGenerationSettled\(\)\.then\(\(\) => \{\s*reject\(", timeout), (
        "the timeout rejects before the generation has stopped, or without telling the person"
    )
    assert "Promise.race" not in timeout and "GRACE" not in src, "a grace path hands the lock over with the GPU busy"
    assert re.search(r"await readWithTimeout\(piece\.read\.run, \(\) => \{\s*timedOut = true;\s*(//[^\n]*\s*)*if \(selection === latestOcrSelection\) \{\s*onDeviceStatus\(TIMED_OUT_STATUS\);", improve), (
        "a superseded selection's timeout writes over the current selection's status"
    )
    assert timeout.count("window.clearTimeout(timer);") == 2
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    give_up = _js_function(qwen, "giveUpOnDeviceModel")
    assert "loadFailed = true;" in give_up and "qwenRuntimeDisabled = true;" in give_up
    # Reads honor the flag before AND after the load: a load that outran its
    # budget still completes and must not start a generation.
    read = _js_function(qwen, "recognizeWithQwenWebGPU")
    checks = [m.start() for m in re.finditer(r"if \(qwenRuntimeDisabled\) \{\s*return \"\";", read)]
    load = read.index("await loadQwenOCRRuntime()")
    generate = read.index("runtime.model.generate(")
    processed = read.index("await runtime.processor(prompt, image)")
    assert any(c < load for c in checks) and any(load < c < processed for c in checks) and any(processed < c < generate for c in checks), (
        "the disabled flag is not honored before the load, after it, and again after the image work right before generation"
    )
    # A generation that used every token it was allowed did not reach the end
    # of the page: no reading, the standard text stays.
    assert re.search(r"if \(generated >= MAX_NEW_TOKENS\) \{\s*console\.warn\([^\n]*\);\s*return \"\";", read), (
        "a capped transcription replaces a complete standard reading"
    )
    assert read.index("const generated = outputs.dims[outputs.dims.length - 1] - promptLength;") < read.index("batch_decode(")


def test_a_stalled_download_is_given_up_not_waited_on_forever():
    """A route change mid-download stalls the library's stream and its load
    never rejects (page check: two cold loads hung until the page budget).
    Progress is watched while files are in flight; a stall gives the load up
    for this visit with a reload hint. Not retried: the library dedupes
    in-flight downloads, so a second load would wait on the same stalled
    stream (review). A load given up on that finishes later releases its
    model instead of leaking it."""
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    assert re.search(r"const DOWNLOAD_STALL_MS = \d+_000;", qwen)
    assert "LOAD_ATTEMPTS" not in qwen, "a retry is back; it waits on the same in-flight download"
    raw = _js_function(qwen, "loadQwenOCRRuntimeRaw")
    assert raw.count("progress_callback") >= 3, "a load is missing the progress callback the watchdog needs"
    # In flight from the first byte, not from "initiate": an optional file
    # the hub answers with a 404 never reports "done".
    assert re.search(r'else if \(event\.status === "download" \|\| event\.status === "progress"\) \{\s*inFlight\.add\(event\.file\);', raw), (
        "a file is counted in flight from initiate; a 404 on an optional file arms the watchdog for good"
    )
    assert re.search(r"if \(inFlight\.size > 0 && performance\.now\(\) - lastProgressAt > DOWNLOAD_STALL_MS\) \{", raw), (
        "the stall watchdog no longer requires a file in flight; shader compilation would trip it"
    )
    assert "await Promise.race([loads, stalled])" in raw
    assert re.search(r"window\.clearInterval\(timer\);\s*noteAbandonedLoad\(\);", raw), "a stall is not recorded for the remove control"
    # Any failed load: the library's sibling downloads may run on, and a model
    # that loaded (or lands later) is released.
    assert re.search(r"\} catch \(error\) \{\s*loadFailed = true;[^}]*noteAbandonedLoad\(\);\s*releaseModelIfUnused\(\);", raw), (
        "a failed load keeps its model or skips the cross-tab caution"
    )
    # Not started after the page gave up while the import was pending.
    assert raw.index("if (qwenRuntimeDisabled) {") < raw.index("const modelPromise = "), (
        "the downloads can start after a give-up, outside the lock"
    )
    assert re.search(r"if \(error instanceof DownloadStalled\) \{\s*console\.warn\(", raw)
    memo = _js_function(qwen, "loadQwenOCRRuntime")
    assert "runtimeLoad = null" not in memo, "a stalled load is retried; it would wait on the same stream"
    src = _scrub_source()
    improve = _js_function(src, "improveWithOnDeviceModel")
    assert "onDeviceModelLoadFailed()" in improve, "a failed load no longer gets the reload hint"
    assert "could not be loaded; reload the page to try again" in improve


def test_giving_up_stops_a_running_generation():
    """A timed-out generation must not keep the GPU busy after the pass has
    released the lock (review: a second tab could then generate alongside
    it). The library's stop switch is handed to generate() and pulled on
    give-up; a stopped generation's output is not a reading."""
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    give_up = _js_function(qwen, "giveUpOnDeviceModel")
    assert "runningGeneration?.interrupt();" in give_up
    # The model is not wanted any more, loaded or still loading: released
    # once any generation on it has stopped, and released on arrival if it
    # lands later; a model whose processor failed is not stranded either.
    assert "releaseModelIfUnused();" in give_up
    release = _js_function(qwen, "releaseModelIfUnused")
    assert re.search(r"modelUnwanted = true;[\s\S]*void generationSettled\.then\(\(\) => model\.dispose\?\.\(\)\);", release)
    raw = _js_function(qwen, "loadQwenOCRRuntimeRaw")
    assert re.search(r"loadedModel = loaded;\s*if \(modelUnwanted\) \{\s*releaseModelIfUnused\(\);", raw), (
        "a model that lands after the page gave up is kept"
    )
    assert re.search(r"if \(processorLoad\.status === \"rejected\"\) \{\s*releaseModelIfUnused\(\);\s*throw processorLoad\.reason;", raw), (
        "a model whose processor failed is stranded"
    )
    read = _js_function(qwen, "recognizeWithQwenWebGPU")
    assert "generationSettled = generation.then(" in read
    # An inference failure switches the model off AND releases it.
    assert re.search(r"qwenRuntimeDisabled = true;\s*(//[^\n]*\s*)*releaseModelIfUnused\(\);\s*return \"\";", read), (
        "an inference failure keeps the disabled model's sessions in GPU memory"
    )
    read = _js_function(qwen, "recognizeWithQwenWebGPU")
    assert "const stop = new runtime.StoppingSwitch();" in read and "runningGeneration = stop;" in read
    assert "stopping_criteria: stop," in read
    assert re.search(r"\} finally \{\s*runningGeneration = null;\s*\}", read)
    after_generate = read[read.index("runningGeneration = null;") :]
    assert re.search(r"if \(qwenRuntimeDisabled\) \{\s*return \"\";", after_generate), "a stopped generation's output is used as a reading"
    assert "InterruptableStoppingCriteria: new () => StoppingSwitch;" in qwen


def test_the_remove_control_deletes_exactly_the_library_bucket():
    src = _scrub_source()
    assert 'const ON_DEVICE_MODEL_CACHE = "transformers-cache";' in src
    assert "caches.delete(ON_DEVICE_MODEL_CACHE)" in src
    assert "caches.has(ON_DEVICE_MODEL_CACHE)" in src, "the control shows without checking the cache exists"
    html = _scrub_template()
    start = html.index('id="advanced_ocr_section"')
    section = html[start : html.index("{% endif %}", start)]
    button = re.search(r'<button[^>]*id="advanced_ocr_remove_model"[^>]*>', section)
    assert button is not None, "the remove control is not inside the offered section"
    assert re.search(r"\bhidden\b", button.group(0)), "the remove control is visible before the model exists"
    assert 'id="advanced_ocr_status"' in section


def test_the_runtime_ships_with_the_build_and_both_loads_are_awaited():
    """The library's ONNX runtime defaulted to a public CDN: executable code
    and a 27 MB wasm from a third party on every cold load (review)."""
    webpack = (JS / "webpack.config.js").read_text()
    assert "require.resolve('onnxruntime-web', { paths: [transformersDir] })" in webpack, (
        "the runtime is not resolved the way the library resolves it"
    )
    # One build, the asyncify one: the only one with WebGPU support. Safari,
    # where the library would pick the plain build, is not offered the option.
    assert "['ort-wasm-simd-threaded.asyncify'].flatMap(" in webpack
    assert "'ort-wasm-simd-threaded'" not in webpack, "the plain build is shipped again; it cannot do WebGPU"
    assert "to: `vendor/onnxruntime-web/${build}.js`, info: { minimized: true }" in webpack, (
        "the runtime module is not published as .js, or is run through the minimizer, which cannot parse it"
    )
    assert "to: `vendor/onnxruntime-web/${build}.wasm`" in webpack
    assert "...ortRuntimeAssets," in webpack
    assert not re.search(r"to:\s*[`']vendor/[^`']*\.mjs[`']", webpack), "a vendor module is published as .mjs"
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    assert 'const ORT_RUNTIME_URL = "/static/js/dist/vendor/onnxruntime-web/";' in qwen
    assert "transformers.env.backends.onnx.wasm.wasmPaths = ortRuntimePaths();" in qwen
    assert 'const ORT_RUNTIME_BUILD = "ort-wasm-simd-threaded.asyncify";' in qwen
    paths = _js_function(qwen, "ortRuntimePaths")
    assert "${ORT_RUNTIME_URL}${ORT_RUNTIME_BUILD}.js" in paths and "${ORT_RUNTIME_URL}${ORT_RUNTIME_BUILD}.wasm" in paths
    detect = _js_function(qwen, "detectWebGPUAvailability")
    assert re.search(r"if \(isSafari\(\)\) \{\s*return \{ available: false, reason:", detect), (
        "Safari is offered a runtime build that cannot do WebGPU"
    )
    assert "Promise.all(" not in qwen and "Promise.allSettled([" in qwen, (
        "a failed load leaves the other download running, refilling the cache after the pass ended"
    )


def test_the_library_ships_with_the_build_and_loads_from_this_origin():
    webpack = (JS / "webpack.config.js").read_text()
    assert re.search(
        r"from:\s*'node_modules/@huggingface/transformers/dist/transformers\.min\.js',\s*to:\s*'vendor/transformers/transformers\.min\.js'",
        webpack,
    ), "the build no longer ships the library's module build"
    qwen = (JS / "qwen_webgpu_ocr.ts").read_text()
    assert 'TRANSFORMERS_MODULE_URL = "/static/js/dist/vendor/transformers/transformers.min.js"' in qwen
    assert "cdn." not in qwen, "the module is loaded from a third party again"
    assert re.search(r"import\(\s*/\* webpackIgnore: true \*/\s*TRANSFORMERS_MODULE_URL\s*\)", qwen), (
        "the library is bundled again; the runtime inside it cannot locate its files that way"
    )


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
    """The wasm path must be derived, never pinned.

    A hardcoded ``onnxruntime-web@1.22.0`` shipped for months while webpack
    bundled 1.24.2, so the runtime asked for ``asyncify`` files that 1.22.0's
    dist does not contain and the fetch 404'd. Any literal version pin here is
    the same bug waiting to happen, because the two versions drift
    independently. Strings, nested templates included, are kept in this view.
    """
    # What is forbidden is ASSIGNING wasmPaths. A read-only diagnostic of
    # env.backends.onnx.wasm.wasmPaths is fine, and so is a string mentioning
    # it; the check runs on the string-blanked view and looks for the
    # identifier followed by a single `=` (review).
    assert not re.search(r"\bwasmPaths\s*=(?!=)", _qwen_structural()), (
        "wasmPaths is being set again; let transformers.js derive it from the "
        "onnxruntime build it actually shipped with"
    )
    assert not re.search(
        r"onnxruntime-web@\d", _qwen_executable()
    ), "a hardcoded onnxruntime-web version is back in qwen_webgpu_ocr.ts"


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
    assert "{% endif %}" in html[start : html.index("</div>", html.index("</label>", start)) + 60]
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


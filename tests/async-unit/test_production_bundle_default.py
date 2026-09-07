"""The webpack build must be a production build unless a developer opts out.

For as long as the config tested ``NODE_ENV === 'production'``, nothing set it:
not ``npm run build``, not any of the three build scripts, not CI. So every
build, including the one collected into the deployed image, was a development
bundle: unminified, about three times the size, and skipping the optimization
block whose Terser ``pure_funcs`` strip ``console.log/info/debug`` as a defense
against logging PHI to the browser console.

The default is now production; ``npm run build:dev`` opts out. These pin both
halves so the safe build stays the one you get by accident.
"""

import json
import pathlib
import re

JS = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "js"


def _webpack_config() -> str:
    return (JS / "webpack.config.js").read_text()


def _scripts() -> dict:
    return json.loads((JS / "package.json").read_text())["scripts"]


def test_production_is_the_default_mode():
    """Production unless NODE_ENV=development, with a CLI --mode taking precedence.

    The CLI clause matters: `webpack --mode X` overrides the configured mode
    after the config has run, so a flag derived only from NODE_ENV could
    disagree with the mode actually built (review).
    """
    src = _webpack_config()
    assert re.search(
        r"const\s+isProduction\s*=\s*argv\s*&&\s*argv\.mode\s*\?\s*argv\.mode\s*===\s*['\"]production['\"]"
        r"\s*:\s*process\.env\.NODE_ENV\s*!==\s*['\"]development['\"]\s*;",
        src,
        re.S,
    ), "isProduction is no longer: CLI --mode if given, else production unless NODE_ENV=development"
    assert not re.search(
        r"isProduction\s*=\s*process\.env\.NODE_ENV\s*===\s*['\"]production['\"]",
        src,
    ), "the opt-in production test is back; nothing in the repo sets NODE_ENV=production"


def test_developers_can_still_opt_out():
    scripts = _scripts()
    assert "build:dev" in scripts, "npm run build:dev is gone"
    assert "NODE_ENV=development" in scripts["build:dev"], scripts["build:dev"]


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


def test_mode_and_optimization_are_wired_to_the_flag():
    """A correct flag that nothing consumes is no fix (review).

    A hardcoded mode, or an optimization block kept as text but no longer
    conditional on the flag, would ship development output again with the
    default test above still green.
    """
    src = _webpack_config()
    assert re.search(
        r"mode\s*:\s*isProduction\s*\?\s*['\"]production['\"]\s*:\s*['\"]development['\"]",
        src,
    ), "webpack mode is no longer selected by isProduction"
    assert re.search(r"optimization\s*:\s*isProduction\s*\?\s*\{", src), (
        "the optimization block is no longer conditional on isProduction"
    )


def test_console_stripping_covers_exactly_the_chatty_levels():
    """The PHI defense is the reason production mode matters; keep it intact.

    Exactly log/info/debug: warn and error stay, on purpose, so production
    issues remain debuggable. The list must live inside the optimization
    block, where it is actually consumed.
    """
    src = _webpack_config()
    opt = re.search(r"optimization\s*:\s*isProduction\s*\?\s*\{", src)
    assert opt is not None
    block = _brace_block(src, opt.end() - 1)
    m = re.search(r"pure_funcs\s*:\s*\[([^\]]*)\]", block)
    assert m is not None, "the Terser pure_funcs list is gone from the optimization block"
    listed = set(re.findall(r"['\"]([\w.]+)['\"]", m.group(1)))
    assert listed == {"console.log", "console.info", "console.debug"}, listed


def test_build_marks_its_mode_and_the_cache_checks_it():
    """build_static.sh must not reuse a dist built in a different mode.

    webpack removes dist/BUILD_MODE before every compilation and writes it
    back, with the real mode, only after an error-free emit, so an
    interrupted or failed build leaves no marker. build_static.sh wants
    production unless NODE_ENV=development, and skips only when the marker
    says so (review). Regexes tolerate wrapping and quote style; they pin
    the values and the wiring, not the formatting.
    """
    src = _webpack_config()
    q = r"['\"]"
    assert re.search(
        r"path\.resolve\(\s*__dirname\s*,\s*" + q + r"dist" + q + r"\s*,\s*" + q + r"BUILD_MODE" + q + r"\s*\)",
        src,
        re.S,
    ), "the marker is no longer dist/BUILD_MODE"
    assert re.search(r"beforeCompile\.tap\(\s*" + q + r"BuildModeMarker", src, re.S) and re.search(
        r"unlinkSync\(\s*marker\s*\)", src
    ), "the marker is no longer removed before compilation"
    assert re.search(
        r"afterEmit\.tap\(\s*" + q + r"BuildModeMarker.*?compilation\.errors\.length\s*>\s*0\s*\)\s*return",
        src,
        re.S,
    ), "the marker is written even when the compilation had errors"
    # The marker must come from the compiler's EFFECTIVE mode, not from the
    # isProduction flag: `webpack --mode development` overrides the configured
    # mode after the config has run (review). A hardcoded value, or one
    # derived from the flag, would let a development build pass as production.
    assert re.search(
        r"compiler\.options\.mode\s*===\s*" + q + r"production" + q
        + r"\s*\?\s*" + q + r"production" + q + r"\s*:\s*" + q + r"development" + q,
        src,
        re.S,
    ), "the marker no longer records the compiler's effective mode"
    assert re.search(r"writeFileSync\(\s*marker\s*,\s*mode\s*\+", src, re.S), (
        "the marker is no longer written from the effective mode"
    )

    sh = (JS.parents[2] / "scripts" / "build_static.sh").read_text()
    assert re.search(r"^\s*EXPECTED_BUILD_MODE=production\s*$", sh, re.M), (
        "build_static.sh no longer expects production by default"
    )
    assert re.search(
        r"\[\s*\"\$\{NODE_ENV:-\}\"\s*=\s*\"development\"\s*\].*?EXPECTED_BUILD_MODE=development",
        sh,
        re.S,
    ), "build_static.sh no longer expects development only when NODE_ENV=development"
    assert re.search(r"BUILT_MODE=\$\(cat\s+\"\$\{JS_PATH\}/dist/BUILD_MODE\"", sh), (
        "build_static.sh no longer reads dist/BUILD_MODE"
    )
    assert re.search(
        r"\"\$CURRENT_JS_CHECKSUM\"\s*=\s*\"\$STORED_JS_CHECKSUM\".*?\"\$BUILT_MODE\"\s*=\s*\"\$EXPECTED_BUILD_MODE\"",
        sh,
        re.S,
    ), "the cache skip no longer requires the built mode to match"

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
    src = _webpack_config()
    assert re.search(
        r"const\s+isProduction\s*=\s*process\.env\.NODE_ENV\s*!==\s*['\"]development['\"]\s*;",
        src,
    ), "isProduction no longer defaults to true; the deployed bundle is a dev build again"
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
    """build_static.sh must not reuse a development dist under a matching checksum.

    webpack writes dist/BUILD_MODE after emit; the cache skip requires it to
    name the mode this run wants (review).
    """
    src = _webpack_config()
    assert "BuildModeMarker" in src and "'dist', 'BUILD_MODE'" in src, (
        "webpack no longer records the build mode in dist/BUILD_MODE"
    )
    sh = (JS.parents[2] / "scripts" / "build_static.sh").read_text()
    assert 'BUILT_MODE=$(cat "${JS_PATH}/dist/BUILD_MODE"' in sh, (
        "build_static.sh no longer reads dist/BUILD_MODE"
    )
    assert re.search(
        r'\[ "\$CURRENT_JS_CHECKSUM" = "\$STORED_JS_CHECKSUM" \].*\[ "\$BUILT_MODE" = "\$EXPECTED_BUILD_MODE" \]',
        sh,
    ), "the cache skip no longer requires the built mode to match"

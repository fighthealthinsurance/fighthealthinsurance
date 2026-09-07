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


def test_cache_key_covers_mode_and_the_bundles_actually_in_dist():
    """build_static.sh must not reuse bundles it did not produce.

    Any intervening build (`build:dev`, a CLI `--mode` or
    `--no-optimization-minimize` override, an interrupted run) rewrites
    bundles under an unchanged source checksum, and a marker written by
    webpack is only a claim by whoever ran webpack. So the stored key is
    source checksum + wanted mode + a fingerprint of dist/*.bundle.js, saved
    after this script's own successful build, and the skip requires the
    whole key to match (review). Regexes tolerate wrapping; they pin the
    shape and the wiring, not the formatting.
    """
    sh = (JS.parents[2] / "scripts" / "build_static.sh").read_text()
    assert re.search(r"^\s*EXPECTED_BUILD_MODE=production\s*$", sh, re.M), (
        "build_static.sh no longer expects production by default"
    )
    assert re.search(
        r"\[\s*\"\$\{NODE_ENV:-\}\"\s*=\s*\"development\"\s*\].*?EXPECTED_BUILD_MODE=development",
        sh,
        re.S,
    ), "build_static.sh no longer expects development only when NODE_ENV=development"
    fp = re.search(r"dist_fingerprint\(\)\s*\{(.*?)\n\s*\}", sh, re.S)
    assert fp is not None, "dist_fingerprint is gone from build_static.sh"
    body = fp.group(1)
    assert re.search(r"find\s+\"\$\{JS_PATH\}/dist\".*?-type\s+f.*?md5sum", body, re.S), (
        "the dist fingerprint no longer hashes the files in dist/"
    )
    # Recursive and unfiltered: workers/, the wasm, .mjs and .map files ship
    # too, and a missing worker with untouched bundles broke PDF uploads
    # while the narrower fingerprint still matched (review).
    assert "-maxdepth" not in body and "-name" not in body, (
        "the dist fingerprint is restricted again; it must cover every file "
        "under dist/, recursively"
    )
    assert re.search(
        r"CURRENT_BUILD_KEY=\"\$\{CURRENT_JS_CHECKSUM\}:\$\{EXPECTED_BUILD_MODE\}:\$\(dist_fingerprint\)\"",
        sh,
    ), "the cache key no longer combines sources, mode and the dist fingerprint"
    assert re.search(
        r"\[\s*\"\$CURRENT_BUILD_KEY\"\s*=\s*\"\$STORED_JS_CHECKSUM\"\s*\].*?SKIP_JS_BUILD=true",
        sh,
        re.S,
    ), "the skip no longer requires the whole key to match"
    assert re.search(
        r"echo\s+\"\$\{CURRENT_JS_CHECKSUM\}:\$\{EXPECTED_BUILD_MODE\}:\$\(dist_fingerprint\)\"\s*>\s*\"\$JS_CHECKSUM_FILE\"",
        sh,
    ), "the saved key no longer records the mode and fingerprint of the build just made"
    src = _webpack_config()
    assert "BUILD_MODE" not in src, (
        "a webpack-written build-mode marker is back; it is only a claim by "
        "whoever ran webpack, and the cache must validate output instead"
    )

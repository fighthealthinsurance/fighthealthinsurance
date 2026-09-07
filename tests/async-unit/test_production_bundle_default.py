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


def test_console_stripping_still_covers_the_chatty_levels():
    """The PHI defense is the reason production mode matters; keep it intact."""
    src = _webpack_config()
    m = re.search(r"pure_funcs\s*:\s*\[([^\]]*)\]", src)
    assert m is not None, "the Terser pure_funcs list is gone"
    listed = set(re.findall(r"['\"]([\w.]+)['\"]", m.group(1)))
    assert {"console.log", "console.info", "console.debug"} <= listed, listed

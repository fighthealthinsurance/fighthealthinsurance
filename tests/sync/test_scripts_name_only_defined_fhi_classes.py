"""Every fhi- class a script writes is drawn by a rule in the site stylesheets.

The blog index and the blog and FAQ posts are React, so their markup lives in
blog.tsx and blog_post.tsx rather than in a template, and the checks that read
templates never see it. Two of the rules the posts lean on, .fhi-breadcrumbs
and .fhi-notice-danger, came over as copies of the same rules on other
branches. A copy that does not come along when a branch is rebuilt leaves the
breadcrumb a numbered list and the error message unboxed, and every other test
still passes. This reads each class name the scripts write, the way the
Bootstrap ratchet reads them, and checks each fhi- one against the classes
custom.css and main.css style.
"""

from pathlib import Path

from tests.sync.test_bootstrap_ratchet import (
    _script_classes,
    class_names_in_stylesheet,
)

APP = Path(__file__).resolve().parents[2] / "fighthealthinsurance"
CSS = APP / "static" / "css"
SCRIPTS = APP / "static" / "js"


def _scripts():
    for path in sorted(SCRIPTS.rglob("*.ts*")):
        if path.suffix not in (".ts", ".tsx"):
            continue
        if {"node_modules", "dist"} & set(path.relative_to(SCRIPTS).parts):
            continue
        yield path


def test_every_fhi_class_a_script_writes_is_defined() -> None:
    defined: "set[str]" = set()
    for name in ("custom.css", "main.css"):
        defined |= class_names_in_stylesheet((CSS / name).read_text())
    missing: "dict[str, set[str]]" = {}
    for path in _scripts():
        for name in _script_classes(path.read_text(errors="replace")):
            if name.startswith("fhi-") and name not in defined:
                missing.setdefault(name, set()).add(path.name)
    assert not missing, (
        "these classes are written by a script but no stylesheet styles them, "
        "so the part of the page that uses them draws without its layout: %s"
        % {name: sorted(files) for name, files in sorted(missing.items())}
    )


def test_the_reading_sees_the_blog_post_classes() -> None:
    """So a clean run means the classes are defined, not that none were read."""
    found = set(_script_classes((SCRIPTS / "blog_post.tsx").read_text()))
    assert {"fhi-page", "fhi-breadcrumbs", "fhi-notice-danger"} <= found, found

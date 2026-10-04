"""Every fhi- class a template uses is defined somewhere.

The site's own layout pieces replace Bootstrap's classes one page at a time.
A template can name one of them while the rule that draws it is missing,
for instance after a branch is rebuilt and a hand-added rule does not come
along, and nothing else notices: the page renders, the other tests pass,
and the layout quietly falls apart (the state index lost its abbreviation
rows that way). This reads every class="..." in the templates and checks
that each fhi- class appears as a selector in the site stylesheets or in a
template's own <style> block.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"
STYLESHEETS = [
    REPO_ROOT / "fighthealthinsurance" / "static" / "css" / "custom.css",
    REPO_ROOT / "fighthealthinsurance" / "static" / "css" / "main.css",
]

# Classes that are hooks rather than styles: each needs no rule of its own.
HOOKS = {
    # The word "Menu" inside the header's menu button. The button styles it;
    # the class names the span for anyone reading the markup.
    "fhi-nav-toggle-word",
}

CLASS_ATTR = re.compile(r'class="([^"]*)"')
STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S)


def _classes_used() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text()
        for attr in CLASS_ATTR.findall(text):
            # Template tags inside a class attribute are not classes.
            attr = re.sub(r"\{[%{].*?[%}]\}", " ", attr)
            for name in attr.split():
                if name.startswith("fhi-"):
                    used.setdefault(name, set()).add(path.name)
    return used


def _all_css() -> str:
    css = "\n".join(p.read_text() for p in STYLESHEETS)
    for path in TEMPLATES.rglob("*.html"):
        css += "\n".join(STYLE_BLOCK.findall(path.read_text()))
    # A class named only in a comment, or only inside :not(...), is not drawn
    # by anything, so neither counts as its definition.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    while re.search(r":not\([^()]*\)", css):
        css = re.sub(r":not\([^()]*\)", "", css)
    return css


def test_every_fhi_class_a_template_uses_is_defined() -> None:
    css = _all_css()
    missing = {
        name: sorted(pages)
        for name, pages in _classes_used().items()
        if name not in HOOKS
        and not re.search(r"\." + re.escape(name) + r"(?![\w-])", css)
    }
    assert not missing, (
        "these classes are used in templates but no stylesheet defines them, "
        f"so the pages that use them lose that layout: {missing}"
    )


def test_every_hook_is_still_used() -> None:
    used = _classes_used()
    stale = sorted(name for name in HOOKS if name not in used)
    assert not stale, f"no template uses these hooks any more; remove them: {stale}"

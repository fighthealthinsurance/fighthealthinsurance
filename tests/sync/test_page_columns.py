"""Every page that has moved onto a page column says so in its source.

The browser guard in tests/selenium/test_selenium_page_widths.py measures
the column on the pages a plain GET reaches. The rest of the delete flow is
reached only by a token or a POST, and the Bootstrap ratchet cannot see
which of the site's own wrappers a page is on: swapping one of them to the
wide tier changes no Bootstrap count. So the tier each moved page opens on
is named here, and read straight from the template, the way the ratchet
reads its counts.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "fighthealthinsurance" / "templates"
CUSTOM_CSS = REPO_ROOT / "fighthealthinsurance" / "static" / "css" / "custom.css"


def declared(body: str, prop: str) -> str:
    """The last value ``prop`` is given in a rule body, or an empty string."""
    found = re.findall(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body)
    return found[-1].strip() if found else ""

# Template -> the class the page's first wrapper carries. Grows as pages move.
ON_A_COLUMN = {
    "about_us.html": "fhi-page-wide",
    "other_resources.html": "fhi-page-wide",
    "how_to_help.html": "fhi-page-wide",
    "media_references.html": "fhi-page-wide",
    "microsite_directory.html": "fhi-page-wide",
    "about_ai.html": "fhi-page",
    "faq.html": "fhi-page",
    "contact.html": "fhi-page",
    "privacy_policy.html": "fhi-page",
    "tos.html": "fhi-page",
    "mhmda.html": "fhi-page",
    "glossary.html": "fhi-page",
    "remove_data.html": "fhi-page fhi-page-centred",
    "confirm_delete.html": "fhi-page fhi-page-centred",
    "removed_data.html": "fhi-page fhi-page-centred",
    "delete_data_email_sent.html": "fhi-page fhi-page-centred",
    "share_denial.html": "fhi-page fhi-page-centred",
}

# Hero pages -> the class every band under the hero opens on. The first
# wrapper on these pages is inside the hero, so the page is read band by
# band instead: each full-width section after the hero holds its own column,
# which is how a band keeps its background running edge to edge.
BANDS_ON_A_COLUMN = {
    "state_help_index.html": "fhi-page-wide",
    "state_help.html": "fhi-page-wide",
    "glossary_index.html": "fhi-page-wide",
}

FIRST_WRAPPER = re.compile(r"{%\s*block content\s*%}.*?<div class=\"([^\"]+)\"", re.S)
HERO_END = re.compile(r"<section\b[^>]*\bclass=\"slider\"[^>]*>.*?</section>", re.S)
# A band's opening tag and the first element inside it, which must be the
# column rather than a Bootstrap container or a bare row.
BAND = re.compile(r"<section\b([^>]*)>\s*<(\w+)([^>]*)>", re.S)
CLASS = re.compile(r"\bclass=\"([^\"]*)\"")


def test_each_moved_page_opens_on_the_column_it_is_named_for() -> None:
    wrong = []
    for name, expected in ON_A_COLUMN.items():
        match = FIRST_WRAPPER.search((TEMPLATES / name).read_text())
        found = match.group(1) if match else None
        if found != expected:
            wrong.append(f"{name}: opens on {found!r}, not {expected!r}")
    assert not wrong, "\n".join(wrong)


def test_each_band_under_a_hero_opens_on_its_column() -> None:
    wrong = []
    for name, expected in BANDS_ON_A_COLUMN.items():
        text = (TEMPLATES / name).read_text()
        hero = HERO_END.search(text)
        if hero is None:
            wrong.append(f"{name}: no hero section, so it belongs in ON_A_COLUMN")
            continue
        bands = BAND.findall(text, hero.end())
        if not bands:
            wrong.append(f"{name}: no band follows the hero")
        for attributes, tag, inner in bands:
            band = CLASS.search(attributes)
            label = re.search(r"\bid=\"([^\"]+)\"", attributes)
            label = label.group(1) if label else (band.group(1) if band else "a band")
            found = CLASS.search(inner)
            found = found.group(1) if found and tag == "div" else None
            if found != expected:
                wrong.append(f"{name} #{label}: opens on {found!r}, not {expected!r}")
    assert not wrong, "\n".join(wrong)


def test_a_moved_page_keeps_no_bootstrap_container() -> None:
    kept = [
        name
        for name in [*ON_A_COLUMN, *BANDS_ON_A_COLUMN]
        if re.search(r"class=\"[^\"]*\bcontainer(-fluid)?\b", (TEMPLATES / name).read_text())
    ]
    assert not kept, f"back on a Bootstrap container: {kept}"


def test_both_tiers_exist_and_read_the_width_tokens() -> None:
    css = re.sub(r"/\*.*?\*/", "", CUSTOM_CSS.read_text(), flags=re.S)
    rules = dict(re.findall(r"([^{}]+)\{([^{}]*)\}", css))
    shared = rules[next(k for k in rules if k.strip() == ".fhi-page,\n.fhi-page-wide")]
    assert "var(--fhi-page-reading)" in declared(shared, "max-width")
    wide = rules[next(k for k in rules if k.strip() == ".fhi-page-wide")]
    assert "var(--fhi-page-wide)" in declared(wide, "max-width")
    # Both give way to the window with a gutter, rather than running to it.
    for body in (shared, wide):
        assert "var(--fhi-page-gutter)" in declared(body, "max-width")
    root = rules[next(k for k in rules if k.strip() == ":root")]
    for token in ("--fhi-page-reading", "--fhi-page-wide", "--fhi-page-gutter", "--fhi-measure"):
        assert f"{token}:" in root, f"{token} is not defined in :root"

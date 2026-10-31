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
    "ai_assistants.html": "fhi-page-wide",
    "denial_watch.html": "fhi-page-wide dw",
    "open_enrollment.html": "fhi-page-wide dw",
    "coverage_costs.html": "fhi-page-wide dw",
    "media_references.html": "fhi-page-wide",
    "microsite_directory.html": "fhi-page-wide",
    "fax_thankyou.html": "fhi-page-wide",
    "about_ai.html": "fhi-page-wide",
    "faxfollowup.html": "fhi-page",
    "thankyou.html": "fhi-page",
    "followup_thankyou.html": "fhi-page",
    "fax_followup_thankyou.html": "fhi-page",
    "professional_available.html": "fhi-page",
    "professional_thankyou.html": "fhi-page",
    "server_side_ocr.html": "fhi-page",
    "server_side_ocr_error.html": "fhi-page",
    "faq.html": "fhi-page",
    "contact.html": "fhi-page",
    "privacy_policy.html": "fhi-page",
    "tos.html": "fhi-page",
    "mhmda.html": "fhi-page",
    "glossary.html": "fhi-page",
    "followup.html": "fhi-page",
    "share_plan_documents.html": "fhi-page fhi-page-reading",
    "remove_data.html": "fhi-page fhi-page-centred",
    "pwyw_thanks.html": "fhi-page fhi-page-centred",
    "confirm_delete.html": "fhi-page fhi-page-centred",
    "removed_data.html": "fhi-page fhi-page-centred",
    "delete_data_email_sent.html": "fhi-page fhi-page-centred",
    "share_denial.html": "fhi-page fhi-page-centred",
    "unsubscribed.html": "fhi-page fhi-page-centred",
    "stripe_finish_error.html": "fhi-page fhi-page-centred",
    "fax_payment_cancelled.html": "fhi-page fhi-page-centred",
}

# Hero pages -> the class every band under the hero opens on. The first
# wrapper on these pages is inside the hero, so the page is read band by
# band instead: each full-width section after the hero holds its own column,
# which is how a band keeps its background running edge to edge. A page
# built from bands opens each one on .fhi-column, which reads the same
# width tokens as the page tiers and shares the wide tier's heading scale.
BANDS_ON_A_COLUMN = {
    "state_help_index.html": "fhi-column",
    "state_help.html": "fhi-column",
    "glossary_index.html": "fhi-column",
    "medicaid_eligibility.html": "fhi-column",
    "turning_26.html": "fhi-column",
    "patient_access.html": "fhi-column",
    "microsite.html": "fhi-column",
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
            # the first class names the column; the rest are how it is laid out
            found = found.group(1).split()[0] if found and tag == "div" else None
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


def _rules() -> dict[str, str]:
    """custom.css as selector -> every body it is given, in order, comments
    gone, so declared() reads the value the cascade ends on."""
    css = re.sub(r"/\*.*?\*/", "", CUSTOM_CSS.read_text(), flags=re.S)
    rules: dict[str, str] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        rules[selector.strip()] = rules.get(selector.strip(), "") + ";" + body
    return rules


def _token(root: str, name: str) -> str:
    found = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", root)
    assert found, f"{name} is not defined in :root"
    return found.group(1).strip()


def test_both_tiers_exist_and_read_the_width_tokens() -> None:
    rules = _rules()
    reading = declared(rules[".fhi-page"], "max-width")
    wide = declared(rules[".fhi-page-wide"], "max-width")
    assert "var(--fhi-measure)" in reading
    assert "var(--fhi-page-wide)" in wide
    # Both give way to the window with a gutter, rather than running to it.
    for value in (reading, wide):
        assert "var(--fhi-page-gutter)" in value
    root = rules[":root"]
    for token in ("--fhi-page-reading", "--fhi-page-wide", "--fhi-page-gutter", "--fhi-measure"):
        _token(root, token)


def test_the_reading_column_is_one_line_of_its_prose_wide() -> None:
    """The column is the measure, read at the size its running text is set
    at, plus its edge padding. A ch on the column is a zero at the column's
    own 1rem; the prose is --fhi-text-reading, so the measure is scaled by
    their ratio, and that number has to move if either size does."""
    rules = _rules()
    reading = declared(rules[".fhi-page"], "max-width")
    found = re.search(
        r"calc\(var\(--fhi-measure\)\s*\*\s*([\d.]+)\s*\+\s*2\s*\*\s*var\(--fhi-space-4\)\)",
        reading,
    )
    assert found, f".fhi-page is not the measure plus its padding: {reading}"
    root = rules[":root"]
    prose = float(_token(root, "--fhi-text-reading").removesuffix("rem"))
    body = float(_token(root, "--fhi-text-body").removesuffix("rem"))
    assert float(found.group(1)) == prose / body
    # The column's padding is the edge padding the factor adds back.
    assert declared(rules[".fhi-page,\n.fhi-page-wide"], "padding").split()[1] == "var(--fhi-space-4)"
    assert "--fhi-page-reading" not in reading


def test_running_text_in_the_reading_column_has_no_cap_of_its_own() -> None:
    """With a second cap on p and li, the text stopped about 200px short of
    the column while headings, pictures and boxes ran on to its edge."""
    css = re.sub(r"/\*.*?\*/", "", CUSTOM_CSS.read_text(), flags=re.S)
    capped = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if declared(body, "max-width")
        and any(
            re.match(r"\.fhi-page\s.*(\bp\b|\bli\b)", part.strip())
            for part in selector.split(",")
        )
    ]
    assert not capped, f"text in .fhi-page has its own width again: {capped}"


def test_the_intake_flow_and_the_bands_keep_the_reading_tier() -> None:
    """Only .fhi-page moved to the measure. The intake flow's wrapper and a
    hero page's bands still hold the 760px reading width."""
    rules = _rules()
    for wrapper in (".container-narrow", ".fhi-column"):
        widths = re.findall(r"max-width\s*:\s*([^;]+)", rules[wrapper])
        # The first is the desktop width; a phone's own rule lifts it later.
        assert "var(--fhi-page-reading)" in widths[0], (wrapper, widths)


def test_the_patient_access_hero_is_as_narrow_as_the_other_explainers() -> None:
    """Its subcopy ran one 218-character line across the full 1100px panel;
    Explain Denial, Understand Policy and State Help hold theirs to 800px."""
    for name, hero in (
        ("patient_access.html", "#patient-access-hero"),
        ("explain_denial.html", "#explain-denial-hero"),
    ):
        text = (TEMPLATES / name).read_text()
        rule = re.search(rf"{re.escape(hero)} \.hero-inner\s*\{{([^}}]*)\}}", text)
        assert rule, f"{name} does not cap its hero panel"
        assert declared(rule.group(1), "max-width") == "800px", name

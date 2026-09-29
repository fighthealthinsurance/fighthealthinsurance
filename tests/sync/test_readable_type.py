"""The reading typography: which face, what size, and which links underline.

Running text is set in Atkinson Hyperlegible Next, drawn so easily confused
letters look different; headings and anything shaped like a button keep the
brand face, Poppins. Prose in the reading column is a step larger than the
16px floor. A link inside running text is underlined, because the site's
links are near-black and colour alone never marked them.
"""

import pathlib
import re

from django.test import SimpleTestCase

ROOT = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
MAIN_CSS = (ROOT / "static" / "css" / "main.css").read_text()
CUSTOM_CSS = (ROOT / "static" / "css" / "custom.css").read_text()
BASE_HTML = (ROOT / "templates" / "base.html").read_text()


def rule(css: str, selector_pattern: str) -> tuple[str, str]:
    """The selector and body of the one rule whose selector matches."""
    found = [
        (sel.strip(), body)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(selector_pattern, sel)
    ]
    assert len(found) == 1, f"{len(found)} rules match {selector_pattern!r}"
    return found[0]


class ReadableTypeTest(SimpleTestCase):
    def test_the_body_face_is_requested_from_the_head(self):
        head = BASE_HTML[: BASE_HTML.index("</head>")]
        self.assertIn("family=Atkinson+Hyperlegible+Next", head)

    def test_running_text_is_set_in_the_reading_face(self):
        _sel, body = rule(MAIN_CSS, r"(^|\n)\s*body\s*$")
        family = re.search(r"font-family:\s*([^;]+);", body).group(1)
        self.assertTrue(family.strip().startswith("'Atkinson Hyperlegible Next'"))

    def test_headings_and_every_button_shape_keep_poppins(self):
        sel, body = rule(MAIN_CSS, r"h1, h2, h3")
        self.assertIn("'Poppins'", body)
        for shape in (
            "button",
            'input[type="submit"]',
            '[class*="btn"]',
            '[class*="-button"]',
            ".fhi-nav-cta a",
        ):
            self.assertIn(shape, sel)

    def test_the_reading_size_leaves_sized_and_card_text_alone(self):
        sel, body = rule(CUSTOM_CSS, r"\.fhi-page :where\(p, li\):not")
        self.assertIn("var(--fhi-text-reading)", body)
        for kept in (".lead", ".small", ".breadcrumb-item", '[class*="card"] *'):
            self.assertIn(kept, sel)

    def test_links_in_running_text_and_consent_labels_underline(self):
        sel, body = rule(CUSTOM_CSS, r"main :where\(p, li")
        self.assertIn("text-decoration: underline", body)
        self.assertIn(".form-check-label", sel)
        for component in ('[class*="btn"]', '[class*="card"]', '[class*="nav"]'):
            self.assertIn(component, sel)

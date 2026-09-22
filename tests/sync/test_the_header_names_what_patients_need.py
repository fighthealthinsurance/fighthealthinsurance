"""The header, after nine links became six.

The nav asked a person in the middle of a denial to choose between Chat,
Explain Denial, Understand Policy, About Our AI, How to Help, Resources/Blogs,
Remove Your Data, Professional and Generate Appeal. On a phone that is the
whole first screen before any of the page.

It is six now: Explain Denial, Explain Policy, Resources, Delete Data,
Professional, and Generate Appeal as the one highlighted thing. Chat became a
button in the corner on every page. About Our AI moved to the footer, which is
why one of these tests checks the footer rather than the header: removing it
from the nav without putting it anywhere would have lost the page.

The dropdowns are <details>, so nothing here needs JavaScript to open.
"""

import re

from django.test import SimpleTestCase, TestCase
from django.urls import reverse


def _nav(html: str) -> str:
    """Just the header, so a footer link cannot satisfy a nav assertion."""
    start = html.index('<details class="fhi-nav"')
    return html[start : html.index("</details>", html.index("fhi-nav-cta"))]


class TheNavIsSixThingsTest(TestCase):
    def setUp(self):
        self.html = self.client.get(reverse("root")).content.decode()

    def test_the_six_are_there_in_order(self):
        nav = _nav(self.html)
        wanted = [
            "Explain Denial",
            "Explain Policy",
            "Resources",
            "Delete Data",
            "Professional",
            "Generate Appeal",
        ]
        found = [w for w in wanted if w in nav]
        self.assertEqual(found, wanted, "a nav item is missing")

        positions = [nav.index(w) for w in wanted]
        self.assertEqual(
            positions, sorted(positions), "the nav is not in the agreed order"
        )

    def test_chat_and_about_our_ai_left_the_nav(self):
        nav = _nav(self.html)

        self.assertNotIn("Chat", nav, "Chat is still in the header")
        self.assertNotIn("About Our AI", nav)
        self.assertNotIn("How to Help", nav, "How to help belongs under Resources")

    def test_generate_appeal_is_the_last_and_the_highlighted_one(self):
        nav = _nav(self.html)

        self.assertIn("fhi-nav-cta", nav)
        self.assertGreater(
            nav.index("Generate Appeal"),
            nav.index("Professional"),
            "Generate Appeal should sit rightmost",
        )

    def test_resources_holds_the_three_it_was_given(self):
        nav = _nav(self.html)

        for label, route in (
            ("Guides", "other-resources"),
            ("Blog", "blog"),
            ("How to help", "how-to-help"),
        ):
            self.assertIn(label, nav)
            self.assertIn(reverse(route), nav)


class NothingInTheHeaderNeedsJavaScriptTest(TestCase):
    """The reported defect: Resources "doesn't always work when you click it".

    Everything that opens in this header is a <details>, so it opens with the
    CDN blocked, with JavaScript off, and before any script has run.
    """

    def test_no_bootstrap_toggles_remain_in_the_header(self):
        html = self.client.get(reverse("root")).content.decode()
        nav = _nav(html)

        self.assertNotIn("data-bs-toggle", nav)
        self.assertNotIn("dropdown-toggle", nav)
        self.assertNotIn("navbar-collapse", nav)

    def test_the_menu_is_open_in_the_markup_and_closed_on_phones_by_a_script(self):
        """A closed <details> renders nothing whatever CSS says, and desktop
        hides the toggle, so `open` has to be in the markup for a desktop
        to have a nav at all. The four lines that close it on phones are
        inline, not a library: blocked, the menu is open, not missing."""
        html = self.client.get("/").content.decode()
        self.assertIn('<details class="fhi-nav" id="navbar" open>', html)
        self.assertIn("menu.open = false", html)
        self.assertIn("(min-width: 992px)", html)

    def test_the_menu_and_both_dropdowns_are_details(self):
        html = self.client.get(reverse("root")).content.decode()
        nav = _nav(html)

        self.assertIn('<details class="fhi-nav"', html)
        self.assertEqual(
            nav.count('<details class="fhi-nav-group">'),
            2,
            "Resources and Professional should both be native disclosures",
        )


class ChatAndAboutOurAiStillHaveDoorsTest(TestCase):
    def test_chat_is_a_button_on_every_page(self):
        for route in ("root", "scan", "about"):
            with self.subTest(page=route):
                html = self.client.get(reverse(route)).content.decode()

                self.assertIn("fhi-chat-button", html)
                self.assertIn(reverse("chat"), html)

    def test_about_our_ai_is_in_the_footer(self):
        html = self.client.get(reverse("root")).content.decode()
        footer = html[html.index("<footer") :]

        self.assertIn("About Our AI", footer)
        self.assertIn(reverse("about-ai"), footer)


class TheStyleIsOursTest(SimpleTestCase):
    def test_the_nav_and_chat_button_are_styled_from_the_tokens(self):
        from pathlib import Path

        from django.conf import settings

        css = (
            Path(settings.BASE_DIR)
            / "fighthealthinsurance"
            / "static"
            / "css"
            / "custom.css"
        ).read_text()

        for selector in (".fhi-nav-list", ".fhi-nav-cta a", ".fhi-chat-button"):
            self.assertIn(selector, css, f"{selector} is not styled")

        # Tap targets, the same 44px rule the rest of the nav follows.
        block = css[css.index("/* ---- The header") :]
        self.assertGreaterEqual(
            len(re.findall(r"min-height:\s*44px", block)),
            4,
            "the nav rows and the chat button need a 44px minimum",
        )

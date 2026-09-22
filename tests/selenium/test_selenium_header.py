"""The header, in a real browser, at both widths.

Two things this has to establish and neither can be read off the CSS:
whether a closed <details> can be shown by author CSS on desktop, and
whether the menu and its dropdowns open with no JavaScript at all, which is
what the reported "Resources doesn't always work" was about.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

DESKTOP = (1440, 900)
PHONE = (390, 844)

NAV_WORDS = (
    "Explain Denial",
    "Explain Policy",
    "Resources",
    "Delete Data",
    "Professional",
    "Generate Appeal",
)


class SeleniumTestHeader(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _visible_nav_words(self):
        return self.execute_script(
            """
            const nav = document.querySelector('.fhi-nav-list');
            if (!nav) { return ['NO NAV']; }
            return Array.from(nav.querySelectorAll('a, summary'))
                .filter(el => el.getClientRects().length > 0)
                .map(el => el.textContent.trim());
            """
        )

    def test_desktop_shows_every_nav_item_without_opening_anything(self):
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        # Deliberately nothing clicked first.
        visible = " ".join(self._visible_nav_words())
        for word in NAV_WORDS:
            assert word in visible, (
                f"{word} is not visible on a desktop without opening the menu. "
                f"Visible: {visible}"
            )

    def test_the_phone_menu_opens_with_no_javascript(self):
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        opened = self.execute_script(
            """
            const menu = document.querySelector('details.fhi-nav');
            if (!menu) { return 'NO MENU'; }
            const before = menu.open;
            menu.querySelector('summary').click();
            return [before, menu.open];
            """
        )
        assert opened[0] is False, "the phone menu starts open"
        assert opened[1] is True, "clicking the toggle did not open the menu"

    def test_resources_opens_and_its_links_are_reachable(self):
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        result = self.execute_script(
            """
            const groups = document.querySelectorAll('details.fhi-nav-group');
            let resources = null;
            groups.forEach(g => {
                if (g.querySelector('summary').textContent.includes('Resources')) {
                    resources = g;
                }
            });
            if (!resources) { return 'NO RESOURCES'; }
            resources.querySelector('summary').click();
            const links = Array.from(resources.querySelectorAll('a'))
                .filter(a => a.getClientRects().length > 0)
                .map(a => a.textContent.trim());
            return [resources.open, links];
            """
        )
        assert result[0] is True, "Resources did not open"
        for label in ("Guides", "Blog", "How to help"):
            assert label in result[1], f"{label} not reachable: {result[1]}"

    def test_the_chat_button_is_there_and_goes_to_chat(self):
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        self.assert_element("#fhi-chat-button")
        href = self.get_attribute("#fhi-chat-button", "href")
        assert "/chat" in href, href

    def test_neither_width_scrolls_sideways(self):
        for size in (DESKTOP, PHONE):
            with self.subTest(size=size):
                self.set_window_size(*size)
                self.open(f"{self.live_server_url}/")
                self.wait_for_ready_state_complete()
                widths = self.execute_script(
                    "return [document.documentElement.scrollWidth, window.innerWidth];"
                )
                assert widths[0] <= widths[1], widths

    def test_every_header_link_is_actually_tappable_on_a_phone(self):
        """The reported bug: the open menu painted under the hero.

        Asserting the link exists is not enough, because it existed before
        and taps still landed on the hero heading behind it. This asks the
        browser what is actually at each link's centre.
        """
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        covered = self.execute_script(
            """
            // Everything a person would have open after tapping through:
            // the menu itself and both dropdowns. A closed <details> still
            // reports rects for its children in this browser, so checking
            // them while shut measures nothing real.
            document.querySelectorAll('details.fhi-nav, details.fhi-nav-group')
                .forEach(d => { d.open = true; });
            const items = Array.from(
                document.querySelectorAll('.fhi-nav-list a, .fhi-nav-list summary')
            );
            const covered = [];
            items.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) { return; }
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                if (y < 0 || y > window.innerHeight) { return; }
                const hit = document.elementFromPoint(x, y);
                if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) {
                    covered.push([el.textContent.trim(), hit ? hit.tagName : 'none']);
                }
            });
            return covered;
            """
        )
        assert covered == [], f"something is painted over these header items: {covered}"

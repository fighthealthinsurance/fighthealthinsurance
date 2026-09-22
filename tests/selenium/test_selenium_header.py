"""The header, in a real browser, at both widths.

Two things this has to establish and neither can be read off the CSS.
First, that the desktop actually shows the menu: a closed <details> renders
none of its content whatever author CSS says, and the desktop hides the
toggle, so the markup carries `open` and a short inline script closes it on
phones. Second, that the menu and its dropdowns open by tapping, with no
library involved, which is what the reported "Resources doesn't always work"
was about.
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
        return self.execute_script("""
            const nav = document.querySelector('.fhi-nav-list');
            if (!nav) { return ['NO NAV']; }
            return Array.from(nav.querySelectorAll('a, summary'))
                // checkVisibility, not getClientRects: a closed <details>
                // keeps stale rects for its children, so rects say
                // 'visible' about things nobody can see.
                .filter(el => el.checkVisibility())
                .map(el => el.textContent.trim());
            """)

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

    def test_the_phone_menu_opens_by_tapping_menu(self):
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        opened = self.execute_script("""
            const menu = document.querySelector('details.fhi-nav');
            if (!menu) { return 'NO MENU'; }
            const before = menu.open;
            menu.querySelector('summary').click();
            return [before, menu.open];
            """)
        assert opened[0] is False, "the phone menu starts open"
        assert opened[1] is True, "clicking the toggle did not open the menu"

    def test_resources_opens_and_its_links_are_reachable(self):
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        result = self.execute_script("""
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
                .filter(a => a.checkVisibility())
                .map(a => a.textContent.trim());
            return [resources.open, links];
            """)
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

    def _phone_menu_state(self):
        return self.execute_script("""
            const menu = document.querySelector('details.fhi-nav');
            if (!menu) { return {open: 'NO MENU', shown: [], toggle: false}; }
            const items = Array.from(
                document.querySelectorAll('.fhi-nav-list a, .fhi-nav-list summary')
            );
            return {
                open: menu.open,
                shown: items.filter(el => el.checkVisibility())
                            .map(el => el.textContent.trim()),
                toggle: document.querySelector('.fhi-nav-toggle').checkVisibility(),
            };
            """)

    def test_the_phone_menu_starts_closed_and_takes_no_room(self):
        """Nothing but the Menu toggle is on screen until it is tapped.

        A review asked whether display:flex on the list could show it
        through a closed <details>. It cannot, a closed <details> renders no
        content whatever author CSS says, so this guards the two things that
        would actually change the answer: the markup stopping being a
        <details>, and the inline script that closes it on phones going
        missing, which would leave the open-in-markup menu open.
        """
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        state = self._phone_menu_state()
        assert state["open"] is False, "the phone menu starts open"
        assert state["shown"] == [], f"visible before Menu is tapped: {state['shown']}"
        assert state["toggle"] is True, "the Menu toggle is not visible on a phone"

    def test_turning_a_tablet_closes_the_menu_and_turning_it_back_opens_it(self):
        """The inline script follows the 992px breakpoint in both directions."""
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()
        assert self._phone_menu_state()["open"] is True, "desktop menu not open"

        self.set_window_size(*PHONE)
        self.wait_for_ready_state_complete()
        narrow = self._phone_menu_state()
        assert narrow["open"] is False, "menu stayed open after narrowing"
        assert narrow["toggle"] is True, "the Menu toggle is not visible"

        self.set_window_size(*DESKTOP)
        self.wait_for_ready_state_complete()
        assert (
            self._phone_menu_state()["open"] is True
        ), "menu stayed closed after widening"

    def test_with_scripts_blocked_the_phone_menu_is_open_not_missing(self):
        """The failure mode without JavaScript is a tall header, not no nav.

        The header this replaced needed bootstrap.bundle.min.js from a CDN;
        with that blocked the toggle did nothing and no link was reachable.
        Now the markup is open and only the closing on phones is scripted,
        so with every script blocked each link is on screen and tappable.
        """
        self.set_window_size(*PHONE)
        self.driver.execute_cdp_cmd(
            "Emulation.setScriptExecutionDisabled", {"value": True}
        )
        try:
            self.open(f"{self.live_server_url}/")
            state = self._phone_menu_state()
        finally:
            self.driver.execute_cdp_cmd(
                "Emulation.setScriptExecutionDisabled", {"value": False}
            )
        assert state["open"] is True, "with scripts blocked the menu is closed"
        shown = " ".join(state["shown"])
        for word in NAV_WORDS:
            assert (
                word in shown
            ), f"{word} is not on screen with scripts blocked: {shown}"

    def test_every_header_link_is_actually_tappable_on_a_phone(self):
        """The reported bug: the open menu painted under the hero.

        Asserting the link exists is not enough, because it existed before
        and taps still landed on the hero heading behind it. This asks the
        browser what is actually at each link's centre.
        """
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        covered = self.execute_script("""
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
            """)
        assert covered == [], f"something is painted over these header items: {covered}"

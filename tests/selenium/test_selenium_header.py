"""The header, in a real browser, at both widths.

Two things this has to establish and neither can be read off the CSS.
First, that the desktop actually shows the menu: a closed <details> renders
none of its content whatever author CSS says, and the desktop hides the
toggle, so the markup carries `open` and a short inline script closes it on
phones. Second, that the menu and its dropdowns open by tapping, with no
library involved, which is what the reported "Resources doesn't always work"
was about.
"""

import time

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

DESKTOP = (1440, 900)
PHONE = (390, 844)

NAV_WORDS = (
    "About",
    "Explain Denial/Policy",
    "Resources",
    "Delete",
    "Help",
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

    def test_the_open_phone_menu_pushes_the_page_down(self):
        """The open menu takes its own room instead of covering the page.
        The old sticky plugin wrapped the header in a div frozen at the
        closed height, so the list that opened out of it hung over whatever
        came next. position: sticky leaves the header in the flow, so what
        follows it moves down."""
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()

        edges = self.execute_script("""
            const menu = document.querySelector('details.fhi-nav');
            if (!menu) { return 'NO MENU'; }
            menu.querySelector('summary').click();
            const header = document.querySelector('.navbar-default');
            // Anything wrapped around the header counts as the header, so
            // the next thing on the page is measured from outside it.
            let outer = header;
            while (outer.parentElement && outer.parentElement !== document.body) {
                outer = outer.parentElement;
            }
            let next = outer.nextElementSibling;
            while (next && next.getBoundingClientRect().height === 0) {
                next = next.nextElementSibling;
            }
            return {
                open: menu.open,
                headerBottom: header.getBoundingClientRect().bottom,
                next: next ? next.tagName.toLowerCase() + '#' + next.id : 'nothing',
                nextTop: next ? next.getBoundingClientRect().top : null,
            };
            """)
        assert edges != "NO MENU", "no menu on the page"
        assert edges["open"] is True, "tapping Menu did not open it"
        assert edges["nextTop"] is not None, "nothing follows the header"
        assert edges["nextTop"] >= edges["headerBottom"] - 1, (
            f"the open menu ends at {edges['headerBottom']:.0f}px but "
            f"{edges['next']} starts at {edges['nextTop']:.0f}px, under it"
        )

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
        for label in ("Guides", "Blog"):
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

    def _menu_once_it_is(self, open_: bool, timeout: float = 5.0):
        """The state, once the breakpoint has caught up with a resize.

        The matchMedia change callback that opens or closes the menu runs
        after the resize, not with it, so reading straight away could see
        the old state and fail for no reason.
        """
        deadline = time.monotonic() + timeout
        while True:
            state = self._phone_menu_state()
            if state["open"] is open_ or time.monotonic() > deadline:
                return state
            time.sleep(0.05)

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
        assert self._menu_once_it_is(True)["open"] is True, "desktop menu not open"

        self.set_window_size(*PHONE)
        narrow = self._menu_once_it_is(False)
        assert narrow["open"] is False, "menu stayed open after narrowing"
        assert narrow["toggle"] is True, "the Menu toggle is not visible"

        self.set_window_size(*DESKTOP)
        assert (
            self._menu_once_it_is(True)["open"] is True
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
            // the menu itself, then each dropdown in turn, since the three
            // groups share a name and the browser keeps one open. A closed
            // <details> still reports rects for its children in this
            // browser, so checking them while shut measures nothing real.
            document.querySelector('details.fhi-nav').open = true;
            const groups = Array.from(document.querySelectorAll('details.fhi-nav-group'));
            const covered = [];
            const check = el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) { return; }
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                if (y < 0 || y > window.innerHeight) { return; }
                const hit = document.elementFromPoint(x, y);
                if (!hit || !(el === hit || el.contains(hit) || hit.contains(el))) {
                    covered.push([el.textContent.trim(), hit ? hit.tagName : 'none']);
                }
            };
            groups.forEach(g => { g.open = false; });
            document.querySelectorAll('.fhi-nav-list > li > a, .fhi-nav-list > li > details > summary')
                .forEach(check);
            groups.forEach(g => {
                g.open = true;
                g.querySelectorAll('a').forEach(check);
                g.open = false;
            });
            return covered;
            """)
        assert covered == [], f"something is painted over these header items: {covered}"


class SeleniumTestDropdownLinksGoSomewhere(FHISeleniumBase, StaticLiveServerTestCase):
    """The report: "Guides doesn't work as a link". A link inside an open
    desktop dropdown has to be the thing under the pointer and has to take
    the visitor to its page when the pointer clicks it; a hero's stacking
    context, a sticky header, or a second dropdown painted over the first
    could put something else on top without any test noticing. These use
    WebDriver's own pointer clicks, not a script's .click(), so whatever is
    on top gets the click, and the check runs from the top of the page and
    after scrolling, when the header is sticky."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    UNDER_THE_POINTER = """
        const [group, label] = arguments;
        const details = Array.from(document.querySelectorAll('details.fhi-nav-group'))
            .find(d => d.querySelector('summary').textContent.includes(group));
        const link = Array.from(details.querySelectorAll('a'))
            .find(a => a.textContent.trim() === label);
        const r = link.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return {
            open: details.open,
            onTop: top === link || link.contains(top),
            covering: top ? top.tagName.toLowerCase() + '.' + top.className : 'nothing',
        };
    """

    def _pointer_click(self, xpath):
        """A pointer moved to the element's centre and clicked there. Not
        seleniumbase's click(), which falls back to a script click when a
        real one is intercepted and would hide exactly the defect this
        looks for: whatever is on top gets this click."""
        element = self.driver.find_element(By.XPATH, xpath)
        ActionChains(self.driver).move_to_element(element).click().perform()

    def _summary(self, group):
        return f"//details[contains(@class, 'fhi-nav-group')]/summary[contains(., '{group}')]"

    def _open_groups_once_settled(self, timeout: float = 2.0):
        """Which groups are open, once nothing is still closing. A <details>
        fires its toggle event asynchronously, so the script that closes
        the other groups runs a moment after the click; the native name=
        path closes them synchronously. Poll until the set holds still."""
        read = """
            return Array.from(document.querySelectorAll('details.fhi-nav-group[open] summary'))
                .map(s => s.textContent.trim());
        """
        deadline = time.time() + timeout
        last = self.execute_script(read)
        while time.time() < deadline:
            time.sleep(0.1)
            now = self.execute_script(read)
            if now == last and len(now) <= 1:
                return now
            last = now
        return last

    def _open_link(self, label):
        return f"//details[contains(@class, 'fhi-nav-group') and @open]//a[normalize-space() = '{label}']"

    def _dropdown_link_works(self, page, group, label, path, scroll=0):
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}{page}")
        self.wait_for_ready_state_complete()
        if scroll:
            self.execute_script("window.scrollTo(0, arguments[0])", scroll)
            # html has scroll-behavior: smooth, so the page glides there.
            time.sleep(0.5)
            header = self.execute_script(
                "return document.querySelector('details.fhi-nav').getBoundingClientRect().top"
            )
            assert 0 <= header < 200, (
                f"after scrolling {scroll}px the header is at {header:.0f}px; it is not sticky"
            )
        self._pointer_click(self._summary(group))
        found = self.execute_script(self.UNDER_THE_POINTER, group, label)
        assert found["open"], f"{page}: {group} did not open on a pointer click"
        assert found["onTop"], (
            f"{page}: {label} under {group} is covered by {found['covering']}"
        )
        self._pointer_click(self._open_link(label))
        self.wait_for_ready_state_complete()
        assert self.get_current_url().endswith(path), (
            f"{page}: clicking {label} led to {self.get_current_url()}"
        )

    def test_guides_works_as_a_link_from_the_home_page(self):
        self._dropdown_link_works("/", "Resources", "Guides", "/other-resources")

    def test_guides_works_as_a_link_from_a_hero_page(self):
        self._dropdown_link_works("/explain-denial", "Resources", "Guides", "/other-resources")

    def test_guides_works_as_a_link_once_the_header_is_sticky(self):
        self._dropdown_link_works("/", "Resources", "Guides", "/other-resources", scroll=400)

    def test_explain_policy_works_as_a_link(self):
        self._dropdown_link_works("/", "Explain Denial/Policy", "Explain Policy", "/understand-policy")

    def test_opening_one_dropdown_closes_the_other(self):
        """Two open at once overlapped, and Resources painted over the Explain
        links: the right half of "Explain Denial" answered to Guides. The
        groups share a name now, so the browser keeps one open."""
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/")
        self.wait_for_ready_state_complete()
        self._pointer_click(self._summary("Explain Denial/Policy"))
        self._pointer_click(self._summary("Resources"))
        open_groups = self._open_groups_once_settled()
        assert open_groups == ["Resources"], f"open at once: {open_groups}"
        # Browsers before late 2023 ignore name=; the inline script closes the
        # others by hand. Strip the attribute to prove it does.
        self.execute_script("""
            document.querySelectorAll('details.fhi-nav-group')
                .forEach(d => { d.removeAttribute('name'); d.open = false; });
        """)
        self._pointer_click(self._summary("Explain Denial/Policy"))
        self._pointer_click(self._summary("Professional"))
        open_groups = self._open_groups_once_settled()
        assert open_groups == ["Professional"], f"without name=, open at once: {open_groups}"
        self.execute_script("""
            document.querySelectorAll('details.fhi-nav-group').forEach(d => { d.open = false; });
        """)
        # And back: Explain reopens on its own, with nothing over its links.
        self._pointer_click(self._summary("Explain Denial/Policy"))
        found = self.execute_script(self.UNDER_THE_POINTER, "Explain Denial/Policy", "Explain Denial")
        assert found["open"] and found["onTop"], found

"""The questions that open in place, in a real browser.

The FAQ on every microsite and the list on Preparing for 2026 were Bootstrap
accordions, and opened only once Bootstrap's script had come in from a CDN.
They are <details> now, and the script is gone. What a sync test cannot
establish is that the browser really does the rest: that a question opens
its answer under a real pointer and from the keyboard, that the answer is
then on screen, that a group keeps one answer open with name= and without
it, and that none of it needs a script at all.
"""

import time

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium.webdriver import ActionChains
from selenium.webdriver.common.keys import Keys
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

PHONE = (390, 844)
DESKTOP = (1440, 900)

# The microsite FAQ is one template rendered on about 120 pages; one of
# them stands for the rest. Each page is a label, a URL name and its kwargs.
PAGES = (
    ("a microsite", "microsite", {"slug": "mri-denial"}),
    ("Preparing for 2026", "preparing-2026", {}),
)

STATE = """
    return Array.from(document.querySelectorAll('.fhi-accordion > details'))
        .map(d => ({
            open: d.open,
            // checkVisibility, not getClientRects: a closed <details> can
            // keep stale rects for what is inside it.
            shown: d.querySelector('.fhi-accordion-body').checkVisibility(),
            question: d.querySelector('summary').textContent.trim(),
        }));
"""


class SeleniumTestQuestionsOpenInPlace(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _load(self, url_name, kwargs, size=PHONE):
        self.set_window_size(*size)
        self.open(f"{self.live_server_url}{reverse(url_name, kwargs=kwargs)}")
        self.wait_for_ready_state_complete()

    def _summaries(self):
        return self.find_elements(".fhi-accordion > details > summary")

    def _pointer_click(self, element):
        """A real pointer on the row's centre, so whatever is on top of it
        gets the click. The page glides when it scrolls, so it is moved
        there at once first."""
        self.execute_script(
            "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
            element,
        )
        ActionChains(self.driver).move_to_element(element).click().perform()

    def _state_once_settled(self, timeout: float = 2.0):
        """The group, once nothing is still closing. A <details> fires its
        toggle event after the click, so the fallback that closes the other
        answers runs a moment later; native name= closes them at once."""
        deadline = time.time() + timeout
        last = self.execute_script(STATE)
        while time.time() < deadline:
            time.sleep(0.1)
            now = self.execute_script(STATE)
            if now == last and sum(item["open"] for item in now) <= 1:
                return now
            last = now
        return last

    def test_a_question_opens_its_answer_under_the_pointer(self):
        for label, url_name, kwargs in PAGES:
            with self.subTest(page=label):
                self._load(url_name, kwargs)
                before = self.execute_script(STATE)
                assert len(before) >= 3, f"{label}: only {len(before)} questions"
                assert before[1]["open"] is False, f"{label}: the second starts open"
                assert (
                    before[1]["shown"] is False
                ), f"{label}: the second answer shows while it is closed"
                self._pointer_click(self._summaries()[1])
                after = self._state_once_settled()
                assert after[1]["open"] is True, (
                    f"{label}: a pointer click on '{after[1]['question']}' did "
                    "not open it"
                )
                assert (
                    after[1]["shown"] is True
                ), f"{label}: the answer opened but is not on screen"

    def test_a_question_opens_from_the_keyboard_with_a_ring_on_it(self):
        for label, url_name, kwargs in PAGES:
            with self.subTest(page=label):
                self._load(url_name, kwargs)
                self.execute_script(
                    "document.querySelector('.fhi-accordion > details > summary')"
                    ".focus();"
                )
                ActionChains(self.driver).send_keys(Keys.TAB).perform()
                focused = self.execute_script("""
                    const active = document.activeElement;
                    const summaries = Array.from(
                        document.querySelectorAll('.fhi-accordion > details > summary'));
                    const style = getComputedStyle(active);
                    return {
                        index: summaries.indexOf(active),
                        ring: style.outlineStyle,
                        width: parseFloat(style.outlineWidth),
                    };
                    """)
                assert focused["index"] == 1, (
                    f"{label}: Tab from the first question went to "
                    f"{focused['index']}, not the second"
                )
                assert (
                    focused["ring"] != "none" and focused["width"] >= 2
                ), f"{label}: the focused question shows no ring: {focused}"

                ActionChains(self.driver).send_keys(Keys.ENTER).perform()
                opened = self._state_once_settled()
                assert (
                    opened[1]["open"] and opened[1]["shown"]
                ), f"{label}: Enter did not open the answer: {opened[1]}"

                ActionChains(self.driver).send_keys(Keys.SPACE).perform()
                closed = self._state_once_settled()
                assert (
                    closed[1]["open"] is False
                ), f"{label}: Space did not close the answer again"

    def test_opening_one_answer_closes_the_one_that_was_open(self):
        for label, url_name, kwargs in PAGES:
            with self.subTest(page=label):
                self._load(url_name, kwargs, DESKTOP)
                start = self.execute_script(STATE)
                assert [item["open"] for item in start][:2] == [
                    True,
                    False,
                ], f"{label}: the first answer should start open, alone: {start}"
                self._pointer_click(self._summaries()[1])
                now = self._state_once_settled()
                assert [item["open"] for item in now] == [
                    index == 1 for index in range(len(now))
                ], f"{label}: open at once: {now}"

                # Browsers before late 2023 ignore name=; the inline script
                # after </main> closes the others by hand. Strip the
                # attribute to prove it does.
                self.execute_script("""
                    document.querySelectorAll('.fhi-accordion > details')
                        .forEach(d => { d.removeAttribute('name'); d.open = false; });
                    """)
                self._pointer_click(self._summaries()[0])
                self._pointer_click(self._summaries()[2])
                now = self._state_once_settled()
                assert [item["open"] for item in now] == [
                    index == 2 for index in range(len(now))
                ], f"{label}: without name=, open at once: {now}"

    def test_with_scripts_blocked_a_question_still_opens(self):
        """A <details> is the browser's own. With every script on the page
        blocked, which is what a failed CDN used to amount to, a question
        still opens."""
        self.driver.execute_cdp_cmd(
            "Emulation.setScriptExecutionDisabled", {"value": True}
        )
        try:
            for label, url_name, kwargs in PAGES:
                with self.subTest(page=label):
                    self._load(url_name, kwargs)
                    self._pointer_click(self._summaries()[1])
                    state = self.execute_script(STATE)
                    assert (
                        state[1]["open"] and state[1]["shown"]
                    ), f"{label}: with scripts blocked the answer stayed shut"
        finally:
            self.driver.execute_cdp_cmd(
                "Emulation.setScriptExecutionDisabled", {"value": False}
            )

    def test_the_chevron_turns_and_holds_still_when_asked_to(self):
        chevron = """
            const details = document.querySelectorAll('.fhi-accordion > details')[1];
            const after = getComputedStyle(details.querySelector('summary'), '::after');
            return {transform: after.transform, duration: after.transitionDuration};
        """
        _, url_name, kwargs = PAGES[0]
        try:
            for motion, still in (("no-preference", False), ("reduce", True)):
                with self.subTest(motion=motion):
                    self.driver.execute_cdp_cmd(
                        "Emulation.setEmulatedMedia",
                        {
                            "features": [
                                {"name": "prefers-reduced-motion", "value": motion}
                            ]
                        },
                    )
                    self._load(url_name, kwargs)
                    closed = self.execute_script(chevron)
                    self._pointer_click(self._summaries()[1])
                    # Read once any turn has finished; mid-turn the chevron
                    # can still report where it started.
                    time.sleep(0.5)
                    opened = self.execute_script(chevron)
                    assert (
                        closed["transform"] != opened["transform"]
                    ), f"the chevron did not turn: {closed} then {opened}"
                    if still:
                        assert opened["duration"] == "0s", (
                            f"reduced motion asked for, and the chevron still "
                            f"animates: {opened}"
                        )
                    else:
                        assert (
                            opened["duration"] != "0s"
                        ), f"the chevron snaps even for motion: {opened}"
        finally:
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": []})

"""Selenium tests for long-message and weird-Unicode rendering in the chat UI.

Guards the frontend fixes that keep long pasted content, long unbroken strings,
and weird Unicode from breaking the chat layout (horizontal page overflow) or
crashing rendering.

Invisible / bidi characters are built with ``chr()`` so this source file
contains no hidden (or "Trojan Source") characters.
"""

import time

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

# A ZWJ family emoji + a bidi override span, built from code points so no
# literal invisible characters live in this source file.
_ZWJ = chr(0x200D)
_FAMILY_EMOJI = chr(0x1F468) + _ZWJ + chr(0x1F469) + _ZWJ + chr(0x1F467)
_BIDI_TEXT = chr(0x202E) + "reversed" + chr(0x202C)
# Allow a few pixels for sub-pixel rounding / scrollbars.
_OVERFLOW_TOLERANCE_PX = 5


class SeleniumChatLongMessageTest(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def wait_for_element(self, selector, timeout=10):
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def fill_consent_form(self):
        self.type("input#store_fname", "TestFirstName")
        self.type("input#store_lname", "TestLastName")
        self.type("input#email", "longmsg@example.com")
        self.type("input#store_street", "123 Test St")
        self.type("input#store_city", "TestCity")
        self.type("input#store_state", "CA")
        self.type("input#store_zip", "12345")
        self.click("input#tos")
        self.click("input#privacy")

    def _open_chat(self):
        self.open(f"{self.live_server_url}/chat-consent")
        self.fill_consent_form()
        self.click("button[type='submit']")
        self.wait_for_element("#chat-interface-root", timeout=15)
        self.wait_for_page_ready()
        # Wait for React to actually mount the input before any test logic
        # touches it (the root div exists before React renders into it).
        self.wait_for_element("textarea", timeout=15)
        time.sleep(1)  # Let React components settle

    def _horizontal_overflow(self):
        """Pixels of horizontal overflow on the document (0 means no overflow)."""
        return self.execute_script(
            "return document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth;"
        )

    def _set_textarea(self, value):
        """Set the React-controlled textarea value via its native setter."""
        self.wait_for_element("textarea", timeout=15)
        self.execute_script(
            """
            const textarea = document.querySelector('textarea');
            if (!textarea) { return; }
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value').set;
            setter.call(textarea, arguments[0]);
            textarea.dispatchEvent(new Event('input', { bubbles: true }));
            """,
            value,
        )

    def test_long_no_space_string_does_not_overflow(self):
        """A long unbroken string in the input must not widen the page."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        self._set_textarea("A" * 5000)
        time.sleep(0.5)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

    def test_long_pasted_message_bubble_does_not_overflow(self):
        """A long pasted message rendered in a bubble must not widen the page."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        long_text = "This claim was denied as not medically necessary. " * 80
        self._set_textarea(long_text)
        time.sleep(0.3)  # let React enable the send button after the input event
        self.wait_for_element("button[aria-label='Send message']", timeout=10)
        self.execute_script(
            "const b = document.querySelector('button[aria-label=\"Send message\"]');"
            " if (b) b.click();"
        )
        # The user message is echoed optimistically (no backend needed).
        self.wait_for_page_ready(
            predicate=lambda d: "not medically necessary"
            in d.execute_script("return document.body.innerText || ''")
        )
        time.sleep(0.5)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

    def test_unicode_message_renders_without_crashing(self):
        """Emoji / bidi / non-Latin input must not crash rendering or overflow."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        weird = "Patient café " + _FAMILY_EMOJI + " " + _BIDI_TEXT + " 你好 مرحبا"
        self._set_textarea(weird)
        time.sleep(0.5)
        # The textarea accepted the value and the page is still responsive.
        value = self.execute_script("return document.querySelector('textarea').value;")
        self.assertIn("café", value)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

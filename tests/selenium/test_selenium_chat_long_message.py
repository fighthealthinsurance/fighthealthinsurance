"""Selenium tests for the chat UI with long and weird-Unicode input.

These validate that the chat page mounts and stays functional (no React
ErrorBoundary crash) and that the page does not overflow horizontally when a
long unbroken string or weird Unicode (emoji ZWJ, bidi overrides, non-Latin)
is entered into the chat input.

Note: ``StaticLiveServerTestCase`` is WSGI and does not serve the Channels
WebSocket route (``/ws/ongoing-chat/`` 404s), so messages can't actually be
sent or rendered here. These tests therefore exercise input handling, page
layout, and crash-free rendering -- not sent-message bubbles. (This setup is
what surfaced the autosize ``maxHeight`` crash: a render-time exception trips
the ErrorBoundary so the textarea never mounts and ``_open_chat`` fails.)

Invisible / bidi characters are built with ``chr()`` so this source file
contains no hidden (or "Trojan Source") characters.
"""

import time

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

# A ZWJ family emoji + a bidi override span, built from code points so no
# literal invisible characters live in this source file.
_ZWJ = chr(0x200D)
_FAMILY_EMOJI = chr(0x1F468) + _ZWJ + chr(0x1F469) + _ZWJ + chr(0x1F467)
_BIDI_TEXT = chr(0x202E) + "reversed" + chr(0x202C)
# Allow a few pixels for sub-pixel rounding / scrollbars.
_OVERFLOW_TOLERANCE_PX = 5
# Generous element-wait timeout: the chat bundle is large and can render the
# input late on a cold browser cache.
_WAIT_TIMEOUT = 30
# How many times to (re)load the chat page waiting for React to mount the input.
_MAX_LOAD_ATTEMPTS = 2


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
        self.wait_for_element_present("#chat-interface-root", timeout=_WAIT_TIMEOUT)
        self.wait_for_page_ready()
        # React mounts the input asynchronously after the JS bundle loads; retry
        # with a refresh if it isn't visible on the first load. If a render-time
        # error tripped the ErrorBoundary, the textarea never appears and this
        # raises -- which is the crash signal we want to catch.
        for attempt in range(_MAX_LOAD_ATTEMPTS):
            try:
                self.wait_for_element_visible("textarea", timeout=_WAIT_TIMEOUT)
                break
            except Exception:
                if attempt == _MAX_LOAD_ATTEMPTS - 1:
                    raise
                self.refresh_page()
                self.wait_for_element_present(
                    "#chat-interface-root", timeout=_WAIT_TIMEOUT
                )
                self.wait_for_page_ready()
        time.sleep(1)  # Let React components settle

    def _horizontal_overflow(self):
        """Pixels of horizontal overflow on the document (0 means no overflow)."""
        return self.execute_script(
            "return document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth;"
        )

    def _set_textarea_via_js(self, value):
        """Set the React-controlled textarea value via its native setter.

        Used instead of send_keys for content that can't be typed reliably
        (non-BMP emoji) or cheaply (very long strings); React needs the native
        setter so its onChange fires.
        """
        self.wait_for_element_visible("textarea", timeout=_WAIT_TIMEOUT)
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

    def test_long_unbroken_input_does_not_overflow_page(self):
        """A long unbroken string in the input must render without crashing and
        without causing horizontal page overflow."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        self._set_textarea_via_js("A" * 5000)
        time.sleep(0.5)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

    def test_unicode_input_renders_without_crashing(self):
        """Emoji / bidi / non-Latin input must not crash rendering or overflow."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        weird = "Patient café " + _FAMILY_EMOJI + " " + _BIDI_TEXT + " 你好 مرحبا"
        self._set_textarea_via_js(weird)
        time.sleep(0.5)
        # The textarea accepted the value and the page is still responsive.
        value = self.execute_script("return document.querySelector('textarea').value;")
        self.assertIn("café", value)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

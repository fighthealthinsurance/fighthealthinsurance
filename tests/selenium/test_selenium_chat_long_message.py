"""Selenium tests for long-message and weird-Unicode rendering in the chat UI.

Guards the frontend fixes that keep long pasted content, long unbroken strings,
and weird Unicode from breaking the chat layout (horizontal page overflow),
collapsing very long messages, or crashing rendering.

These mirror the resilient interaction pattern of test_selenium_chat_status.py:
element finding/typing/clicking goes through seleniumbase's own methods (which
reliably locate the React-rendered textarea), and the native value-setter is
used only where send_keys can't help (non-BMP emoji and long pastes).

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
# Element-wait timeout. Kept short here so the diagnostic run fails fast and
# returns its dump quickly (the input has never mounted in CI regardless of how
# long we wait, so a long timeout only slows the run).
_WAIT_TIMEOUT = 15
# How many times to (re)load the chat page waiting for React to mount the input.
_MAX_LOAD_ATTEMPTS = 1


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
        # React mounts the input asynchronously after the large JS bundle loads;
        # on a cold/slow first load (this file sorts before chat_status, so it
        # often pays the cold-cache cost) it can miss the window. Retry with a
        # page refresh until the textarea actually renders.
        for attempt in range(_MAX_LOAD_ATTEMPTS):
            try:
                self.wait_for_element_visible("textarea", timeout=_WAIT_TIMEOUT)
                break
            except Exception:
                if attempt == _MAX_LOAD_ATTEMPTS - 1:
                    # TEMPORARY: capture why React never renders the chat input
                    # in this test class (works fine in chat_status) so we can
                    # diagnose the CI-only failure.
                    self._dump_diagnostics("textarea-not-found")
                    raise
                self.refresh_page()
                self.wait_for_element_present(
                    "#chat-interface-root", timeout=_WAIT_TIMEOUT
                )
                self.wait_for_page_ready()
        time.sleep(1)  # Let React components settle

    def _dump_diagnostics(self, label):
        """TEMPORARY: print page state to the CI log to diagnose why the chat
        input doesn't mount. Remove once understood."""

        def _safe(script):
            try:
                return self.execute_script(script)
            except Exception as exc:  # noqa: BLE001
                return f"<script error: {exc}>"

        info = {
            "url": _safe("return document.location.href;"),
            "title": _safe("return document.title;"),
            "ready_state": _safe("return document.readyState;"),
            "textarea_count": _safe(
                "return document.querySelectorAll('textarea').length;"
            ),
            "root_present": _safe(
                "return !!document.getElementById('chat-interface-root');"
            ),
            "root_html_len": _safe(
                "var r=document.getElementById('chat-interface-root');"
                "return r ? r.innerHTML.length : -1;"
            ),
            "script_tags": _safe(
                "return Array.from(document.scripts).map(s=>s.src).join(' | ');"
            ),
            "body_text": _safe("return (document.body.innerText||'').slice(0,2000);"),
            "root_html_head": _safe(
                "var r=document.getElementById('chat-interface-root');"
                "return r ? r.innerHTML.slice(0,1500) : '<no root>';"
            ),
        }
        try:
            logs = self.driver.get_log("browser")
            info["console"] = " || ".join(
                entry.get("message", "")[:600] for entry in logs[-30:]
            )
        except Exception as exc:  # noqa: BLE001
            info["console"] = f"<no browser logs: {exc}>"

        print(f"\n===== CHAT DIAGNOSTICS [{label}] =====")
        for key, value in info.items():
            print(f"--- {key}:\n{value}")
        print("===== END CHAT DIAGNOSTICS =====\n")

    def _horizontal_overflow(self):
        """Pixels of horizontal overflow on the document (0 means no overflow)."""
        return self.execute_script(
            "return document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth;"
        )

    def _set_textarea_via_js(self, value):
        """Set the React-controlled textarea value via its native setter.

        Required for content send_keys can't type reliably (non-BMP emoji) or
        cheaply (long pastes); React needs the native setter so onChange fires.
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

    def _send_current_input(self):
        self.wait_for_element_visible(
            "button[aria-label='Send message']", timeout=_WAIT_TIMEOUT
        )
        self.click("button[aria-label='Send message']")

    def test_long_no_space_string_in_bubble_does_not_overflow(self):
        """A long unbroken string, once rendered in a bubble, must not widen the
        page (exercises overflow-wrap/word-break on the message content)."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        long_token = "A" * 240
        self.type("textarea", long_token)
        self._send_current_input()
        # The user message is echoed optimistically (no backend needed).
        self.wait_for_text(long_token[:60], timeout=_WAIT_TIMEOUT)
        time.sleep(0.5)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

    def test_very_long_message_is_collapsed(self):
        """A very long pasted message renders collapsed behind a Show more
        toggle and does not widen the page."""
        self._open_chat()
        baseline = self._horizontal_overflow()
        long_text = "This claim was denied as not medically necessary. " * 120
        self._set_textarea_via_js(long_text)
        self._send_current_input()
        # Collapse toggle appears for very long content.
        self.wait_for_text("Show more", timeout=_WAIT_TIMEOUT)
        time.sleep(0.5)
        self.assertLessEqual(
            self._horizontal_overflow(), baseline + _OVERFLOW_TOLERANCE_PX
        )

    def test_unicode_message_renders_without_crashing(self):
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

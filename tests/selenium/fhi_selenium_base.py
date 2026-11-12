from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from seleniumbase import BaseCase

from django.test import TestCase

# Third-party hosts base.html loads on every page: the two Google Fonts
# stylesheets (and the font files they point at), Google Analytics, and
# Microsoft UET. The stylesheets are render-blocking and sit ahead of inline
# <head> scripts, so the parser stops until they arrive: when one stalled on
# a CI runner, a page the test had just submitted to (the chat after the
# consent or explain-denial form, most often) had no body yet when a 10-15s
# wait ran out. The tags also sent every test page view to the live
# analytics accounts. Resolving these names to nothing makes each request
# fail at once, so the page renders in the fallback face, every run the same.
BLOCKED_THIRD_PARTY_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "www.googletagmanager.com",
    "*.google-analytics.com",
    "analytics.google.com",
    "bat.bing.com",
)

TEST_BROWSER_ARGS = (
    # Keep Chrome from routing localhost through an HTTP proxy.
    "--no-proxy-server",
    "--host-resolver-rules="
    + ", ".join(f"MAP {host} ~NOTFOUND" for host in BLOCKED_THIRD_PARTY_HOSTS),
)


def configure_test_browser():
    """Add TEST_BROWSER_ARGS to the Chromium args seleniumbase launches with.

    Call it in setUp before BaseCase.setUp() reads sb_config. chromium_arg is
    set by the pytest plugin, as a comma-separated string; it is turned into
    a list here, because the host-resolver rule itself contains commas and
    seleniumbase splits a string on them.
    """
    import seleniumbase.config as sb_config

    existing = getattr(sb_config, "chromium_arg", None) or []
    if isinstance(existing, str):
        existing = [arg.strip() for arg in existing.split(",") if arg.strip()]
    args = list(existing)
    for arg in TEST_BROWSER_ARGS:
        if arg not in args:
            args.append(arg)
    sb_config.chromium_arg = args


class FHISeleniumBase(BaseCase, TestCase):
    def setUp(self):
        configure_test_browser()
        super().setUp()

    def assert_title_eventually(self, desired_title):
        try:
            WebDriverWait(self.driver, 15).until(EC.title_is(desired_title))
        except Exception:
            # On failure assert_title gives us a better error message than the timeout.
            self.assert_title(desired_title)

    def assert_text_eventually(self, expected_text, target):
        try:
            element_locator = (By.ID, target)
            WebDriverWait(self.driver, 15).until(
                lambda d: d.find_element(*element_locator).text == expected_text
            )
        except Exception as e:
            # On failure assert_text gives us a better error message than the timeout.
            self.assert_text(expected_text, target)

    def assert_text_eventually_contains(self, expected_text, target):
        element_locator = (By.ID, target)
        WebDriverWait(self.driver, 15).until(
            lambda d: expected_text in d.find_element(*element_locator).text
        )

    def continue_past_extraction(self):
        """The extraction page no longer moves on by itself.

        It reads the letter over a socket the test server does not serve, so
        the run ends at once in its terminal state, which offers a retry and
        a continue. A person presses Continue; so does this.
        """
        self.assert_title_eventually("Analyzing Your Denial")
        self.click("button#entity-continue", timeout=90)

    def click_button_eventually(self, target):
        element_locator = (By.ID, target)
        WebDriverWait(self.driver, 60).until(
            lambda d: d.find_element(*element_locator) != None
        )
        return self.click(f"button#{target}")

    def wait_for_page_ready(
        self, timeout=15, predicate=None, selector=None, localstorage_key=None
    ):
        """Wait for page JavaScript to complete loading.

        Args:
            timeout: Maximum wait time in seconds.
            predicate: Optional callable(driver) -> bool to poll after readyState.
            selector: Optional CSS selector; waits until the element has a non-empty value.
            localstorage_key: Optional localStorage key; waits until the key exists.
        """
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        if predicate is not None:
            WebDriverWait(self.driver, timeout).until(predicate)
        elif selector is not None:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_element(By.CSS_SELECTOR, selector).get_attribute(
                    "value"
                )
                not in (None, "")
            )
        elif localstorage_key is not None:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script(
                    f"return localStorage.getItem('{localstorage_key}')"
                )
                is not None
            )

    def wait_for_clickable(self, selector, timeout=15):
        """Wait for element to be clickable."""
        WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
        )

    # Reaching the chat from a form is two or three navigations (the form's
    # POST, the chat_redirect.html auto-submit, sometimes a redirect) before
    # the page and its ~900 KB bundle load: 10-15s was too tight a budget
    # for that on a loaded CI runner. Waiting longer costs nothing on a pass.
    CHAT_LOAD_TIMEOUT = 30

    def wait_for_chat_interface(self, timeout=CHAT_LOAD_TIMEOUT):
        """Wait for the chat page a form just handed off to."""
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#chat-interface-root"))
        )

    def wait_for_url_contains(self, text, timeout=15):
        """Wait until URL contains specific text."""
        WebDriverWait(self.driver, timeout).until(EC.url_contains(text))

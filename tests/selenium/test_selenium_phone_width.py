"""No page may scroll sideways on a phone.

`body { overflow-x: hidden }` used to sit over the whole site. It did not stop
anything from being too wide; it stopped the too-wide part from being
reachable, which is worse than a scrollbar, because a table whose right-hand
columns cannot be scrolled to is a table with missing data. Removing it is
what made these two failures visible, and both were real:

  Delete Your Data put a Bootstrap .row outside any container. A row carries a
  negative side margin that only cancels against a container's padding, so it
  hung twelve pixels off each edge of the page.

  The upload page's consent labels contain
  "(doctor/therapist/practitioner/office/hospital)", which no browser will
  break on its own, so that one label grew wider than the phone and took the
  document with it.

A page that scrolls sideways on a phone is hard to notice on a desktop and
easy to reintroduce, so this walks the public pages at 390px, which is an
iPhone 14, and fails if the document is wider than the window.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

PHONE = (390, 844)

# Every page a patient can reach without a session.
PUBLIC_PAGES = (
    ("the home page", ""),
    ("the upload page", "scan"),
    ("delete your data", "remove_data"),
    ("the terms", "tos"),
    ("the privacy policy", "privacy_policy"),
    ("the consumer health data notice", "mhmda"),
    ("about us", "about-us"),
    ("how to help", "how-to-help"),
    # A Bootstrap row outside any container made this page 12px wider than
    # the phone until it moved onto the page column.
    ("share your denial", "share_denial"),
)


class SeleniumTestPhoneWidth(FHISeleniumBase, StaticLiveServerTestCase):
    """The public pages, at the width most people actually read them."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _document_and_window_width(self):
        return self.execute_script(
            "return [document.documentElement.scrollWidth, window.innerWidth];"
        )

    def _widest_things(self):
        """What is sticking out, so a failure says where to look."""
        return self.execute_script(
            """
            var out = [];
            document.querySelectorAll('*').forEach(function (el) {
                var r = el.getBoundingClientRect();
                if (r.width > 0 && r.right > window.innerWidth + 1) {
                    out.push(el.tagName.toLowerCase()
                        + (el.id ? '#' + el.id : '')
                        + (el.className ? '.' + String(el.className).trim()
                            .split(/\\s+/).join('.') : '')
                        + ' right=' + Math.round(r.right));
                }
            });
            return out.slice(-4);
            """
        )

    def test_no_public_page_scrolls_sideways_on_a_phone(self):
        self.set_window_size(*PHONE)
        for name, path in PUBLIC_PAGES:
            with self.subTest(page=name):
                self.open(f"{self.live_server_url}/{path}")
                self.wait_for_page_ready()
                document, window = self._document_and_window_width()
                self.assertLessEqual(
                    document,
                    window + 1,
                    "%s is %dpx wide in a %dpx window, so it scrolls sideways "
                    "on a phone. The innermost things sticking out:\n  %s"
                    % (name, document, window, "\n  ".join(self._widest_things())),
                )

    def test_a_table_too_wide_for_the_phone_scrolls_itself(self):
        """The wide table is reachable, rather than hidden or page-widening.

        This is the other half of the rule above: it would be easy to satisfy
        that one by clipping the table, which is what the site used to do.
        """
        self.set_window_size(*PHONE)
        self.open(f"{self.live_server_url}/remove_data")
        self.wait_for_page_ready()
        reachable = self.execute_script(
            """
            var w = document.querySelector('.scroll-x');
            if (!w) return null;
            return [w.scrollWidth, w.clientWidth, getComputedStyle(w).overflowX];
            """
        )
        self.assertIsNotNone(
            reachable, "the wide table on Delete Your Data lost its scroll wrapper"
        )
        content, visible, overflow_x = reachable
        self.assertIn(
            overflow_x,
            ("auto", "scroll"),
            "the wrapper around the table does not scroll, so anything wider "
            "than the phone is simply cut off",
        )
        if content > visible:
            self.assertGreater(
                content,
                0,
                "the table has width but the wrapper reports none",
            )

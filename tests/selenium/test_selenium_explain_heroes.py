"""The two Explain heroes, in a real browser, at four widths.

The home page's slider band has a 650px floor in main.css. Explain My Denial
and Understand My Policy carry three lines of copy, so each page lowers the
floor to 380px in its own style block. That is a floor and not a cap, which
is the thing this has to establish: on a desktop the band stops at 380px
with the copy centred in it, and on a phone, where the copy wraps taller
than 380px, the band grows with the copy rather than cutting it off.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

DESKTOP = (1440, 900)
LAPTOP = (1280, 720)
PHONE = (390, 844)
SMALL_PHONE = (320, 568)

HERO_FLOOR = 380

PAGES = {
    "explain-denial": "explain-denial-hero",
    "understand-policy": "understand-policy-hero",
    "professionals/patient-access": "patient-access-hero",
    "microsite/biologic-denial": "microsite-hero",
}

MEASURE_JS = """
const hero = document.getElementById(arguments[0]);
const band = hero.querySelector('.item');
const copy = hero.querySelector('.hero-inner');
const bandRect = band.getBoundingClientRect();
const copyRect = copy.getBoundingClientRect();
// What the band adds around the copy when the copy is what sets the
// height: its own and the caption's vertical padding and borders, and
// the copy's vertical margins. Read from the styles, not from the band,
// so the expectation is independent of the thing it checks.
const v = (el, props) => props.reduce((n, p) => n + parseFloat(getComputedStyle(el)[p]), 0);
const caption = copy.parentElement;
const chrome = v(band, ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'])
  + v(caption, ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'])
  + v(copy, ['marginTop', 'marginBottom']);
return {
  chrome: chrome,
  band: bandRect.height,
  copy: copyRect.height,
  above: copyRect.top - bandRect.top,
  below: bandRect.bottom - copyRect.bottom,
  // scrollHeight past clientHeight is copy the band has cut off.
  clipped: band.scrollHeight > band.clientHeight + 1,
};
"""


class SeleniumTestExplainHeroes(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _measure(self, page, hero, size):
        self.set_window_size(*size)
        self.open(f"{self.live_server_url}/{page}")
        self.wait_for_ready_state_complete()
        return self.execute_script(MEASURE_JS, hero)

    def test_a_desktop_band_stops_at_the_floor_with_the_copy_centred(self):
        for page, hero in PAGES.items():
            for size in (DESKTOP, LAPTOP):
                with self.subTest(page=page, width=size[0]):
                    m = self._measure(page, hero, size)
                    # The floor when the copy plus what the band wraps around
                    # it is shorter; otherwise that sum, since it is a floor
                    # and not a cap. The sum is read from the styles, so a
                    # band back at 650px fails here.
                    expected = max(HERO_FLOOR, m["copy"] + m["chrome"])
                    assert abs(m["band"] - expected) <= 1, (
                        f"{page} at {size[0]}px: the band is {m['band']:.0f}px, "
                        f"not {expected:.0f}px. Copy is {m['copy']:.0f}px."
                    )
                    assert m["band"] >= HERO_FLOOR - 1, f"{page}: under the floor"
                    assert abs(m["above"] - m["below"]) <= 2, (
                        f"{page} at {size[0]}px: {m['above']:.0f}px above the "
                        f"copy and {m['below']:.0f}px below it."
                    )

    def test_a_phone_band_grows_with_the_copy_and_cuts_none_of_it_off(self):
        for page, hero in PAGES.items():
            for size in (PHONE, SMALL_PHONE):
                with self.subTest(page=page, width=size[0]):
                    m = self._measure(page, hero, size)
                    assert not m["clipped"], (
                        f"{page} at {size[0]}px: the band cuts off its copy."
                    )
                    assert m["band"] >= m["copy"] and m["below"] >= 0, (
                        f"{page} at {size[0]}px: the copy ({m['copy']:.0f}px) "
                        f"runs past the band ({m['band']:.0f}px)."
                    )
                    assert m["band"] >= HERO_FLOOR - 1, (
                        f"{page} at {size[0]}px: the band is {m['band']:.0f}px, "
                        f"under the {HERO_FLOOR}px floor."
                    )

    def test_the_patient_access_buttons_sit_side_by_side_on_a_desktop(self):
        """Two calls to action in one row, not one under the other."""
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/professionals/patient-access")
        self.wait_for_ready_state_complete()
        # Vertical centres, not tops: the two buttons are different heights
        # and sit centred in the row, so their tops differ by a few pixels
        # even when they share it, and a half-pixel shift of the whole page
        # could tip a rounded comparison. Two buttons in one row have centres
        # within a few pixels; stacked, they are a button's height apart.
        centres = self.execute_script("""
            return Array.from(document.querySelectorAll('#patient-access-hero .hero-cta-group a'))
                .map(a => { const r = a.getBoundingClientRect(); return r.top + r.height / 2; });
        """)
        assert len(centres) == 2, f"expected two buttons, found {centres}"
        assert abs(centres[0] - centres[1]) <= 4, (
            f"the buttons are on different rows: centres at {centres}"
        )

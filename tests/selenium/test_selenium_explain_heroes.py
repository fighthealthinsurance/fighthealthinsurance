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

HERO_FLOOR = 320

PAGES = {
    "explain-denial": "explain-denial-hero",
    "understand-policy": "understand-policy-hero",
    "professionals/patient-access": "patient-access-hero",
    "microsite/biologic-denial": "microsite-hero",
}

# The brand green, as Chromium reports #a5c422.
BRAND_GREEN = "rgb(165, 196, 34)"

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
  // Copy the band has cut off: any part of the copy box, or of the text
  // inside it (a headline half that stopped wrapping would poke out of
  // the box sideways), outside the band's box.
  clipped: (() => {
    const boxes = [copyRect, ...Array.from(copy.querySelectorAll('*'))
      .filter(el => el.getClientRects().length)
      .map(el => el.getBoundingClientRect())];
    return boxes.some(r => r.top < bandRect.top - 1 || r.bottom > bandRect.bottom + 1
      || r.left < bandRect.left - 1 || r.right > bandRect.right + 1);
  })(),
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

    def test_the_hero_buttons_sit_side_by_side_on_a_desktop(self):
        """Every call to action in one row, not one under the other: Patient
        Access and the sample page it links to."""
        # No microsite has a guide link today, so the row holds two buttons;
        # the medicare page is the variant where Chat is the primary.
        for page, hero in (
            ("professionals/patient-access", "patient-access-hero"),
            ("microsite/biologic-denial", "microsite-hero"),
            ("microsite/medicare-work-requirements-denial", "microsite-hero"),
        ):
          for size in (DESKTOP, LAPTOP):
            with self.subTest(page=page, width=size[0]):
                self.set_window_size(*size)
                self.open(f"{self.live_server_url}/{page}")
                self.wait_for_ready_state_complete()
                # Vertical centres, not tops: the buttons are different heights
                # and sit centred in the row, so their tops differ by a few
                # pixels even when they share it. In one row the centres are
                # within a few pixels; stacked, a button's height apart.
                centres = self.execute_script("""
                    return Array.from(document.querySelectorAll('#' + arguments[0] + ' .hero-cta-group a'))
                        .map(a => { const r = a.getBoundingClientRect(); return r.top + r.height / 2; });
                """, hero)
                assert len(centres) >= 2, f"{page}: expected at least two buttons, found {centres}"
                assert max(centres) - min(centres) <= 4, (
                    f"{page}: the buttons are on different rows: centres at {centres}"
                )

    def test_three_buttons_still_share_the_row(self):
        """A microsite with a guide link has three calls to action. None has
        all three today, so the third is added to the real row in the
        browser, the way the template would render it, and the row must
        still hold all three at both desktop widths."""
        for size in (DESKTOP, LAPTOP):
            with self.subTest(width=size[0]):
                self.set_window_size(*size)
                self.open(f"{self.live_server_url}/microsite/biologic-denial")
                self.wait_for_ready_state_complete()
                centres = self.execute_script("""
                    const row = document.querySelector('#microsite-hero .hero-cta-group');
                    const guide = document.createElement('a');
                    guide.href = '#'; guide.className = 'btn btn-outline-primary';
                    guide.innerHTML = '<i class="bi bi-book"></i> Read Our Guide';
                    row.prepend(guide);
                    return Array.from(row.querySelectorAll('a'))
                        .map(a => { const r = a.getBoundingClientRect(); return r.top + r.height / 2; });
                """)
                assert len(centres) == 3, f"{size[0]}px: expected three buttons, found {centres}"
                assert max(centres) - min(centres) <= 4, (
                    f"{size[0]}px: three buttons do not share the row: centres at {centres}"
                )

    def test_the_patient_access_headline_breaks_after_infrastructure(self):
        """"Appeal Infrastructure" on one line, "for Patient Access Teams" on
        the next, at both desktop widths: the second half moves down whole
        rather than leaving "Teams" alone on a third line."""
        for size in (DESKTOP, LAPTOP):
            with self.subTest(width=size[0]):
                self.set_window_size(*size)
                self.open(f"{self.live_server_url}/professionals/patient-access")
                self.wait_for_ready_state_complete()
                # Each word's line, from the rectangles of a range over it: a
                # word on the second line has a top one line-height down.
                lines = self.execute_script("""
                    const h1 = document.querySelector('#patient-access-hero .hero-headline');
                    const line = parseFloat(getComputedStyle(h1).lineHeight);
                    const top = h1.getBoundingClientRect().top;
                    const out = {};
                    const walker = document.createTreeWalker(h1, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const text = node.textContent;
                        const re = /\\S+/g; let m;
                        while ((m = re.exec(text))) {
                            const range = document.createRange();
                            range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
                            const r = range.getBoundingClientRect();
                            out[m[0]] = Math.round((r.top - top) / line);
                        }
                    }
                    return out;
                """)
                assert lines == {"Appeal": 0, "Infrastructure": 0, "for": 1,
                                 "Patient": 1, "Access": 1, "Teams": 1}, (
                    f"{size[0]}px: the headline's words fall on these lines: {lines}"
                )

    def test_every_short_hero_is_blurred_and_shares_one_tagline_colour(self):
        """The four short heroes carry the blur behind the copy, keep the copy
        above it, and give their taglines the one colour: the green the
        Explain pages had, which Patient Access and the sample page lacked."""
        colours = {}
        for page, hero in PAGES.items():
            with self.subTest(page=page):
                self.set_window_size(*DESKTOP)
                self.open(f"{self.live_server_url}/{page}")
                self.wait_for_ready_state_complete()
                m = self.execute_script("""
                    const band = document.querySelector('#' + arguments[0] + ' .item');
                    const before = getComputedStyle(band, '::before');
                    const copy = band.querySelector('.hero-inner');
                    const r = copy.getBoundingClientRect();
                    const onTop = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                    const radius = (before.filter.match(/blur\\(([\\d.]+)px\\)/) || [0, 0])[1];
                    return {
                        drawn: before.content !== 'none' && before.display !== 'none'
                            && before.visibility === 'visible' && parseFloat(before.opacity) > 0.9,
                        // The layer is the band's size: an inset that resolved to
                        // auto left a 0x0 box that still reported a blur.
                        layerWidth: parseFloat(before.width), layerHeight: parseFloat(before.height),
                        bandWidth: band.clientWidth, bandHeight: band.clientHeight,
                        clipped: getComputedStyle(band).overflow === 'hidden',
                        radius: parseFloat(radius),
                        image: before.backgroundImage !== 'none',
                        copyOnTop: copy.contains(onTop) || onTop === copy,
                        tagline: getComputedStyle(band.querySelector('.hero-tagline')).color,
                    };
                """, hero)
                assert m["drawn"], f"{page}: the blurred layer is not drawn"
                assert (
                    abs(m["layerWidth"] - m["bandWidth"]) <= 1
                    and abs(m["layerHeight"] - m["bandHeight"]) <= 1
                ), (
                    f"{page}: the blurred layer is {m['layerWidth']:.0f}x{m['layerHeight']:.0f}, "
                    f"the band {m['bandWidth']}x{m['bandHeight']}"
                )
                assert m["clipped"], f"{page}: the band does not clip the blur's edge"
                assert m["radius"] >= 2, f"{page}: the blur is {m['radius']}px, not a blur"
                assert m["image"], f"{page}: the blurred layer has no image"
                assert m["copyOnTop"], f"{page}: the blurred layer covers the copy"
                colours[page] = m["tagline"]
        assert set(colours.values()) == {BRAND_GREEN}, f"taglines are not all the brand green: {colours}"

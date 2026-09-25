"""Every page that has moved onto a page column, in a real browser.

A page's content sits in one of two columns, `.fhi-page` (reading, 760px)
or `.fhi-page-wide` (1140px), both defined once in custom.css. This walks
the pages that have moved and holds three things a stylesheet cannot prove
about itself: the column is the width its tier says at two desktop sizes and
is centred; on a phone it is the whole width the browser gives the page,
with the edge padding inside it, and nothing scrolls sideways; and running
text inside either column stops at the measure while the column itself
does not.

Widths are the column's padding box, which is the box the visitor sees. On
a phone the reference is the document's own client width rather than the
window size, because headless Chromium paints a scrollbar and takes its
width from the page.

Add a page here when it moves. The Bootstrap ratchet catches a page that
moves back.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

DESKTOP = (1440, 900)
LAPTOP = (1280, 720)
# Narrower than the wide tier wants: the column must give way and keep a
# buffer from the window's edge rather than run to it.
SMALL_LAPTOP = (1100, 800)
TABLET = (800, 1024)
PHONE = (390, 844)

TIER_WIDTH = {"reading": 760, "wide": 1140}
EDGE_PADDING = 16
# The least a column keeps from the window's edge above phone width (2rem).
LEAST_GUTTER = 32

# Page path -> tier. Grows as pages move. Only pages a plain GET reaches;
# the rest of the delete flow shares Delete Data's wrapper and is held by
# the Bootstrap ratchet. A hero page is measured on the column in its first
# band under the hero. Share your Denial and a glossary term are on the
# reading column but are left out: the first has no running text for the
# measure check to hold, and the second's text is a 20px lead whose 65ch is
# wider than the column. tests/sync/test_page_columns.py names both.
PAGES = {
    "/about-us": "wide",
    "/other-resources": "wide",
    "/how-to-help": "wide",
    "/media-references": "wide",
    "/treatments/": "wide",
    "/glossary/": "wide",
    "/state-help/": "wide",
    "/state-help/california/": "wide",
    "/remove_data": "reading",
    "/about-ai": "reading",
    "/faq/": "reading",
    "/contact": "reading",
    "/privacy_policy": "reading",
    "/tos": "reading",
    "/mhmda": "reading",
}

COLUMN_JS = """
const column = document.querySelector('.fhi-page, .fhi-page-wide');
if (!column) { return {missing: true}; }
const style = getComputedStyle(column);
const rect = column.getBoundingClientRect();
const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
const viewport = document.documentElement.clientWidth;
return {
  tier: column.classList.contains('fhi-page-wide') ? 'wide' : 'reading',
  // The padding box: the column as drawn, edge padding included.
  outer: column.clientWidth,
  inner: column.clientWidth - padding,
  padding: padding,
  left: rect.left,
  right: viewport - rect.right,
  viewport: viewport,
  // The column is the page's own; a Bootstrap grid around it would put
  // the width back in Bootstrap's hands.
  insideBootstrap: column.closest('.container, .container-fluid, .row') !== null,
  sideways: document.documentElement.scrollWidth
    > document.documentElement.clientWidth + 1,
  // Anything drawn past the column's content box, other than inside a
  // .scroll-x, which scrolls by design, and other than Bootstrap's grid
  // boxes: a .row is 24px wider than its parent by design and its
  // columns pad the content back inside, so neither draws anything.
  escapes: Array.from(column.querySelectorAll('*'))
    .filter(el => el.checkVisibility() && !el.closest('.scroll-x') && el.tagName !== 'SCRIPT')
    .filter(el => !el.matches('.row, [class*="col-"], .col'))
    .filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && (r.right > rect.right - parseFloat(style.paddingRight) + 1
        || r.left < rect.left + parseFloat(style.paddingLeft) - 1);
    })
    .slice(0, 5)
    .map(el => el.tagName.toLowerCase() + '.' + el.className + ' ' + Math.round(el.getBoundingClientRect().width) + 'px'),
};
"""

MEASURE_JS = """
const column = document.querySelector('.fhi-page, .fhi-page-wide');
const out = [];
for (const p of column.querySelectorAll('p, li')) {
  if (p.textContent.trim().length < 80 || !p.checkVisibility()) { continue; }
  // What holds the paragraph, less its padding: the width it could fill.
  const parent = p.parentElement, ps = getComputedStyle(parent);
  const room = parent.clientWidth - parseFloat(ps.paddingLeft) - parseFloat(ps.paddingRight);
  // 65 zeros in this paragraph's own face and size is what 65ch resolves
  // to. The probe sits on the body, so it cannot widen the paragraph.
  const face = getComputedStyle(p);
  const probe = document.createElement('span');
  probe.textContent = '0'.repeat(65);
  probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
  probe.style.fontFamily = face.fontFamily;
  probe.style.fontSize = face.fontSize;
  probe.style.fontWeight = face.fontWeight;
  probe.style.fontStyle = face.fontStyle;
  document.body.appendChild(probe);
  const measure = probe.getBoundingClientRect().width;
  probe.remove();
  out.push({text: p.textContent.trim().slice(0, 40),
            width: p.getBoundingClientRect().width, measure: measure, room: room,
            // The lede is narrower by its own rule and is left out; a
            // paragraph in a card, an alert or a grid cell fills that, and
            // room above is measured against that, not the column.
            lede: p.matches('.fhi-page-lede')});
}
return out;
"""


# The page's name and every section heading in its columns, as drawn.
HEADINGS_JS = """
const title = document.querySelector('main h1');
if (!title) { return {missing: true}; }
const sections = [];
for (const column of document.querySelectorAll('.fhi-page, .fhi-page-wide')) {
  for (const h2 of column.querySelectorAll('h2')) {
    if (!h2.checkVisibility()) { continue; }
    sections.push({text: h2.textContent.trim().slice(0, 40),
                   size: parseFloat(getComputedStyle(h2).fontSize)});
  }
}
return {title: title.textContent.trim().slice(0, 40),
        size: parseFloat(getComputedStyle(title).fontSize), sections: sections};
"""


class SeleniumTestPageWidths(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _column(self, page, size):
        self.set_window_size(*size)
        self.open(f"{self.live_server_url}{page}")
        self.wait_for_ready_state_complete()
        column = self.execute_script(COLUMN_JS)
        assert not column.get("missing"), f"{page} has no .fhi-page column"
        return column

    def test_a_desktop_column_is_its_tier_wide_and_centred(self):
        for page, tier in PAGES.items():
            for size in (DESKTOP, LAPTOP):
                with self.subTest(page=page, width=size[0]):
                    c = self._column(page, size)
                    assert c["tier"] == tier, f"{page} is on the {c['tier']} tier"
                    assert abs(c["outer"] - TIER_WIDTH[tier]) <= 1, (
                        f"{page} at {size[0]}px: the column is {c['outer']:.0f}px, "
                        f"not {TIER_WIDTH[tier]}px."
                    )
                    assert abs(c["left"] - c["right"]) <= 2, (
                        f"{page} at {size[0]}px: {c['left']:.0f}px on the left, "
                        f"{c['right']:.0f}px on the right."
                    )
                    assert not c["insideBootstrap"], (
                        f"{page}: the column sits inside a Bootstrap grid."
                    )

    def test_a_narrow_window_keeps_a_buffer_beside_the_column(self):
        """Between a phone and a full laptop the column gives way to the
        window and keeps a margin, the way Bootstrap's stepped container
        did. Without this a 1100px window ran the wide tier to the edge."""
        for page, tier in PAGES.items():
            for size in (SMALL_LAPTOP, TABLET):
                with self.subTest(page=page, width=size[0]):
                    c = self._column(page, size)
                    assert c["outer"] <= TIER_WIDTH[tier] + 1, (
                        f"{page} at {size[0]}px: the column is {c['outer']:.0f}px, wider "
                        f"than its {TIER_WIDTH[tier]}px tier."
                    )
                    assert min(c["left"], c["right"]) >= LEAST_GUTTER, (
                        f"{page} at {size[0]}px: {c['left']:.0f}px on the left and "
                        f"{c['right']:.0f}px on the right; the column runs to the edge."
                    )
                    assert abs(c["left"] - c["right"]) <= 2
                    assert not c["sideways"] and not c["escapes"], (
                        f"{page} at {size[0]}px: sideways={c['sideways']} escapes={c['escapes']}"
                    )

    def test_a_phone_column_is_the_whole_screen_with_the_edge_inside_it(self):
        for page in PAGES:
            with self.subTest(page=page):
                c = self._column(page, PHONE)
                assert abs(c["outer"] - c["viewport"]) <= 1, (
                    f"{page} on a phone: the column is {c['outer']:.0f}px of a "
                    f"{c['viewport']:.0f}px screen."
                )
                assert abs(c["padding"] - 2 * EDGE_PADDING) <= 1, (
                    f"{page} on a phone: {c['padding']:.0f}px of edge padding, "
                    f"not {2 * EDGE_PADDING}px."
                )
                assert not c["sideways"], f"{page} scrolls sideways on a phone."
                assert not c["escapes"], (
                    f"{page} on a phone: past the column's edge: {c['escapes']}"
                )

    def test_the_page_after_the_delete_form_holds_the_column_too(self):
        """Check Your Email is reached only by submitting Delete Data, so the
        walk submits it. Its note used to carry an inline 640px cap that beat
        the column on a phone and ran 373px wide in a 350px column."""
        for size in (DESKTOP, PHONE):
            with self.subTest(width=size[0]):
                self.set_window_size(*size)
                self.open(f"{self.live_server_url}/remove_data")
                self.wait_for_ready_state_complete()
                self.type("#id_email", "nobody@example.com")
                self.click("#submit")
                self.wait_for_element(".alert-info")
                c = self.execute_script(COLUMN_JS)
                assert not c.get("missing"), "Check Your Email has no .fhi-page column"
                if size is PHONE:
                    assert abs(c["outer"] - c["viewport"]) <= 1, (
                        f"Check Your Email on a phone: the column is {c['outer']:.0f}px "
                        f"of a {c['viewport']:.0f}px screen."
                    )
                else:
                    assert abs(c["outer"] - TIER_WIDTH["reading"]) <= 1, (
                        f"Check Your Email at {size[0]}px: the column is {c['outer']:.0f}px."
                    )
                assert not c["sideways"], "Check Your Email scrolls sideways."
                assert not c["escapes"], (
                    f"Check Your Email at {size[0]}px: past the column's edge: {c['escapes']}"
                )
                note = self.execute_script(
                    "return document.querySelector('.alert-info').getBoundingClientRect().width"
                )
                assert note <= min(640, c["inner"]) + 1, (
                    f"Check Your Email at {size[0]}px: the note is {note:.0f}px wide in a "
                    f"{c['inner']:.0f}px column."
                )

    def test_wide_text_fills_its_column_like_the_boxes_below_it(self):
        """The wide tier has no measure: a paragraph is as wide as what
        holds it, whether that is the column, a grid cell or a card, the
        way the boxes around it are. Melanie's call after 650px paragraphs
        sat over 1140px of boxes on About Us."""
        for page, tier in PAGES.items():
            if tier != "wide":
                continue
            with self.subTest(page=page):
                self._column(page, DESKTOP)
                paragraphs = [p for p in self.execute_script(MEASURE_JS) if not p["lede"]]
                assert paragraphs, f"{page}: no running text to measure"
                for p in paragraphs:
                    assert p["width"] >= p["room"] - 1, (
                        f"{page}: '{p['text']}...' is {p['width']:.0f}px in "
                        f"{p['room']:.0f}px of room; a wide page's text fills it."
                    )

    def test_a_section_heading_is_smaller_than_the_page_name(self):
        """main.css draws every h2 at the page size, the size of the title's
        h1, so a page with a real outline drew its sections as large as its
        name and the pages took h4 to h6 for their sections instead. The
        column sizes its headings now; this holds that a section on any
        moved page, at a desktop and on a phone, stays below the title."""
        checked = 0
        for page in PAGES:
            for size in (DESKTOP, PHONE):
                with self.subTest(page=page, width=size[0]):
                    self._column(page, size)
                    found = self.execute_script(HEADINGS_JS)
                    assert not found.get("missing"), f"{page} has no h1 in <main>"
                    for h2 in found["sections"]:
                        checked += 1
                        assert h2["size"] < found["size"], (
                            f"{page} at {size[0]}px: the h2 '{h2['text']}' is "
                            f"{h2['size']:.1f}px, as large as the page's name "
                            f"'{found['title']}' at {found['size']:.1f}px."
                        )
        assert checked, "no page in PAGES has a section heading to compare"

    def test_running_text_stops_at_the_measure_and_the_column_does_not(self):
        for page, tier in PAGES.items():
            if tier != "reading":
                continue
            with self.subTest(page=page):
                c = self._column(page, DESKTOP)
                paragraphs = [p for p in self.execute_script(MEASURE_JS) if not p["lede"]]
                assert paragraphs, f"{page}: no running text found to measure"
                for p in paragraphs:
                    assert p["width"] <= p["measure"] + 1, (
                        f"{page}: '{p['text']}...' runs {p['width']:.0f}px, past the "
                        f"{p['measure']:.0f}px measure."
                    )
                # A paragraph that wraps is exactly as wide as the measure,
                # so the widest one proves the measure really is 65ch and
                # not something narrower that the cap above would also pass.
                longest = max(paragraphs, key=lambda p: p["width"])
                assert abs(longest["width"] - longest["measure"]) <= 1, (
                    f"{page}: the widest paragraph is {longest['width']:.0f}px; the "
                    f"measure is {longest['measure']:.0f}px, so text is not reaching it."
                )
                # The measure is on the text, not on the page: the column
                # keeps room past the longest line, at least the edge
                # padding's worth on both sides.
                widest = max(p["measure"] for p in paragraphs)
                assert c["inner"] - widest >= 2 * EDGE_PADDING, (
                    f"{page}: the column ({c['inner']:.0f}px) is no wider than the "
                    f"measure ({widest:.0f}px); the measure is on the page, not the text."
                )

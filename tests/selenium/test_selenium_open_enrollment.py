"""The open enrollment page, run in a browser.

The sync tests read the script's source; these check that the marketplace
chart plots each February's count and marks the year CMS is still counting,
that a link into a closed question or the folded sources opens it, and that
the page fits a phone without scrolling sideways.
"""

import json
import pathlib
import re

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase
from .test_selenium_denial_watch import HASH_CHANGE

BaseCase.main(__name__, __file__)

DATA = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static" / "denial_watch.json").read_text()
)

# What the marketplace chart and year table drew: points, axis ticks, year labels, the line, the end label and the table's cells.
MARKETPLACE_DRAWN = """
var svg = document.getElementById('mkline'), num = (e, a) => Number(e.getAttribute(a)),
    labels = svg.querySelectorAll('g.dw-axis text');
return {
  points: [...svg.querySelectorAll('circle')].map(c => [num(c, 'cx'), num(c, 'cy')]),
  ticks: [...svg.querySelectorAll('g.dw-axis line')].map((l, i) => [labels[i].textContent, num(l, 'y1')]),
  years: [...svg.querySelectorAll(':scope > text:not(.dw-end)')].map(t => [t.textContent, num(t, 'x')]),
  path: svg.querySelector('path').getAttribute('d'),
  end: svg.querySelector('text.dw-end').textContent,
  rows: [...document.querySelectorAll('#mktable tbody tr')].map(r => [...r.cells].map(c => c.textContent)),
};
"""

# The target's fold is open and its top is in view, below the sticky header (a pixel or two of rounding).
OPEN_AND_LANDED = """
var t = document.getElementById(arguments[0]), r = t.getBoundingClientRect(),
    head = document.querySelector('.navbar-default').getBoundingClientRect().bottom;
return t.closest('details').open && r.top >= head - 2 && r.top < window.innerHeight;
"""


def _marketplace_drawn_problems(drawn, mk):
    """Where the drawn chart or year table disagrees with the bundle; empty when they agree."""
    feb, problems = mk["feb"], []
    # The value scale comes from the drawn axis: each tick's label and the height of its line.
    ticks = [(float(t.rstrip("M")) * (1e6 if t.endswith("M") else 1), y) for t, y in drawn["ticks"]]
    (v0, y0), (v1, y1) = ticks[0], ticks[-1]

    def height(v):
        return y0 + (v - v0) * (y1 - y0) / (v1 - v0)

    problems += [f"tick {v:g} at {y}" for v, y in ticks if abs(y - height(v)) > 1e-6]
    if len(drawn["points"]) != len(feb):
        return problems + [f"{len(drawn['points'])} points for {len(feb)} Februaries"]
    for r, (_, cy) in zip(feb, drawn["points"]):
        if abs(cy - height(r["n"])) > 1e-6:
            problems.append(f"{r['y']} drawn at {cy}, not {height(r['n'])}")
    xs = [cx for cx, _ in drawn["points"]]
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    if not all(g > 0 and abs(g - gaps[0]) < 1e-6 for g in gaps):
        problems.append(f"the years aren't evenly spaced in order: {xs}")
    x_of = {str(r["y"]): cx for r, cx in zip(feb, xs)}
    for year, x in drawn["years"]:
        if year not in x_of or abs(x - x_of[year]) > 1e-6:
            problems.append(f"year label {year} at {x}, its point at {x_of.get(year)}")
    path = [tuple(map(float, p.split())) for p in re.split("[ML]", drawn["path"]) if p.strip()]
    if len(path) != len(xs) or any(abs(a - b) > 0.051 for p, q in zip(path, drawn["points"]) for a, b in zip(p, q)):
        problems.append(f"the line doesn't run through the points: {drawn['path']}")
    if drawn["end"] != f"{feb[-1]['n'] / 1e6:.1f}M":
        problems.append(f"end label {drawn['end']}")
    rows = []
    for i, r in enumerate(feb):
        change = r["n"] / feb[i - 1]["n"] - 1 if i else None
        rows.append([
            f"{r['y']} (preliminary)" if r["y"] == mk["prelim"] else str(r["y"]),
            f"{r['n']:,}",
            "" if change is None else ("+" if change > 0 else "") + f"{change * 100:.1f}%",
        ])
    if drawn["rows"] != rows:
        problems.append(f"year table {drawn['rows']} isn't {rows}")
    return problems


class SeleniumTestOpenEnrollment(FHISeleniumBase, StaticLiveServerTestCase):
    """The marketplace chart, the folded questions and sources, and the phone layout, on /open-enrollment."""

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _open(self, width=1280, height=900, hash=""):
        self.set_window_size(width, height)
        self.open(f"{self.live_server_url}{reverse('open-enrollment')}{hash}")
        self.wait_for_page_ready(predicate=lambda d: d.execute_script("return !!document.querySelector('#mkline circle')"))

    def _landed_on(self, ident):
        # The page scrolls smoothly, and mid-scroll the target passes through the view, so check where it stops.
        last = []

        def stopped(d):
            last.append(d.execute_script("return document.getElementById(arguments[0]).getBoundingClientRect().top", ident))
            return len(last) > 1 and last[-1] == last[-2]

        self.wait_for_page_ready(predicate=stopped)
        self.assertTrue(self.execute_script(OPEN_AND_LANDED, ident), f"{ident} stopped at {last[-1]}")

    def test_marketplace_chart_draws_only_the_preliminary_year_hollow(self):
        feb, prelim = DATA["marketplace"]["feb"], DATA["marketplace"]["prelim"]
        self._open()
        fills, rows = self.execute_script(
            "return [[...document.querySelectorAll('#mkline circle')].map(c => c.getAttribute('fill')),"
            " [...document.querySelectorAll('#mktable tbody tr')].map(r => r.cells[0].textContent)];"
        )
        # One point and one row per February, in order; only the year without its December is hollow and labelled.
        self.assertEqual(len(fills), len(feb))
        self.assertEqual([r["y"] for r, fill in zip(feb, fills) if fill == "#ffffff"], [prelim])
        self.assertEqual(rows, [f"{r['y']} (preliminary)" if r["y"] == prelim else str(r["y"]) for r in feb])

    def test_marketplace_chart_and_table_show_each_february_count(self):
        self._open()
        self.assertEqual(_marketplace_drawn_problems(self.execute_script(MARKETPLACE_DRAWN), DATA["marketplace"]), [])

    def test_a_how_we_got_these_numbers_link_opens_the_folded_sources(self):
        self._open()
        self.assertFalse(self.execute_script("return document.getElementById('sources-details').open"))
        # Chromium opens a fold itself on a jump into it, so the jump is cancelled: a listener on window runs
        # after the page's own on document, which leaves only the page script to open the fold.
        self.execute_script("window.addEventListener('click', e => e.preventDefault())")
        self.click('#enroll .dw-how a[href="#method-enroll"]')
        self.assertTrue(self.execute_script("return document.getElementById('sources-details').open"))

    def test_a_hash_change_opens_a_closed_question_below_the_header(self):
        self._open()
        self.assertFalse(self.execute_script("return document.getElementById('lost-coverage').open"))
        self.execute_script(HASH_CHANGE, "#lost-coverage")
        self._landed_on("lost-coverage")

    def test_opening_the_page_at_its_method_note_opens_the_folded_sources(self):
        self._open(hash="#method-enroll")
        self._landed_on("method-enroll")

    def test_an_old_denial_watch_link_lands_on_the_part_that_moved_here(self):
        # Denial Watch used to have this question; a link shared to it there opens it here.
        self.set_window_size(1280, 900)
        self.open(f"{self.live_server_url}{reverse('denial-watch')}#lost-coverage")
        here = reverse("open-enrollment")
        self.wait_for_page_ready(
            predicate=lambda d: d.execute_script("return location.pathname === arguments[0] && !!document.querySelector('#mkline circle')", here)
        )
        self.assertEqual(self.execute_script("return location.hash"), "#lost-coverage")
        self._landed_on("lost-coverage")

    def test_no_sideways_scroll_on_a_phone(self):
        for width in (390, 320):
            with self.subTest(width=width):
                self._open(width, 844)
                page, window = self.execute_script("return [document.documentElement.scrollWidth, window.innerWidth]")
                self.assertLessEqual(page, window + 1, f"the page scrolls sideways at {width}px")

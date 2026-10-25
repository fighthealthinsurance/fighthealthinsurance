"""Denial Watch's view buttons, run in a browser.

The sync tests read the script's source; these check that switching a view
changes what a reader sees, that the map buttons wrap on a phone, and that
the marketplace chart plots each February's count and marks the year CMS is
still counting.
"""

import json
import pathlib
import re
from datetime import date
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

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


class SeleniumTestDenialWatchViews(FHISeleniumBase, StaticLiveServerTestCase):
    """The map and trend buttons, and the marketplace chart, on /denial-watch."""

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _open(self, width=1280, height=900):
        self.set_window_size(width, height)
        self.open(f"{self.live_server_url}{reverse('denial-watch')}")
        self.wait_for_page_ready(predicate=lambda d: d.execute_script("return !!document.querySelector('#dmap g')"))

    @mock.patch("fighthealthinsurance.views.DENIAL_WATCH_HERO_UNTIL", date(2099, 1, 1))
    def test_home_page_announces_it_with_todays_count(self):
        # The announcement retires itself on a date, so the check pins it open.
        self.set_window_size(1280, 900)
        self.open(f"{self.live_server_url}{reverse('root')}")
        self.assert_text_eventually_contains(
            f"HealthCare.gov claims today, at {DATA['trend'][-1]['year']}'s pace", "denial-watch-hero"
        )
        self.assertIn("Denial Watch: insurers denied", self.get_text("#denial-watch-hero"))
        shown, expected, seconds_in = self.execute_script(
            "var a = document.getElementById('denial-watch-hero');"
            "var n = new Date(), m = new Date(n.getFullYear(), n.getMonth(), n.getDate());"
            "return [Number(a.querySelector('.hero-announce-count').textContent.replace(/,/g, '')),"
            " Math.floor((n - m) / 86400000 * Number(a.getAttribute('data-per-day'))), (n - m) / 1000];"
        )
        # Today's count so far, not the day's average: at most a few minutes behind the clock.
        if seconds_in > 600:
            self.assertLessEqual(shown, expected)
            self.assertGreaterEqual(shown, expected - 1000)
        self.assertTrue(self.get_attribute("#denial-watch-hero", "href").endswith(reverse("denial-watch")))

    def test_out_of_network_view_is_on_the_trend_chart_only(self):
        self._open()
        self.assertEqual(self.execute_script("return document.querySelectorAll('#dmode button[data-m=\"out\"]').length"), 0)
        self.click('#tmode button[data-m="out"]')
        self.assert_text_eventually_contains("about twice the in-network rate", "tnote")
        self.assertIn(
            "out-of-network denial rate by year, 2022 to 2024",
            self.get_attribute("#tline", "aria-label"),
        )
        self.assertTrue(self.execute_script("return !!document.querySelector('#tnote a[href=\"#who-to-call\"]')"))

    def test_all_claims_map_outlines_left_out_states_and_greys_the_rest(self):
        self._open()
        self.click('#dmode button[data-m="all"]')
        gone = self.execute_script(
            "return ['AK','ND','OH','CA'].map(s=>document.querySelector('#dmap g[data-s=\"'+s+'\"]').classList.contains('dw-gone'));"
        )
        # Alaska and North Dakota report but have no all-claims figure; Ohio has one; California has no data here.
        self.assertEqual(gone, [True, True, False, False])

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

    def _rows(self):
        return self.execute_script("return [...document.querySelectorAll('#itable tbody tr')].map(r => r.cells[0].textContent)")

    def test_picking_a_state_on_the_map_lists_its_insurers(self):
        texas = sorted(i["n"] for i in DATA["issuers"] if i["s"] == "TX")
        self._open()
        self.assert_text_eventually_contains("in Florida", "icount")
        self.click('#dmap g[data-s="TX"]')
        self.assert_text_eventually_contains("in Texas", "icount")
        self.assertEqual(self.get_value("#istate"), "TX")
        self.assertEqual(sorted(self._rows()), texas)
        # Hovering another state previews it without moving the table.
        self.hover('#dmap g[data-s="OH"]')
        self.assert_text_eventually_contains("Ohio", "dread")
        self.assertEqual(sorted(self._rows()), texas)

    def test_picking_a_state_with_no_insurers_says_so(self):
        self._open()
        self.click('#dmap g[data-s="CA"]')
        self.assert_text_eventually_contains("California isn't in this data", "itable")
        self.assertEqual(self.get_value("#istate"), "")

    def test_map_buttons_wrap_on_a_phone(self):
        self._open(375, 812)
        sizes = self.execute_script(
            """
            var box = document.getElementById('dmode').getBoundingClientRect();
            var out = [...document.querySelectorAll('#dmode button')].filter(
                b => b.getBoundingClientRect().right > box.right + 1).length;
            var wrap = document.querySelector('#insurers .dw-table-wrap');
            return [document.documentElement.scrollWidth, window.innerWidth, out,
                    getComputedStyle(wrap).overflowX];
            """
        )
        page, window, buttons_out, table_overflow = sizes
        self.assertLessEqual(page, window + 1, "the page scrolls sideways on a phone")
        self.assertEqual(buttons_out, 0, "a map button runs past its row")
        # A table wider than the phone scrolls inside its own box, as elsewhere on the site.
        self.assertIn(table_overflow, ("auto", "scroll"))

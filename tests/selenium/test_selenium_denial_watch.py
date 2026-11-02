"""Denial Watch's view buttons, run in a browser.

The sync tests read the script's source; these check that switching a view
changes what a reader sees, that the map buttons wrap on a phone, and that a
link into the folded sources opens them. The marketplace chart moved to the
open enrollment page, with its browser tests.
"""

import json
import pathlib
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

# The folded sources are open and the target's top is in view, below the sticky header (a pixel or two of rounding).
LANDED = """
var t = document.getElementById(arguments[0]), r = t.getBoundingClientRect(),
    head = document.querySelector('.navbar-default').getBoundingClientRect().bottom;
return document.getElementById('sources-details').open && r.top >= head - 2 && r.top < window.innerHeight;
"""

# A hash change with no jump of the browser's own.
HASH_CHANGE = """
history.replaceState(null, '', arguments[0]);
window.dispatchEvent(new HashChangeEvent('hashchange'));
"""


class SeleniumTestDenialWatchViews(FHISeleniumBase, StaticLiveServerTestCase):
    """The map and trend buttons, and the folded sources, on /denial-watch."""

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
    @mock.patch("fighthealthinsurance.views.denial_watch_enrollment", new=lambda today: None)
    def test_home_page_announces_it_with_todays_count(self):
        # The announcement retires on a date and gives way to open enrollment's, so the check pins it open and the season shut.
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
            "out-of-network denial rate by year, 2022 to 2025",
            self.get_attribute("#tline", "aria-label"),
        )
        self.assertTrue(self.execute_script("return !!document.querySelector('#tnote a[href=\"#who-to-call\"]')"))

    def test_all_claims_map_outlines_left_out_states_and_greys_the_rest(self):
        self._open()
        self.click('#dmode button[data-m="all"]')
        gone = self.execute_script(
            "return ['MI','OH','CA'].map(s=>document.querySelector('#dmap g[data-s=\"'+s+'\"]').classList.contains('dw-gone'));"
        )
        # Michigan reports but has no all-claims figure; Ohio has one; California has no data here.
        self.assertEqual(gone, [True, False, False])

    def _sources_open(self):
        return self.execute_script("return document.getElementById('sources-details').open")

    def _landed_on(self, ident):
        # The page scrolls smoothly, and mid-scroll the target passes through the view, so check where it stops.
        last = []

        def stopped(d):
            last.append(d.execute_script("return document.getElementById(arguments[0]).getBoundingClientRect().top", ident))
            return len(last) > 1 and last[-1] == last[-2]

        self.wait_for_page_ready(predicate=stopped)
        self.assertTrue(self.execute_script(LANDED, ident), f"{ident} stopped at {last[-1]}")

    def test_a_how_we_got_these_numbers_link_opens_the_folded_sources(self):
        self._open()
        self.assertFalse(self._sources_open())
        # Chromium opens a fold itself on a jump into it, so the jump is cancelled: a listener on window runs
        # after the page's own on document, which leaves only the page script to open the fold.
        self.execute_script("window.addEventListener('click', e => e.preventDefault())")
        self.click('#coverage .dw-how a[href="#method-coverage"]')
        self.assertTrue(self._sources_open())
        self.assertEqual(self.execute_script("return location.hash"), "")

    def test_a_hash_change_opens_the_folded_sources_below_the_header(self):
        # As on back or forward after a reader closed the fold: replaceState neither scrolls nor opens anything.
        self._open()
        self.assertFalse(self._sources_open())
        self.execute_script(HASH_CHANGE, "#method-coverage")
        self._landed_on("method-coverage")

    def test_opening_the_page_at_a_method_note_opens_the_folded_sources(self):
        # Chromium may open this one itself; the check is that the reader lands below the header.
        self.set_window_size(1280, 900)
        self.open(f"{self.live_server_url}{reverse('denial-watch')}#method-coverage")
        self.wait_for_page_ready(predicate=lambda d: d.execute_script("return !!document.querySelector('#dmap g')"))
        self._landed_on("method-coverage")

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
        self.assertNotIn("NY", DATA["grey_issuers"])
        self._open()
        self.click('#dmap g[data-s="NY"]')
        self.assert_text_eventually_contains("New York isn't in this data", "itable")
        self.assertEqual(self.get_value("#istate"), "")
        self.assertFalse(self.is_element_visible("#isource"))

    def test_picking_a_grey_state_lists_its_insurers_with_their_year(self):
        georgia = DATA["grey_issuers"]["GA"]
        self._open()
        self.click('#dmap g[data-s="GA"]')
        self.assert_text_eventually_contains(f"in Georgia, {georgia['year']} claims", "icount")
        self.assertEqual(self.get_value("#istate"), "GA")
        self.assertEqual(sorted(self._rows()), sorted(r["n"] for r in georgia["rows"]))
        self.assertIn(f"{georgia['year']} claims, from insurers' own reports to CMS, the last year its files include Georgia.", self.get_text("#isource"))
        self.assertIn("See Georgia's insurers", self.get_text("#dread"))

    def test_california_has_no_appeals_column_and_names_who_is_left_out(self):
        california = DATA["grey_issuers"]["CA"]
        self._open()
        self.select_option_by_value("#istate", "CA")
        self.assert_text_eventually_contains("in California, 2025 claims", "icount")
        self.assertEqual(sorted(self._rows()), sorted(r["n"] for r in california["rows"]))
        heads = self.execute_script("return [...document.querySelectorAll('#itable thead th')].map(c => c.textContent)")
        self.assertEqual(heads, ["Insurer", "Denial rate", "Claims"])
        self.assertIn("These reports have no appeal counts.", self.get_text("#isource"))
        self.assertIn("Not in the table: Aetna CVS Health (no 2025 report posted)", self.get_text("#ileft"))

    def test_what_changed_tables_are_drawn_and_a_change_matches_its_figures(self):
        self._open()
        rows = self.execute_script(
            "return [...document.querySelectorAll('#chcompany tbody tr')].map(r => [...r.cells].map(c => c.textContent))"
        )
        self.assertEqual([r[0] for r in rows], [c["k"] for c in DATA["changes"]["companies"] if c["k"] != "All these insurers"])
        for row in rows:
            old, new = float(row[1].rstrip("%")), float(row[2].rstrip("%"))
            word = "down" if new < old else "up"
            self.assertEqual(row[3], "no change" if new == old else f"{word} {abs(new - old):.1f}", row[0])
        for table, count in (("chreasons", 9), ("chappeals", 3), ("chreview", len(DATA["imr_periods"]["rows"]))):
            self.assertEqual(self.execute_script(f"return document.querySelectorAll('#{table} tbody tr').length"), count, table)

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

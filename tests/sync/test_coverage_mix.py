"""Marketplace enrollment in context: where people get coverage, and coverage beside population.

The open enrollment page and Denial Watch's coverage section share one donut of
everyone, each person counted once, from FHI's tally of Census's survey
(partials/coverage_mix.html). The open enrollment page also sets each
February's marketplace coverage beside the people under 65. The numbers come
from static/denial_watch.json, which build_bundle.py makes from the checked-in
CSVs, and some are written into sentences, so these tests check the copy still
agrees with the data after a refresh.
"""

import csv
import math
import pathlib
import re
import tempfile

from django.test import TestCase
from django.urls import reverse

from tests.sync.test_contrast import DW_PAIRS, contrast_ratio, dw_colour
from tests.sync.test_denial_watch import DATA, MK, REPO, _between, _load_builder, _millions, _page, _text
from tests.sync.test_open_enrollment import IN_SEASON, OE_JS, _enroll, _section_parents, _table_rows, _words

HERE = REPO / "scripts" / "denial_watch"
MIX = DATA["coverage_mix"]
POP = DATA["population"]
COUNTS = DATA["coverage_counts"]
STYLE = (REPO / "fighthealthinsurance" / "templates" / "partials" / "denial_watch_style.html").read_text()

# From CMS's 2025 Open Enrollment state-level public use file (the zip marketplace_signups.csv records), Total/All row:
# Age_GE65 and BHP_Enrlmnt. The page rounds them to "about 1.7%" and "about 1.8 million".
OEP_2025_65_PLUS = 412_938
OEP_2025_BASIC_HEALTH = 1_792_695
# Where the population series join, April 1, 2020, people under 65: the intercensal file's CENSUS2020POP and
# Vintage 2025's ESTIMATESBASE2020, summed over ages 0 to 64.
JOIN_INTERCENSAL, JOIN_V2025 = 275_687_042, 275_888_022
SURVEY_PAGE = "https://www.census.gov/data/datasets/time-series/demo/cps/cps-asec.2026.html"
INTERCENSAL = "https://www2.census.gov/programs-surveys/popest/datasets/2010-2020/intercensal/national/asrh/nc-est2020int-agesex-res.csv"
VINTAGE_2025 = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/national/asrh/nc-est2025-agesex-res.csv"
MEDICARE_PAGE = "https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/medicare-and-medicaid-reports/medicare-monthly-enrollment"
MEDICAID_PAGE = "https://data.medicaid.gov/dataset/6165f45b-ca93-5bb5-9d06-db29c692a360"


def _mix(html: str) -> str:
    return _between(html, '<section class="fhi-stack" id="coverage-mix"', "</section>")


def _donut(mix: str) -> str:
    return _between(mix, '<svg class="dw-mix-donut"', "</svg>")


# One ring slice as the view writes it: along the outer edge clockwise, in to the inner edge, and back anticlockwise.
_N = r"(-?[\d.]+)"
SLICE = re.compile(
    rf'<path class="dw-mix-([\w-]+)" d="M {_N} {_N} A {_N} \4 0 ([01]) 1 {_N} {_N} L {_N} {_N} A {_N} \10 0 \5 0 {_N} {_N} Z"/>'
)


def _slices(mix: str) -> list[dict]:
    """Each path in the donut, in drawing order: its kind, radii, arc flag, and the four corners as (x, y)."""
    donut = _donut(mix)
    found = SLICE.findall(donut)
    assert len(found) == donut.count("<path"), "a slice path isn't one ring slice"
    out = []
    for css, x0, y0, outer, big, x1, y1, x2, y2, inner, x3, y3 in found:
        f = [float(v) for v in (x0, y0, x1, y1, x2, y2, x3, y3)]
        out.append(
            dict(css=css, outer=float(outer), inner=float(inner), big=big == "1", start=(f[0], f[1]), end=(f[2], f[3]), end_in=(f[4], f[5]), start_in=(f[6], f[7]))
        )
    return out


def _centre(mix: str) -> float:
    # A square viewBox from 0, so the centre is half its side on both axes.
    side = re.search(r'viewBox="0 0 ([\d.]+) \1"', _donut(mix))
    assert side, "the donut's viewBox isn't a square from 0"
    return float(side.group(1)) / 2


def _turn(point: tuple[float, float], c: float) -> float:
    """How far round a point is, clockwise from 12 o'clock, in percent of the circle."""
    x, y = point
    return 100 * (math.atan2(x - c, c - y) / (2 * math.pi) % 1)


def _apart(a: float, b: float) -> float:
    """The distance between two turns round the circle, in percent."""
    return abs((a - b + 50) % 100 - 50)


def _by_kind(mix: str) -> dict[str, dict]:
    return {s["css"]: s for s in _slices(mix)}


def _css(key: str) -> str:
    return key.replace("_", "-")


def _rows(path, **match):
    with open(path, newline="") as f:
        return [r for r in csv.DictReader(f) if all(r[k] == v for k, v in match.items())]


def _share(key: str) -> float:
    return next(r["pct"] for r in MIX["rows"] if r["k"] == key)


def _under_65() -> dict[int, int]:
    return {r["y"]: r["n"] for r in POP["under_65"]}


class TestCoverageMixData(TestCase):
    """The donut's rows are the checked-in tally, each person once, and the build stops on a tally it can't draw."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    def _load(self, edit):
        with open(HERE / "coverage_mix.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        rows = edit(rows)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        try:
            return self.b.load_coverage_mix(f.name)
        finally:
            pathlib.Path(f.name).unlink()

    def test_bundle_rows_are_the_checked_in_tally(self):
        rows = _rows(HERE / "coverage_mix.csv", measure="now", age="all")
        self.assertEqual(
            [(r["k"], r["label"], r["n"], r["pct"]) for r in MIX["rows"]],
            [(r["key"], r["category"], int(r["people"]), float(r["percent"])) for r in rows],
        )
        self.assertEqual(self.b.load_coverage_mix(), MIX)

    def test_shares_add_to_100_within_rounding(self):
        # Each person is in one slice, so the slices add to everyone, and the shares as shown add to 100 too.
        self.assertAlmostEqual(sum(r["pct"] for r in MIX["rows"]), 100, delta=0.05)
        self.assertAlmostEqual(sum(round(r["pct"], 1) for r in MIX["rows"]), 100, delta=0.3)
        total = sum(r["n"] for r in MIX["rows"])
        for r in MIX["rows"]:
            self.assertAlmostEqual(100 * r["n"] / total, r["pct"], delta=0.006, msg=r["k"])

    def test_every_margin_of_error_is_under_half_a_point(self):
        # The method note says every share is within half a point either way.
        self.assertTrue(all(r["pct_moe"] < 0.5 for r in MIX["rows"]), [r["pct_moe"] for r in MIX["rows"]])

    def test_the_donut_is_everyone_at_the_interview(self):
        self.assertEqual(MIX["period"], "At interview, Feb-Apr 2026")
        self.assertEqual(MIX["page"], SURVEY_PAGE)
        self.assertEqual(MIX["file"].rsplit("/", 1)[1], "asecpub26csv.zip")

    def test_builder_refuses_shares_that_dont_add_to_100(self):
        def one_point_more(rows):
            for r in rows:
                if r["measure"] == "now" and r["age"] == "all" and r["key"] == "uninsured":
                    r["percent"] = f"{float(r['percent']) + 1:.2f}"
            return rows

        with self.assertRaisesRegex(SystemExit, "the shares add to 101.00%"):
            self._load(one_point_more)

    def test_builder_refuses_a_margin_that_isnt_a_number(self):
        for bad in ("nan", "inf", "-0.1"):
            with self.subTest(bad=bad):

                def broken(rows):
                    for r in rows:
                        if r["measure"] == "now" and r["age"] == "all" and r["key"] == "uninsured":
                            r["percent_moe90"] = bad
                    return rows

                with self.assertRaisesRegex(SystemExit, "margin of error"):
                    self._load(broken)

    def test_builder_refuses_slices_out_of_order(self):
        def swap(rows):
            mine = [i for i, r in enumerate(rows) if r["measure"] == "now" and r["age"] == "all"]
            rows[mine[0]], rows[mine[1]] = rows[mine[1]], rows[mine[0]]
            return rows

        with self.assertRaisesRegex(SystemExit, "in that order"):
            self._load(swap)

    def test_tally_script_keeps_the_page_order_and_the_counting_rule(self):
        script = (HERE / "cps_coverage_mix.py").read_text()
        self.assertEqual(re.findall(r'^    \("(\w+)", "([^"]+)"\),$', script, re.M), [(r["k"], r["label"]) for r in MIX["rows"]])
        self.assertIn('if "medicare" in held:\n        return "medicare"', script)
        self.assertIn("if len(held) == 1:", script)
        self.assertIn('return "more_than_one"', script)
        self.assertIn("https://www2.census.gov/programs-surveys/cps/datasets/2026/march/asecpub26csv.zip", script)
        self.assertIn(MIX["sha256"], script)


class TestPopulationData(TestCase):
    """People under 65 by July 1, from two Census series that are never mixed with another vintage."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()
        with open(HERE / "population_under_65.csv", newline="") as f:
            cls.csv = list(csv.DictReader(f))

    def _load(self, edit, feb_years=(2016,)):
        rows = edit([dict(r) for r in self.csv])
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        try:
            return self.b.load_population(list(feb_years), f.name)
        finally:
            pathlib.Path(f.name).unlink()

    def test_bundle_population_is_the_csv(self):
        self.assertEqual(POP["under_65"], [dict(y=int(r["year"]), n=int(r["under_65"])) for r in self.csv])
        self.assertEqual(self.b.load_population([r["y"] for r in MK["feb"]]), POP)

    def test_intercensal_to_2019_then_vintage_2025(self):
        files = {int(r["year"]): (r["source_url"], r["file_sha256"]) for r in self.csv}
        self.assertEqual({y for y, (u, _) in files.items() if u == INTERCENSAL}, set(range(2015, 2020)))
        self.assertEqual({y for y, (u, _) in files.items() if u == VINTAGE_2025}, set(range(2020, 2026)))
        self.assertEqual({s for y, (_, s) in files.items() if y < 2020}, {"494672c99edd48ebc3f8052a0fdae54083ef7bc1467d9c5394de47cd8a1525e3"})
        self.assertEqual({s for y, (_, s) in files.items() if y >= 2020}, {"6436d4f6972d415caf14cb20776807bc031fc9f1bfc9d6ff26055d592a2b0230"})

    def test_covers_the_july_before_every_february(self):
        self.assertTrue({r["y"] - 1 for r in MK["feb"]} <= set(_under_65()))

    def test_builder_refuses_another_vintage(self):
        # Vintage 2024 rebuilt its base, so mixing it in would fake a jump of about 1.7 million at 2020.
        def v2024(rows):
            rows[-1].update(series="Vintage 2024", source_file="nc-est2024-agesex-res.csv")
            return rows

        with self.assertRaisesRegex(SystemExit, "must come from Vintage 2025"):
            self._load(v2024)

    def test_builder_refuses_a_missing_july(self):
        with self.assertRaisesRegex(SystemExit, "no July"):
            self._load(lambda rows: rows, feb_years=(2027,))

    def test_builder_refuses_a_gap(self):
        with self.assertRaisesRegex(SystemExit, "without a gap"):
            self._load(lambda rows: [r for r in rows if r["year"] != "2018"])


class TestCoverageCountsData(TestCase):
    """Medicare and Medicaid and CHIP for the marketplace's latest February, from CMS's records."""

    def test_bundle_counts_are_the_csv(self):
        rows = {r["coverage"]: r for r in _rows(HERE / "coverage_counts.csv")}
        self.assertEqual(COUNTS["medicare"]["n"], int(rows["Medicare"]["people"]))
        self.assertEqual(COUNTS["medicaid_chip"]["n"], int(rows["Medicaid and CHIP"]["people"]))
        self.assertEqual((COUNTS["medicare"]["page"], COUNTS["medicaid_chip"]["page"]), (MEDICARE_PAGE, MEDICAID_PAGE))

    def test_counts_are_for_the_latest_marketplace_february(self):
        self.assertEqual(COUNTS["month"], f"{MK['feb'][-1]['y']}-02")

    def test_builder_refuses_another_month(self):
        with self.assertRaisesRegex(SystemExit, "the page compares 2027-02"):
            _load_builder().load_coverage_counts("2027-02")


class TestCoverageMixOnBothPages(TestCase):
    """The same donut on both pages, drawn by the view, with every share in words."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oe = _enroll(IN_SEASON)
        cls.dw = _page(IN_SEASON)
        cls.pages = {"open enrollment": _mix(cls.oe), "Denial Watch": _mix(cls.dw)}

    def test_renders_without_the_script(self):
        # The donut, its middle and the list are in the page itself; no script names them.
        for name, mix in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual(_donut(mix).count("<path "), len(MIX["rows"]))
                self.assertEqual(len(re.findall(r"<li[ >]", _between(mix, 'id="mix-list"', "</ul>"))), len(MIX["rows"]))
        for script in (OE_JS, (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()):
            self.assertNotIn("mix", script)

    def test_list_says_every_share_in_words_in_the_datas_order(self):
        for name, mix in self.pages.items():
            items = re.findall(r'<li[^>]*><i class="dw-mix-sw [\w-]+"></i><span>([^<]+)</span> <span class="dw-num">([^<]+)</span></li>', mix)
            self.assertEqual(items, [(r["label"], f"{r['pct']:.1f}%") for r in MIX["rows"]], name)

    def test_one_slice_per_row_with_the_marketplace_drawn_last(self):
        # Drawn last, the marketplace slice sits over its neighbours' edges; the list keeps the data's order.
        kinds = [_css(r["k"]) for r in MIX["rows"]]
        for name, mix in self.pages.items():
            with self.subTest(page=name):
                self.assertEqual([s["css"] for s in _slices(mix)], [k for k in kinds if k != "marketplace"] + ["marketplace"])
                self.assertEqual(re.findall(r'<i class="dw-mix-sw dw-mix-([\w-]+)"></i>', mix), kinds)

    def test_slice_spans_are_the_shares(self):
        for name, mix in self.pages.items():
            c, slices = _centre(mix), _by_kind(mix)
            for r in MIX["rows"]:
                s = slices[_css(r["k"])]
                with self.subTest(page=name, kind=r["k"]):
                    span = (_turn(s["end"], c) - _turn(s["start"], c)) % 100
                    self.assertAlmostEqual(span, r["pct"], delta=0.01)
                    self.assertEqual(s["big"], r["pct"] > 50)
                    # Straight edges in to the centre, and every corner on its ring.
                    self.assertAlmostEqual(_apart(_turn(s["start_in"], c), _turn(s["start"], c)), 0, delta=0.01)
                    self.assertAlmostEqual(_apart(_turn(s["end_in"], c), _turn(s["end"], c)), 0, delta=0.01)
                    for corner, radius in (("start", "outer"), ("end", "outer"), ("start_in", "inner"), ("end_in", "inner")):
                        self.assertAlmostEqual(math.dist(s[corner], (c, c)), s[radius], delta=0.01)

    def test_slices_follow_each_other_round_the_whole_circle(self):
        for name, mix in self.pages.items():
            c, slices = _centre(mix), _by_kind(mix)
            ring = [slices[_css(r["k"])] for r in MIX["rows"]]
            with self.subTest(page=name):
                for here, after in zip(ring, ring[1:] + ring[:1]):
                    self.assertAlmostEqual(_apart(_turn(here["end"], c), _turn(after["start"], c)), 0, delta=0.01, msg=(here["css"], after["css"]))
                self.assertAlmostEqual(sum((_turn(s["end"], c) - _turn(s["start"], c)) % 100 for s in ring), 100, delta=0.05)

    def test_marketplace_slice_starts_at_twelve_oclock(self):
        before = [r["k"] for r in MIX["rows"]][[r["k"] for r in MIX["rows"]].index("marketplace") - 1]
        for name, mix in self.pages.items():
            c, slices = _centre(mix), _by_kind(mix)
            marketplace = slices["marketplace"]
            with self.subTest(page=name):
                # Straight above the centre, on both rings, and the slice before it in the list ends there.
                self.assertEqual(marketplace["start"], (c, c - marketplace["outer"]))
                self.assertEqual(marketplace["start_in"], (c, c - marketplace["inner"]))
                self.assertAlmostEqual(_turn(marketplace["end"], c), _share("marketplace"), delta=0.01)
                self.assertEqual(slices[_css(before)]["end"], (c, c - slices[_css(before)]["outer"]))

    def test_marketplace_slice_stands_out_past_the_ring(self):
        for name, mix in self.pages.items():
            slices = _by_kind(mix)
            marketplace = slices.pop("marketplace")
            with self.subTest(page=name):
                # Every other slice is on one ring, and the marketplace slice reaches past it on both sides by more than the 2-unit gap.
                self.assertEqual(len({(s["outer"], s["inner"]) for s in slices.values()}), 1)
                ring = next(iter(slices.values()))
                self.assertGreater(marketplace["outer"] - ring["outer"], 2)
                self.assertGreater(ring["inner"] - marketplace["inner"], 2)

    def test_middle_says_the_marketplace_share_as_the_finding_does(self):
        in_100 = round(_share("marketplace"))
        for name, mix in self.pages.items():
            c = _centre(mix)
            texts = re.findall(r'<text class="([\w-]+)" x="([\d.]+)" y="[\d.]+">([^<]+)</text>', _donut(mix))
            with self.subTest(page=name):
                self.assertEqual(
                    [(cls, words) for cls, _, words in texts],
                    [("dw-mix-big", f"{in_100} in 100"), ("dw-mix-small", "have a"), ("dw-mix-small", "marketplace plan")],
                )
                self.assertEqual({float(x) for _, x, _ in texts}, {c})
                self.assertIn(f"About {in_100} in 100 have a marketplace plan.", _text(_between(mix, 'id="mix-finding"', "</p>")))
                self.assertIn('<li class="dw-mix-hl"><i class="dw-mix-sw dw-mix-marketplace"></i><span>Marketplace plan only</span>', mix)

    def test_ring_names_the_largest_slices_beside_a_tick_at_their_middle(self):
        named = {"job": "Job-based", "medicare": "Medicare", "medicaid_chip": "Medicaid or CHIP", "uninsured": "Uninsured"}
        label = re.compile(
            rf'<g class="dw-mix-ring-label"><line x1="{_N}" y1="{_N}" x2="{_N}" y2="{_N}"/><text x="{_N}" y="{_N}" text-anchor="(start|end|middle)">'
            rf'<tspan>([^<]+)</tspan><tspan class="dw-num" x="\5" dy="14">([^<]+)</tspan></text></g>'
        )
        for name, mix in self.pages.items():
            c, slices = _centre(mix), _by_kind(mix)
            found = label.findall(_donut(mix))
            with self.subTest(page=name):
                self.assertEqual(
                    [(words, pct) for *_, words, pct in found],
                    [(named[r["k"]], f"{r['pct']:.1f}%") for r in MIX["rows"] if r["k"] in named],
                )
                for x1, y1, x2, y2, x, _, anchor, words, _ in found:
                    s = slices[_css(next(k for k, v in named.items() if v == words))]
                    middle = _turn(s["start"], c) + ((_turn(s["end"], c) - _turn(s["start"], c)) % 100) / 2
                    inner, outer = (float(x1), float(y1)), (float(x2), float(y2))
                    # The tick starts just past the ring at the slice's middle and runs straight out.
                    self.assertTrue(s["outer"] < math.dist(inner, (c, c)) < math.dist(outer, (c, c)), words)
                    self.assertAlmostEqual(_apart(_turn(inner, c), middle), 0, delta=0.01, msg=words)
                    self.assertAlmostEqual(_apart(_turn(outer, c), middle), 0, delta=0.01, msg=words)
                    # The words sit on the side the slice faces, so they never cross the ring.
                    side = math.sin(2 * math.pi * middle / 100)
                    expected = "middle" if abs(side) <= 0.3 else ("start" if side > 0 else "end")
                    self.assertEqual(anchor, expected, words)
                    if anchor == "start":
                        self.assertGreater(float(x), float(x2), words)
                    elif anchor == "end":
                        self.assertLess(float(x), float(x2), words)

    def test_screen_readers_get_the_list_not_the_donut(self):
        for name, mix in self.pages.items():
            with self.subTest(page=name):
                self.assertIn('<svg class="dw-mix-donut" viewBox="0 0 200 200" aria-hidden="true" focusable="false">', mix)
                self.assertEqual(mix.count("<svg"), 1)
                self.assertNotIn("aria-hidden", _between(mix, 'id="mix-list"', "</ul>"))

    def test_finding_matches_the_tally(self):
        job, marketplace = _share("job"), _share("marketplace")
        self.assertTrue(45 <= job < 50, job)
        self.assertEqual(round(marketplace), 4)
        self.assertEqual(max(MIX["rows"], key=lambda r: r["pct"])["k"], "job")
        for name, mix in self.pages.items():
            self.assertIn(
                '<p id="mix-finding">Nearly half of people have coverage only through a job. About 4 in 100 have a marketplace plan.</p>', mix, name
            )

    def test_who_and_when_match_the_tally(self):
        self.assertTrue(MIX["period"].endswith("Feb-Apr 2026"))
        for mix in self.pages.values():
            self.assertIn("All ages, each person counted once, as of Census's interviews from February to April 2026.", _text(mix))

    def test_records_scope_and_source_notes_are_in_the_method_not_here(self):
        for name, mix in self.pages.items():
            for gone in ('id="mix-records"', 'id="mix-scope"', 'id="mix-source"', "CMS's records count", "Source:"):
                with self.subTest(page=name, gone=gone):
                    self.assertNotIn(gone, mix)

    def test_heading_fits_each_page(self):
        self.assertIn('<h2 id="coverage-mix-title">Where people get coverage</h2>', self.oe)
        self.assertIn('<h3 id="coverage-mix-title">Where people get coverage</h3>', self.dw)

    def test_sits_at_the_top_of_open_enrollment_and_in_denial_watchs_coverage_section(self):
        self.assertLess(self.oe.index('id="enroll-numbers"'), self.oe.index('id="coverage-mix"'))
        self.assertLess(self.oe.index('id="coverage-mix"'), self.oe.index('id="enroll-trends"'))
        self.assertEqual(_section_parents(self.oe)["coverage-mix"], None)
        self.assertEqual(_section_parents(self.dw)["coverage-mix"], "coverage")
        self.assertLess(self.dw.index('id="cmap"'), self.dw.index('id="coverage-mix"'))

    def test_denial_watch_links_the_enrollment_page_from_it(self):
        self.assertIn(
            f'<p id="coverage-marketplace">See marketplace coverage year by year on <a href="{reverse("open-enrollment")}">the open enrollment page</a>.</p>',
            self.pages["Denial Watch"],
        )
        self.assertNotIn('id="coverage-marketplace"', self.pages["open enrollment"])

    def test_how_link_opens_each_pages_method(self):
        for html, method in ((self.oe, "method-enroll"), (self.dw, "method-coverage")):
            self.assertIn(
                f'<p class="dw-how"><a href="#{method}">How we got these numbers<span class="fhi-visually-hidden">: Where people get coverage</span></a></p>',
                _mix(html),
            )
            group = _between(_between(html, 'id="sources"', "</details>"), f'id="{method}"', "</ol>")
            self.assertIn("<b>Where people get coverage:</b>", group)

    def test_stays_short(self):
        # The heading, finding, who and when, the donut's middle and ring labels, the list and the method link: about 95 words
        # as _words counts them (it runs the words either side of a tag together), and Denial Watch's line to the open
        # enrollment page: 11 more.
        for name, mix in self.pages.items():
            self.assertLessEqual(_words(mix), 115, name)

    def test_no_dashes(self):
        for mix in self.pages.values():
            for dash in ("\u2014", "\u2013"):
                self.assertNotIn(dash, mix)


class TestCoverageMixMethod(TestCase):
    """Both pages explain the tally, why this survey, how it differs from CMS's records, and what Denial Watch's rates cover."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oe, cls.dw = _enroll(IN_SEASON), _page(IN_SEASON)
        cls.groups = {
            "open enrollment": _between(cls.oe, 'id="method-enroll"', "</ol>"),
            "Denial Watch": _between(cls.dw, 'id="method-coverage"', "</ol>"),
        }

    def test_items_stay_together_in_order(self):
        for name, group in self.groups.items():
            labels = re.findall(r"<li[^>]*><b>([^<]+):</b>", group)
            first = labels.index("Where people get coverage")
            self.assertEqual(
                labels[first : first + 5],
                ["Where people get coverage", "Why this survey", "Surveys and records", "Medicare and Medicaid", "What Denial Watch's rates cover"],
                name,
            )

    def test_cites_the_survey_by_name(self):
        # The survey's full name and the public-use file it was tallied from, in the method's first item.
        for name, group in self.groups.items():
            self.assertIn(
                "FHI's tally of the U.S. Census Bureau's Current Population Survey, 2026 Annual Social and Economic Supplement, from its public-use file",
                _text(group),
                name,
            )
            self.assertIn(f'<a href="{SURVEY_PAGE}">Current Population Survey, 2026 Annual Social and Economic Supplement</a>', group)

    def test_records_open_surveys_and_records(self):
        feb = MK["feb"][-1]
        records = (
            f"Surveys and records: CMS's records count {_millions(feb['n'])} people with marketplace coverage in February {feb['y']}, "
            f"{_millions(COUNTS['medicare']['n'])} on Medicare and {_millions(COUNTS['medicaid_chip']['n'])} on Medicaid or CHIP. "
            "Only surveys count job-based coverage and the uninsured. "
        )
        for name, group in self.groups.items():
            item = _text(_between(group, "<b>Surveys and records:</b>", "</li>"))
            self.assertEqual(item[: len(records)], records, name)
            # Some people are in both of CMS's counts, as the next item says.
            self.assertIn("People with both Medicare and Medicaid are in both counts.", _text(group))

    def test_scope_matches_denial_watch(self):
        # The every-state chart's grey states with a rate: their own reports, or CMS's last year with them.
        self.assertEqual(len(DATA["state_reports"]) + len(DATA["earlier"]), 5)
        # Prior authorization covers Medicare Advantage, Medicaid plans, state Medicaid and HealthCare.gov insurers, nothing job-based.
        self.assertEqual({r["line"] for r in DATA["prior_auth"]}, {"ma", "mcd", "mkt", "ffs"})
        rest = (
            "claim denial rates are for marketplace plans in HealthCare.gov states and five states that run their own marketplace, where some figures also count plans sold outside it. "
            "Its prior authorization rates cover large Medicare Advantage plans, Medicaid plans and programs, and HealthCare.gov insurers. "
            "It has no denial rates for job-based plans or Original Medicare."
        )
        for name, whose in (("open enrollment", "Denial Watch's"), ("Denial Watch", "This page's")):
            scope = _text(_between(self.groups[name], '<li id="mix-scope">', "</li>"))
            self.assertEqual(scope, f"What Denial Watch's rates cover: {whose} " + rest, name)

    def test_scope_links_land_on_denial_watchs_sections(self):
        dw = reverse("denial-watch")
        for name, html, prefix in (("open enrollment", self.oe, dw), ("Denial Watch", self.dw, "")):
            scope = _between(self.groups[name], '<li id="mix-scope">', "</li>")
            self.assertEqual(re.findall(r'href="([^"]+)"', scope), [f"{prefix}#map-denials", f"{prefix}#prior-auth"], name)
            self.assertEqual(html.count('id="mix-scope"'), 1, name)
        for target in ("map-denials", "prior-auth"):
            self.assertIn(f'id="{target}"', self.dw)

    def test_says_how_each_person_is_counted_once(self):
        for name, group in self.groups.items():
            text = _text(group)
            self.assertIn(
                "Each person is counted once: anyone with Medicare under Medicare, anyone else with exactly one kind of coverage under that kind, "
                'and anyone else with two or more kinds under "more than one kind".',
                text,
                name,
            )
            self.assertIn("as of their interviews from February to April 2026", text)
            self.assertIn("Our tally matches the totals and margins of error Census publishes from the same survey", text)
            self.assertIn(f'<a href="{SURVEY_PAGE}">', group)
            self.assertIn('<a href="https://www.census.gov/library/publications/2026/demo/p60-291.html">report P60-291</a>', group)

    def test_names_what_the_kinds_include(self):
        for group in self.groups.values():
            text = _text(group)
            self.assertIn("Medicare includes Medicare Advantage plans, which private insurers run.", text)
            self.assertIn("Job-based includes self-funded plans, where the employer pays the claims itself.", text)
            self.assertIn("Basic Health Programs, such as New York's Essential Plan.", text)

    def test_says_why_this_survey(self):
        for group in self.groups.values():
            text = _text(group)
            self.assertIn("Census's American Community Survey has no marketplace category", text)
            self.assertIn("so it warns against comparing 2025 with earlier years", text)
            self.assertIn("add up to more than 100%, since people can have more than one kind", text)
            self.assertIn("its latest figures are for 2025 and its marketplace figures are for people under 65", text)

    def test_says_why_it_differs_from_cms_in_neutral_words(self):
        for group in self.groups.values():
            text = _text(group)
            self.assertIn("Census's survey finds fewer people with marketplace plans than CMS's records count.", text)
            self.assertIn("CMS counts plans in effect in February, the survey asks from February to April, and some coverage ends in between.", text)
            for word in ("undercount", "said", "because", "miss"):
                self.assertNotIn(word, text.lower())

    def test_names_the_record_sources(self):
        for group in self.groups.values():
            for href in (MEDICARE_PAGE, MEDICAID_PAGE):
                self.assertIn(f'href="{href}"', group)
            self.assertIn("the 50 states and DC added up; it includes Medicare Advantage.", _text(group))

    def test_every_survey_link_is_named_on_denial_watch(self):
        # Denial Watch names every source in the bundle but the marketplace and population files, which the open enrollment page names.
        for url in (MIX["page"], MEDICARE_PAGE, MEDICAID_PAGE):
            self.assertIn(f'href="{url}"', self.groups["Denial Watch"])

    def test_no_dashes(self):
        for group in self.groups.values():
            for dash in ("\u2014", "\u2013"):
                self.assertNotIn(dash, group)


class TestOpenEnrollmentPopulation(TestCase):
    """Marketplace coverage beside the people under 65, each as a change since the first February, on one axis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.section = _between(cls.content, 'id="enroll-population"', "</section>")
        method = _between(cls.content, 'id="method-enroll"', "</ol>")
        cls.method_html = _between(method, "<b>Marketplace coverage and people under 65:</b>", "</li>")
        cls.method = _text(cls.method_html)
        cls.script = _between(OE_JS, "/* coverage and people under 65:", "})();")

    def _changes(self):
        u, feb = _under_65(), MK["feb"]
        return {r["y"]: (r["n"] / feb[0]["n"] - 1, u[r["y"] - 1] / u[feb[0]["y"] - 1] - 1) for r in feb}

    def test_sits_in_the_trends_after_the_two_charts(self):
        self.assertEqual(_section_parents(self.content)["enroll-population"], "enroll-trends")
        self.assertLess(self.content.index('id="pktable"'), self.content.index('id="enroll-population"'))
        self.assertLess(self.content.index('id="enroll-population"'), self.content.index('id="enroll-act"'))
        self.assertIn('<h3 class="fhi-card-title" id="pop-title">Marketplace coverage and people under 65, change since 2016</h3>', self.section)

    def test_finding_matches_the_data(self):
        changes, u, feb = self._changes(), _under_65(), {r["y"]: r["n"] for r in MK["feb"]}
        first, peak, last = MK["feb"][0]["y"], max(feb, key=feb.get), MK["feb"][-1]["y"]
        self.assertEqual((first, peak, last), (2016, 2025, 2026))
        self.assertTrue(1 < changes[peak][0] < 2, changes[peak])
        self.assertEqual(round(changes[peak][1] * 100), 1)
        self.assertEqual(round(100 * feb[last] / u[last - 1]), 7)
        self.assertEqual(round(100 * feb[first] / u[first - 1]), 4)
        self.assertEqual(
            _text(_between(self.section, 'id="popnote"', "</p>")).split(">", 1)[-1],
            "Marketplace coverage more than doubled by February 2025, while the number of people under 65 rose about 1%. "
            "In February 2026 it came to about 7 for every 100 people under 65, up from about 4 in 2016.",
        )

    def test_finding_and_note_stay_short(self):
        prose = _text(_between(self.section, 'id="popnote"', "</p>")) + " " + _text(_between(self.section, 'id="popcaveat"', "</p>"))
        self.assertLessEqual(len(prose.split()), 65, prose)

    def test_note_says_why_everyone_under_65(self):
        self.assertIn(
            "No public count exists of everyone who could buy a marketplace plan, so this compares with everyone under 65, "
            "who these plans are mainly for.",
            _text(self.section),
        )

    def test_chart_label_before_the_script_runs_gives_both_changes(self):
        changes, last = self._changes(), MK["feb"][-1]["y"]
        self.assertIn(
            f'aria-label="Line chart of the change since February {MK["feb"][0]["y"]} in people with marketplace coverage, '
            f'+{changes[last][0] * 100:.1f}% by February {last} (preliminary), and in people under 65, +{changes[last][1] * 100:.1f}%"',
            self.section,
        )

    def test_year_table_is_drawn_by_the_view_from_the_bundle(self):
        u, feb, prelim = _under_65(), MK["feb"], MK["prelim"]
        rows = _table_rows(_between(self.section, '<table class="dw-table" id="poptable">', "</table>"))
        expected = [["February", "Marketplace coverage", "People under 65, July 1 before", "Coverage per 100 under 65"]]
        for i, r in enumerate(feb):
            pop = u[r["y"] - 1]
            n_chg = f" (+{(r['n'] / feb[0]['n'] - 1) * 100:.1f}%)" if i else ""
            pop_chg = f" (+{(pop / u[feb[0]['y'] - 1] - 1) * 100:.1f}%)" if i else ""
            expected.append([
                f"{r['y']} (preliminary)" if r["y"] == prelim else str(r["y"]),
                f"{r['n']:,}{n_chg}",
                f"{pop:,}{pop_chg}",
                f"{100 * r['n'] / pop:.1f}",
            ])
        self.assertEqual(rows, expected)
        self.assertNotIn("poptable", OE_JS)

    def test_script_draws_both_lines_on_one_axis_from_the_data(self):
        call = re.search(r"line\(\$\('popline'\),.*?\{ymin:([\d.]+),ymax:([\d.]+),ticks:\[([^\]]+)\]", self.script)
        self.assertIsNotNone(call)
        ymin, ymax, ticks = float(call.group(1)), float(call.group(2)), [float(t) for t in call.group(3).split(",")]
        self.assertEqual((ymin, ticks[0], ticks[-1]), (0, 0, ymax))
        # One axis: both lines are a change since the first February, and every point fits it.
        for y, pair in self._changes().items():
            for v in pair:
                self.assertTrue(ymin <= v <= ymax, (y, v))
        self.assertEqual(self.script.count("ticks:"), 1)
        self.assertIn("F.map(r=>({x:r.y,y:r.n/f0-1}))", self.script)
        self.assertIn("also:[{pts:F.map(r=>({x:r.y,y:U.get(r.y-1)/u0-1}))", self.script)
        self.assertIn("color:tok('--dw-count'),", self.script)
        self.assertIn("color:tok('--dw-count-deep'),label:y=>'Under 65 '+chg(y),dash:'8 5'}", self.script)
        self.assertIn("open:p=>p.x===MK.prelim", self.script)

    def test_population_is_the_july_before_each_february(self):
        self.assertIn("U.get(r.y-1)", self.script)
        self.assertIn("u0=U.get(F[0].y-1)", self.script)

    def test_shared_chart_draws_a_second_line_dashed_under_the_first(self):
        charts = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch_charts.js").read_text()
        self.assertIn("for(const s of also)series(s.pts,s);\n  series(pts,{color,ink,label,open,dash});", charts)
        self.assertIn('${dash?` stroke-dasharray="${dash}"`:\'\'}', charts)

    def test_key_names_both_lines_without_colour(self):
        key = _between(self.section, '<p class="dw-key" id="popkey">', "</p>")
        self.assertEqual(
            re.findall(r'<i class="dw-sw ([\w-]+)"></i>([^<]+)', key),
            [("dw-sw-count", "Marketplace coverage"), ("dw-sw-dash", "People under 65, dashed"), ("dw-sw-open", f"{MK['prelim']}, preliminary")],
        )
        self.assertIn(".dw-sw-dash { height: 4px; border-radius: 0; background: repeating-linear-gradient(90deg, var(--dw-count-deep) 0 7px, var(--dw-paper) 7px 11px); }", STYLE)

    def test_embeds_the_population_with_the_marketplace(self):
        self.assertIn('"population": {"under_65": [', self.content)
        self.assertIn("const U=D.population&&new Map(D.population.under_65.map(r=>[r.y,r.n]));", self.script)

    def test_method_names_the_census_files_and_the_caveats(self):
        picks = {p["y"]: p["n"] for p in MK["picks"]}
        join = JOIN_V2025 / JOIN_INTERCENSAL - 1
        self.assertEqual(f"{join * 100:.2f}", "0.07")
        for href in (INTERCENSAL, VINTAGE_2025):
            self.assertIn(f'href="{href}"', self.method_html)
        for words in (
            "for July 1 of the year before each February",
            "(nc-est2020int-agesex-res.csv) for 2015 to 2019",
            "(nc-est2025-agesex-res.csv) for 2020 to 2025",
            f"Where the two join, in April 2020, the newer series counts {join * 100:.2f}% more people under 65.",
            "Census revises every year from 2020 on with each new set of estimates; the next estimates by age are due in April 2027.",
            f"CMS's count is for all ages: about {OEP_2025_65_PLUS / picks[2025] * 100:.1f}% of 2025's plan picks were by people 65 or older.",
            f"CMS's 2025 open enrollment file counts about {_millions(OEP_2025_BASIC_HEALTH)} people in them.",
            "February 2026 is CMS's first count for that month and may change.",
        ):
            self.assertIn(words, self.method)

    def test_population_sources_are_in_the_bundle(self):
        urls = {s["url"]: s["years"] for s in DATA["sources"]}
        self.assertEqual((urls[INTERCENSAL], urls[VINTAGE_2025]), ("July 2015-2019", "July 2020-2025"))

    def test_no_dashes(self):
        for part in (self.section, self.method_html, self.script):
            for dash in ("\u2014", "\u2013"):
                self.assertNotIn(dash, part)


def _kind_colours() -> dict[str, tuple[str, str]]:
    """Each kind's slice fill and legend swatch colour, by its class, from the style partial; a striped kind's are "stripes"."""
    # One pass in stylesheet order, so a later rule for a class wins as it does in CSS.
    found = {}
    for rule in re.finditer(
        r"^  (?P<sel>\.dw-mix-[\w-]+(?:, \.dw-mix-[\w-]+)*) \{ (?:fill: var\((?P<fill>--dw-[\w-]+)\); background-color: var\((?P<swatch>--dw-[\w-]+)\);"
        r"|fill: url\(#dw-mix-stripes\); background: " + re.escape(STRIPES_SWATCH) + r"; \})",
        STYLE,
        re.M,
    ):
        colours = (rule["fill"], rule["swatch"]) if rule["fill"] else ("stripes", "stripes")
        for selector in rule["sel"].split(", "):
            found[selector.removeprefix(".dw-mix-")] = colours
    return found


# The legend swatch draws the same stripes as the slice's pattern: health-3 lines on health-1.
STRIPES_SWATCH = "repeating-linear-gradient(135deg, var(--dw-health-3) 0 2px, var(--dw-health-1) 2px 5px)"


class TestCoverageMixColours(TestCase):
    """Coverage purples, the marketplace slice the darkest and set apart by more than colour, and nothing grey, red, green or blue."""

    block = _between(STYLE, "  /* Where people get coverage", "  @media (max-width: 860px)")

    def test_slices_are_coverage_purples(self):
        self.assertEqual(set(re.findall(r"var\((--dw-[\w-]+)\)", self.block)) - {"--dw-paper"}, {"--dw-health-1", "--dw-health-3", "--dw-health-5"})
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", self.block))

    def test_each_kind_has_one_colour_for_its_slice_and_its_swatch(self):
        # The page gives a kind's slice and its swatch in the list the same class (TestCoverageMixOnBothPages), and the class one colour.
        colours = _kind_colours()
        self.assertEqual(set(colours), {_css(r["k"]) for r in MIX["rows"]})
        for kind, (fill, swatch) in colours.items():
            self.assertEqual(fill, swatch, kind)
        self.assertIn(".dw-mix-sw { width: 1rem; height: 0.75rem; border-radius: 2px; align-self: center; }", STYLE)

    def test_marketplace_slice_is_the_darkest_and_solid_kinds_two_steps_apart(self):
        # One step apart, two light purples can't be told apart even with full colour vision; two steps apart they can.
        colours = _kind_colours()
        step = {kind: int(fill.rsplit("-", 1)[1]) for kind, (fill, _) in colours.items() if fill.startswith("--dw-health-")}
        self.assertEqual(set(step), {"marketplace", "medicare", "job"})
        self.assertGreater(step["marketplace"], max(v for k, v in step.items() if k != "marketplace"))
        steps = sorted(step.values())
        for lighter, darker in zip(steps, steps[1:]):
            self.assertGreaterEqual(darker - lighter, 2)
        # Medicaid and the three smallest kinds are striped, so they differ from every solid kind by more than shade.
        for kind in ("medicaid-chip", "other-individual", "military-va", "more-than-one"):
            self.assertEqual(colours[kind], ("stripes", "stripes"), kind)

    def test_the_stripes_pattern_matches_the_swatch(self):
        self.assertIn(".dw-mix-stripe-ground { fill: var(--dw-health-1); }", STYLE)
        self.assertIn(".dw-mix-stripe { fill: var(--dw-health-3); }", STYLE)
        partial = (REPO / "fighthealthinsurance" / "templates" / "partials" / "coverage_mix.html").read_text()
        self.assertIn('<pattern id="dw-mix-stripes" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect class="dw-mix-stripe-ground" width="5" height="5"/><rect class="dw-mix-stripe" width="2" height="5"/></pattern>', partial)

    def test_ring_labels_are_ink_and_leave_on_a_phone(self):
        self.assertIn(".dw-mix-ring-label line { stroke: var(--fhi-ink-2); stroke-width: 1; }", STYLE)
        self.assertIn(".dw-mix-ring-label text { font-size: 12px; fill: var(--fhi-ink-2); }", STYLE)
        # A text-anchor set in CSS would override each label's own side.
        self.assertNotRegex(STYLE, r"\.dw-mix-donut text \{[^}]*text-anchor")
        phone = _between(STYLE, "  @media (max-width: 560px) {\n    .dw-mix {", "  }")
        self.assertIn(".dw-mix-ring-label { display: none; }", phone)

    def test_marketplace_slice_and_the_empty_ring_stand_out_from_the_white_gaps(self):
        # The 2-unit gaps are what the marketplace slice stands out past (TestCoverageMixOnBothPages), so both edges are checked marks.
        self.assertIn(".dw-mix-donut path { stroke: var(--dw-paper); stroke-width: 2;", STYLE)
        ring = re.search(r"\.dw-mix-donut path\.dw-mix-uninsured \{ stroke: var\((--dw-[\w-]+)\); \}", STYLE)
        self.assertIsNotNone(ring)
        marks = {(ink, ground) for ink, ground, minimum, _ in DW_PAIRS if minimum >= 3.0}
        self.assertIn((_kind_colours()["marketplace"][0], "--dw-paper"), marks)
        self.assertIn((ring.group(1), "--dw-paper"), marks)

    def test_marketplace_row_is_set_apart_in_the_list_too(self):
        self.assertIn(".dw-mix-hl { font-weight: 700; color: var(--fhi-ink); }", STYLE)
        self.assertIn(".dw-mix-hl .dw-mix-sw { box-shadow: 0 0 0 2px var(--fhi-ink); }", STYLE)

    def test_no_coverage_is_an_empty_outlined_slice_not_grey(self):
        self.assertIn(".dw-mix-uninsured { fill: var(--dw-paper); background-color: var(--dw-paper); box-shadow: inset 0 0 0 2px var(--dw-health-3); }", STYLE)
        self.assertIn(".dw-mix-donut path.dw-mix-uninsured { stroke: var(--dw-health-3); }", STYLE)

    def test_the_light_job_slice_is_outlined(self):
        # health-1 is under 3:1 on white, so the slice's edge is drawn in health-3 (measured in test_contrast).
        self.assertEqual(_kind_colours()["job"], ("--dw-health-1", "--dw-health-1"))
        self.assertIn(".dw-mix-donut path.dw-mix-job { stroke: var(--dw-health-3); }", STYLE)

    def test_no_grey_red_green_or_denial_blue(self):
        for token in ("--dw-na", "--dw-bad", "--dw-good", "--dw-deny", "--dw-count"):
            self.assertNotIn(token, self.block)

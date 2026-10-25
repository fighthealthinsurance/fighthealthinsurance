"""Tests for the open enrollment page, split out of Denial Watch.

It has the season's dates and steps, the marketplace numbers and the
enrollment questions, from Denial Watch's bundle (static/denial_watch.json)
and behind the same DENIAL_WATCH_PAGE_ENABLED flag. Some of the numbers are
written into its sentences, so these tests check the copy still agrees with
the data after a refresh, and pin the date wherever the season matters.
"""

import json
import re
from datetime import date, datetime, timezone as dt_timezone
from html.parser import HTMLParser
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from tests.sync.test_denial_watch import (
    CHARTS_JS,
    CMS_EFFECTUATED,
    DATA,
    EARLY_FEB_2025,
    JS,
    MARKETPLACE_SOURCES,
    MK,
    REPO,
    _between,
    _census_year,
    _february,
    _millions,
    _page,
    _pct,
    _picks,
    _short,
    _text,
    _without_data,
)

OE_JS = (REPO / "fighthealthinsurance" / "static" / "js" / "open_enrollment.js").read_text()
TEMPLATES = REPO / "fighthealthinsurance" / "templates"

KFF_MARKET = "https://www.kff.org/affordable-care-act/individual-market-enrollment-continues-to-fall-through-mid-2026/"
KFF_SURVEY = "https://www.kff.org/public-opinion/a-follow-up-survey-of-aca-marketplace-enrollees/"
KFF_SENTENCE = "Enrollment in the individual health insurance market fell 18% between June 2025 and June 2026."
CMS_PREMIUM = "plan selections will only become coverage for consumers that effectuate their coverage by paying their first monthly premium"
KFF_MARKET_DATE = "October 14, 2026"
KFF_JUNE = (25.2, 20.5)  # millions, June 2025 and June 2026, from the KFF analysis

IN_SEASON, AFTER_SEASON = date(2026, 10, 18), date(2027, 1, 16)


def _enroll(day: date | None = None) -> str:
    """The rendered open enrollment page, with today pinned when a day is given."""
    if day is None:
        return Client().get(reverse("open-enrollment")).content.decode()
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        return Client().get(reverse("open-enrollment")).content.decode()


def _top(html: str) -> str:
    """The top of the page: the dates line, tiles, chart, steps and their notes."""
    return _between(html, '<div class="fhi-stack" id="enroll">', 'id="enroll-questions"')


def _questions(html: str) -> str:
    return _between(html, 'id="enroll-questions"', "</section>")


def _body(html: str) -> str:
    return _between(html, '<div class="fhi-page-wide dw"', '<script id="dw-data"')


def _words(html: str) -> int:
    return len(_text(html).split())


def _table_rows(html: str) -> list[list[str]]:
    """Each row's cell text, header cells included, spaces collapsed."""

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self.cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.rows.append([])
            elif tag in ("th", "td"):
                self.cell = []

        def handle_endtag(self, tag):
            if tag in ("th", "td") and self.cell is not None:
                self.rows[-1].append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
                self.cell = None

        def handle_data(self, data):
            if self.cell is not None:
                self.cell.append(data)

    parser = Rows()
    parser.feed(html)
    return parser.rows


def _section_parents(html: str) -> dict[str | None, str | None]:
    """Each section's id and the id of the section it sits inside (None at the top)."""

    class Parents(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str | None] = []
            self.of: dict[str | None, str | None] = {}

        def handle_starttag(self, tag, attrs):
            if tag == "section":
                sid = dict(attrs).get("id")
                self.of[sid] = self.stack[-1] if self.stack else None
                self.stack.append(sid)

        def handle_endtag(self, tag):
            if tag == "section":
                self.stack.pop()

    parser = Parents()
    parser.feed(html)
    return parser.of


def _yearly_changes() -> dict[int, float]:
    feb = MK["feb"]
    return {b["y"]: b["n"] / a["n"] - 1 for a, b in zip(feb, feb[1:])}


class TestOpenEnrollmentPage(TestCase):
    """The page serves like the site's other public pages, with only the numbers it draws."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        response = Client().get(reverse("open-enrollment"))
        cls.status_code = response.status_code
        cls.content = _enroll(IN_SEASON)

    def test_page_renders(self):
        self.assertEqual(self.status_code, 200)

    def test_address_has_no_year_in_it(self):
        # One address every fall; the page says which year it is for.
        self.assertEqual(reverse("open-enrollment"), "/open-enrollment")

    def test_title_description_and_canonical_tags(self):
        self.assertIn("<title>Open Enrollment for 2027: Dates, Numbers and What to Do</title>", self.content)
        self.assertIn('content="Open Enrollment for 2027 - Fight Health Insurance"', self.content)
        self.assertRegex(
            self.content,
            r'<meta name="description"\s+content="How many people pick a marketplace plan and how many have coverage, from CMS\'s counts, '
            r'with the steps for signing up and answers to common questions.">',
        )
        self.assertIn('<link rel="canonical" href="https://www.fighthealthinsurance.com/open-enrollment">', self.content)

    def test_title_out_of_season(self):
        html = _enroll(AFTER_SEASON)
        self.assertIn("<title>Signing Up for Marketplace Coverage: Numbers and What to Do</title>", html)
        self.assertIn('content="Signing Up for Marketplace Coverage - Fight Health Insurance"', html)

    def test_page_opens_with_the_title_block(self):
        self.assertEqual(re.findall(r"<h1\b[^>]*>(.*?)</h1>", self.content), ["Open enrollment for 2027"])
        self.assertRegex(self.content, r'<header class="fhi-page-title">\s*<h1>Open enrollment for 2027</h1>\s*</header>')

    def test_page_sits_on_the_wide_column_in_denial_watchs_style(self):
        self.assertIn('class="fhi-page-wide dw"', self.content)
        # One copy of the shared styles, the same block on both pages.
        shared = _between(self.content, "<style>", "</style>")
        self.assertIn(".dw-stat {", shared)
        self.assertEqual(shared, _between(_page(IN_SEASON), "<style>", "</style>"))
        self.assertNotIn(".dw-ticker", self.content)
        self.assertNotIn("denial_watch.css", self.content)

    def test_embeds_only_the_marketplace_numbers(self):
        embedded = json.loads(_between(self.content, '<script id="dw-data" type="application/json">', "</script>").split(">", 1)[1])
        self.assertEqual(embedded, {"marketplace": DATA["marketplace"]})

    def test_loads_the_shared_chart_script_then_its_own(self):
        shared = self.content.index("js/denial_watch_charts.js")
        self.assertLess(shared, self.content.index("js/open_enrollment.js"))
        self.assertNotIn("js/denial_watch.js", self.content)
        dw = _page(IN_SEASON)
        self.assertLess(dw.index("js/denial_watch_charts.js"), dw.index("js/denial_watch.js"))

    def test_both_pages_draw_with_one_copy_of_the_chart_code(self):
        self.assertEqual(CHARTS_JS.count("function line("), 1)
        for script in (JS, OE_JS):
            for own in ("function line(", "const openTo=", "const fmt=", "const pct="):
                self.assertNotIn(own, script)
            self.assertIn("const {foldLinks,fmt,pct,tok,line}=window.DWCharts;", script)

    def test_page_names_the_marketplace_sources(self):
        visible = _without_data(self.content)
        for url in MARKETPLACE_SOURCES:
            self.assertIn(f'href="{url}"', visible)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_page_serves_404_when_denial_watch_is_off(self):
        self.assertEqual(Client().get(reverse("open-enrollment")).status_code, 404)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_options_does_not_reveal_the_route_when_off(self):
        self.assertEqual(Client().options(reverse("open-enrollment")).status_code, 404)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_page_is_in_the_sitemap_when_on(self):
        sitemap = Client().get("/sitemap.xml").content.decode()
        self.assertIn(f"{reverse('open-enrollment')}</loc>", sitemap)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_page_is_hidden_from_the_sitemap_when_off(self):
        sitemap = Client().get("/sitemap.xml").content.decode()
        self.assertNotIn(reverse("open-enrollment"), sitemap)


class TestOpenEnrollmentIsLinked(TestCase):
    """Denial Watch, the coverage guide's fall checklist and, in season, the menu link to the page."""

    def setUp(self):
        self.href = f'href="{reverse("open-enrollment")}"'

    def test_denial_watch_card_links_it_in_season(self):
        card = _between(_page(IN_SEASON), 'id="enroll"', "</section>")
        self.assertIn(f"<a {self.href}>Open enrollment: dates, numbers and what to do</a>", card)

    def test_denial_watch_links_it_out_of_season(self):
        line = _between(_page(AFTER_SEASON), 'id="enroll"', "</p>")
        self.assertIn(f"<a {self.href}>Signing up for marketplace coverage: numbers and what to do</a>", line)

    def test_denial_watch_coverage_section_links_it(self):
        coverage = _between(_page(), 'id="coverage"', "</section>")
        self.assertIn(self.href, coverage)

    def test_coverage_changes_fall_checklist_links_it_by_the_season(self):
        # The same label as Denial Watch's link, so it matches the page's title in and out of season.
        for day, line in (
            (IN_SEASON, f"For the season in short, with the marketplace numbers, see <a {self.href}>Open enrollment: dates, numbers and what to do</a>."),
            (AFTER_SEASON, f"For the marketplace numbers, see <a {self.href}>Signing up for marketplace coverage: numbers and what to do</a>."),
        ):
            with self.subTest(day=day), mock.patch("django.utils.timezone.localdate", return_value=day):
                html = Client().get(reverse("coverage-changes")).content.decode()
                start = html.index('<section id="fall-checklist"')
                checklist = html[start : html.index("</section>", start)]
                self.assertIn(f'<p id="open-enrollment-link">{line}</p>', checklist)

    def test_menu_links_it_in_season_only(self):
        # Under Resources, beside the coverage guide, while Denial Watch shows its card.
        item = f"<li><a {self.href}>Open Enrollment</a></li>"
        for day, shown in ((IN_SEASON, True), (date(2027, 1, 15), True), (AFTER_SEASON, False), (date(2026, 10, 14), False)):
            with self.subTest(day=day), mock.patch("django.utils.timezone.localdate", return_value=day):
                about = Client().get(reverse("about")).content.decode()
                self.assertEqual(item in about, shown)
                if shown:
                    menu = _between(about, "<summary class=\"fhi-nav-link\">Resources</summary>", "</ul>")
                    self.assertLess(menu.index(f'href="{reverse("coverage-changes")}"'), menu.index(item))

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_nothing_links_to_it_before_launch(self):
        with mock.patch("django.utils.timezone.localdate", return_value=IN_SEASON):
            for name in ("root", "about", "coverage-changes"):
                html = Client().get(reverse(name)).content.decode()
                self.assertNotIn(self.href, html, name)


class TestOldDenialWatchLinksHandOff(TestCase):
    """Links shared to the enrollment parts Denial Watch used to have still reach them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        block = re.search(r"const MOVED=new Map\(\[(.*?)\]\);", JS)
        cls.moved = dict(re.findall(r"\['([\w-]+)','([\w-]+)'\]", block.group(1))) if block else {}

    def test_every_moved_part_is_handed_off(self):
        # What main served on Denial Watch and now lives on the open enrollment page, or was folded into its top.
        self.assertEqual(
            self.moved,
            {
                "marketplace-trend": "enroll",
                "marketplace-trend-title": "enroll",
                **{i: i for i in ("lost-coverage", "enroll-renewing", "denials-bridge", "mkline", "mknote", "mktable", "mkpicks")},
            },
        )

    def test_each_old_link_is_gone_from_denial_watch_and_its_target_is_on_the_new_page(self):
        for day in (IN_SEASON, AFTER_SEASON):
            dw, oe = _page(day), _enroll(day)
            for old, new in self.moved.items():
                with self.subTest(day=day, old=old):
                    self.assertNotIn(f'id="{old}"', dw)
                    self.assertIn(f'id="{new}"', oe)

    def test_script_hands_off_before_reading_the_data_to_the_open_enrollment_url(self):
        self.assertLess(JS.index("location.replace(OE_URL+'#'+MOVED_TO)"), JS.index("JSON.parse"))
        script = f'<script src="/static/js/denial_watch.js" data-open-enrollment="{reverse("open-enrollment")}" defer></script>'
        self.assertIn(script, _page())


class TestOpenEnrollmentSeason(TestCase):
    """The season's words show only in season; tests pin the date."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fighthealthinsurance.views import denial_watch_enrollment

        cls.season = staticmethod(denial_watch_enrollment)

    def test_medicare_line_runs_october_15_to_december_7(self):
        self.assertTrue(self.season(date(2026, 10, 15))["medicare"])
        self.assertTrue(self.season(date(2026, 12, 7))["medicare"])

    def test_season_stays_for_healthcare_gov_after_medicare_closes(self):
        self.assertFalse(self.season(date(2026, 12, 8))["medicare"])
        self.assertFalse(self.season(date(2027, 1, 15))["medicare"])

    def test_season_is_for_next_years_plans(self):
        season = self.season(IN_SEASON)
        self.assertEqual((season["starts"], season["ends"], season["plan_year"]), (date(2026, 10, 15), date(2026, 12, 7), 2027))

    def test_season_is_none_outside_it(self):
        self.assertIsNone(self.season(date(2026, 10, 14)))
        self.assertIsNone(self.season(AFTER_SEASON))

    def test_page_shows_the_season_in_season(self):
        html = _enroll(IN_SEASON)
        top = _top(html)
        self.assertIn("<h1>Open enrollment for 2027</h1>", html)
        self.assertIn("Medicare open enrollment runs from October 15 to December 7, 2026", _text(top))
        self.assertIn('href="https://www.medicare.gov/plan-compare/"', top)
        self.assertIn('href="https://www.healthcare.gov/"', top)
        # The insurer rates and prior authorization are Denial Watch's.
        self.assertIn(f'href="{reverse("denial-watch")}#insurers"', top)
        self.assertIn(f'href="{reverse("denial-watch")}#prior-auth"', top)

    def test_page_prints_no_healthcare_gov_dates(self):
        # CMS set the 2027 marketplace dates, but the court ruling behind them is under appeal, so the page links out.
        html = _enroll(IN_SEASON)
        for part in (_top(html), _questions(html)):
            for day in ("November 1", "December 15", "January 15"):
                self.assertNotIn(day, part)

    def test_dates_question_says_why_it_links_out(self):
        dates = _between(_enroll(IN_SEASON), 'id="enroll-dates"', "</details>")
        self.assertIn(
            "When can I sign up for a 2027 HealthCare.gov plan? CMS has set HealthCare.gov's 2027 sign-up dates, "
            "but the court ruling behind them is under appeal, so check HealthCare.gov's dates before you count on a deadline.",
            _text(dates),
        )
        self.assertIn('href="https://www.healthcare.gov/quick-guide/dates-and-deadlines/"', dates)

    def test_method_note_says_cms_set_the_dates_and_the_ruling_is_under_appeal(self):
        html = _enroll(IN_SEASON)
        note = _between(html, "<b>Open enrollment:</b>", "</li>")
        self.assertIn("CMS has set HealthCare.gov's 2027 sign-up dates", _text(note))
        self.assertIn("but the court ruling behind them is under appeal, so we link to HealthCare.gov rather than print them. Checked October 22, 2026.", _text(note))
        self.assertIn('href="https://www.cms.gov/files/document/ftr-requirement-py26-27-statement.pdf"', note)
        for page in (html, _page(IN_SEASON)):
            for stale in ("in a court case", "are in court"):
                self.assertNotIn(stale, _without_data(page))

    def test_oregon_signs_up_at_its_own_marketplace(self):
        # From November 1, 2026, Oregon enrolls through ExploreHealthOR.gov, not HealthCare.gov.
        html = _enroll(IN_SEASON)
        link = '<a href="https://explorehealthor.gov/">ExploreHealthOR.gov</a>'
        dates = _between(html, 'id="enroll-dates"', "</details>")
        self.assertIn(f"Oregon now has its own marketplace: for 2027 plans, Oregon residents sign up at {link}.", dates)
        sources = _between(html, 'id="sources"', "</section>")
        self.assertIn('href="https://www.healthcare.gov/marketplace-in-your-state/"', sources)

    def test_shoppers_hear_that_some_insurers_have_left(self):
        # The insurer table is 2024 data, and some of its insurers don't sell 2027 plans; no insurer is named.
        html = _enroll(IN_SEASON)
        top = _text(_top(html))
        # The table only has HealthCare.gov insurers, so the pointer says so.
        self.assertIn("See how often each HealthCare.gov insurer in your state denied claims in 2024", top)
        self.assertIn("some of those insurers don't sell plans for 2027", top)
        self.assertNotIn("Cigna", top)
        sources = _between(html, 'id="sources"', "</section>")
        self.assertIn("tracking-insurer-participation-changes-in-the-aca-marketplaces-in-2027", sources)

    def test_page_drops_the_medicare_line_after_december_7(self):
        html = _enroll(date(2026, 12, 8))
        self.assertIn("<h1>Open enrollment for 2027</h1>", html)
        self.assertNotIn("Medicare open enrollment runs", html)

    def test_page_drops_medicare_plan_finder_after_december_7(self):
        top = _top(_enroll(date(2026, 12, 8)))
        self.assertNotIn("plan-compare", top)
        self.assertIn("Compare plans and costs on HealthCare.gov or your state's marketplace, even if you'll be renewed automatically. See how often", _text(top))

    def test_dates_and_year_come_from_the_constants(self):
        # Next fall's update is two constants in views.py, not a hunt through the template.
        with (
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_MEDICARE_ENROLLMENT", (date(2027, 10, 15), date(2027, 12, 7))),
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_ENROLLMENT_BOX_ENDS", date(2028, 1, 15)),
        ):
            html = _enroll(date(2027, 10, 20))
        top = _top(html)
        self.assertIn("<h1>Open enrollment for 2028</h1>", html)
        self.assertIn("<title>Open Enrollment for 2028: Dates, Numbers and What to Do</title>", html)
        self.assertIn("runs from October 15 to December 7, 2027,", _text(top))
        # The season's words take the constants' years; the tiles and chart keep the data's own year.
        season = (
            _between(top, 'id="enroll-dates-line"', "</p>"),
            _between(top, 'id="enroll-compare"', "</li>"),
            _between(html, 'id="enroll-dates"', "</details>"),
        )
        for part in season:
            self.assertNotIn("2026", _text(part))
        self.assertIn("2028", _text(season[1]) + _text(season[2]))

    def test_last_day_counts_in_hawaii_not_utc(self):
        # 5am UTC on December 8 is still December 7 in every state.
        late = datetime(2026, 12, 8, 5, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=late):
            html = Client().get(reverse("open-enrollment")).content.decode()
        self.assertIn("Medicare open enrollment runs", html)

    def test_page_after_the_season(self):
        # The marketplace numbers stay; the season's title, steps, dates and notes go.
        html = _enroll(AFTER_SEASON)
        top = _top(html)
        self.assertIn("<h1>Signing up for marketplace coverage</h1>", html)
        self.assertIn('id="mkline"', top)
        for gone in ("Open enrollment for", 'id="enroll-compare"', "Medicare open enrollment runs", "Dates, numbers"):
            self.assertNotIn(gone, html)
        self.assertNotIn('id="enroll-dates"', html)
        self.assertNotIn("<b>Open enrollment:</b>", html)

    def test_out_of_season_the_page_points_to_special_enrollment(self):
        top = _top(_enroll(AFTER_SEASON))
        line = _between(top, 'id="enroll-dates-line"', "</p>")
        self.assertIn('<a href="#lost-coverage">losing coverage can open a special enrollment period</a>', line)
        self.assertIn("Numbers and links checked October 22, 2026.", _text(top))

    # Medicare's window, HealthCare.gov's alone, and the season's last day.
    SEASON_DAYS = (IN_SEASON, date(2026, 12, 8), date(2027, 1, 15))
    RENEWING = (
        "Already have a marketplace plan? During open enrollment, log in, update your income and compare plans, "
        "even if you'll be renewed automatically. Plans and prices change every year, and a new plan only starts "
        "once you pay the first premium. For free, unbiased help, find an independent assister near you or call "
        "1-800-318-2596. Our fall checklist has the rest."
    )

    def _renewing(self, day):
        # The top of the page, and the question and its answer in the enrollment questions.
        html = _enroll(day)
        return _top(html), _between(_questions(html), 'id="enroll-renewing"', "</p>")

    def test_tells_current_enrollees_what_to_do_all_season(self):
        for day in self.SEASON_DAYS:
            with self.subTest(day=day):
                top, para = self._renewing(day)
                self.assertIn(self.RENEWING, _text(para))
                self.assertIn('href="https://www.healthcare.gov/find-local-help/"', para)
                self.assertIn(f'href="{reverse("coverage-changes")}#fall-checklist"', para)
                self.assertIn("Dates, numbers and links checked October 22, 2026.", _text(top))
                # Up top too, in short, with state-run marketplaces named.
                compare = _between(top, 'id="enroll-compare"', "</li>")
                self.assertIn("even if you'll be renewed automatically.", _text(compare))
                self.assertIn('or <a href="https://www.healthcare.gov/marketplace-in-your-state/">your state\'s marketplace</a>', compare)

    def test_help_line_dials_the_number_it_shows(self):
        _, para = self._renewing(self.SEASON_DAYS[0])
        self.assertIn('<a class="dw-tel" href="tel:+18003182596">1-800-318-2596</a>', para)

    def test_prints_no_healthcare_gov_dates_on_any_season_day(self):
        for day in self.SEASON_DAYS:
            top, para = self._renewing(day)
            for text in ("November 1", "December 15", "January 15", "2026"):
                with self.subTest(day=day, text=text):
                    self.assertNotIn(text, para)
            for text in ("November 1", "December 15", "January 15"):
                with self.subTest(day=day, text=text):
                    self.assertNotIn(text, top)

    def test_fall_checklist_link_lands_on_the_checklist(self):
        guide = Client().get(reverse("coverage-changes")).content.decode()
        self.assertIn('id="fall-checklist"', guide)

    def test_renewing_answer_has_no_em_dash_or_insurer(self):
        _, para = self._renewing(self.SEASON_DAYS[0])
        self.assertNotIn("—", para)
        for name in {_short(i["n"]) for i in DATA["issuers"]}:
            self.assertNotIn(name, _text(para))


class TestOpenEnrollmentTop(TestCase):
    """The top of the page: the dates, three numbers, the chart and what to do, kept short."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.section = _top(cls.content)

    def _tiles(self):
        numbers = _between(self.section, 'id="enroll-numbers"', '<div class="fhi-card">')
        return re.findall(
            r'<div class="dw-stat"><p class="dw-stat-head"><b class="dw-num">([^<]+)</b> <strong>([^<]+)</strong></p>(.*?)</div>',
            numbers,
        )

    def test_page_reads_in_order(self):
        order = [
            "<h1>",
            'id="enroll-dates-line"',
            'id="enroll-numbers"',
            'id="mkline"',
            'id="mknote"',
            'id="mktable"',
            'id="enroll-steps"',
            'href="#enroll-questions"',
            'href="#method-enroll"',
            'id="enroll-questions"',
            'id="sources"',
        ]
        at = [self.content.index(m) for m in order]
        self.assertEqual(at, sorted(at))

    def test_headings_and_lead_dont_lead_with_a_loss(self):
        top = _between(self.content, '<header class="fhi-page-title">', 'id="enroll-questions"')
        heads = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", top)
        self.assertEqual([_text(h) for h in heads], ["Open enrollment for 2027", "People with marketplace coverage each February", "What to do now"])
        for part in heads + [_between(self.section, 'id="enroll-dates-line"', "</p>")]:
            for word in ("fell", "drop", "lost", "lower", "fewer", "decline"):
                self.assertNotIn(word, _text(part).lower(), part)

    def test_headings_step_down_one_level_at_a_time(self):
        levels = [int(n) for n in re.findall(r"<h([1-6])\b", _between(self.content, '<main id="main-content"', "</main>"))]
        self.assertEqual(levels[0], 1)
        for before, after in zip(levels, levels[1:]):
            self.assertLessEqual(after, before + 1, levels)

    def test_three_tiles_in_the_page_tile_style(self):
        self.assertIn('<div class="dw-stats" id="enroll-numbers">', self.section)
        # Each label says it's the marketplace, so the Medicare dates above don't read as part of the count.
        self.assertEqual(
            [label for _, label, _ in self._tiles()],
            ["picked a marketplace plan", "had marketplace coverage", "as many had coverage as picked a plan"],
        )

    def test_tile_figures_come_from_the_bundle(self):
        feb, picks = _february(), _picks()
        year = max(picks)
        # The latest February with plan picks to set it against, which CMS is still counting.
        self.assertEqual((year, MK["prelim"]), (MK["feb"][-1]["y"], year))
        share = _pct(feb[year] / picks[year])
        (n0, _, s0), (n1, _, s1), (n2, _, s2) = self._tiles()
        self.assertEqual(n0, _millions(picks[year]))
        self.assertIn(f"for {year} in open enrollment, including people renewed automatically", _text(s0))
        self.assertEqual(n1, _millions(feb[year]))
        self.assertIn(f"in effect in February {year}, nationwide (preliminary)", _text(s1))
        self.assertEqual(n2, share)
        # The share is our division of two CMS counts, the newer one not final.
        self.assertEqual(_text(s2.rsplit("<span>", 1)[1]), f"in February {year} (preliminary), by our math from CMS's counts")
        # The bar draws the same share, and screen readers get the number instead.
        self.assertIn(f'<span class="dw-meter" aria-hidden="true"><i style="width: {share}"></i></span>', s2)

    def test_steps_say_what_to_do_with_links(self):
        steps = _between(self.section, 'id="enroll-steps"', "</ul>")
        leads = re.findall(r"<li[^>]*><b>([^<]+)</b>", steps)
        self.assertEqual(leads, ["Compare plans and costs", "Pay the first premium.", "Disagree with a marketplace decision?", "Get free help."])
        for href in (
            "https://www.healthcare.gov/",
            "https://www.healthcare.gov/marketplace-in-your-state/",
            "https://www.medicare.gov/plan-compare/",
            f"{reverse('denial-watch')}#insurers",
            f"{reverse('denial-watch')}#prior-auth",
            "https://www.healthcare.gov/marketplace-appeals/",
            "https://www.healthcare.gov/find-local-help/",
            "tel:+18003182596",
        ):
            self.assertIn(f'href="{href}"', steps)
        text = _text(steps)
        self.assertIn("A plan you pick only starts once the first month's premium is paid, if there is one.", text)
        self.assertIn("you can appeal to the marketplace, generally within 90 days of the notice.", text)
        # The same deadline as Denial Watch's Who to call card.
        self.assertIn("generally within 90 days of your eligibility notice", _text(_between(_page(), 'id="marketplace-appeals"', "</li>")))

    def test_denial_watch_links_land_on_its_sections(self):
        dw = _page(IN_SEASON)
        for target in re.findall(rf'href="{reverse("denial-watch")}#([\w-]+)"', self.content):
            self.assertIn(f'id="{target}"', dw, target)

    def test_out_of_season_steps_leave_out_comparing_plans(self):
        top = _top(_enroll(AFTER_SEASON))
        steps = _between(top, 'id="enroll-steps"', "</ul>")
        self.assertEqual(re.findall(r"<li[^>]*><b>([^<]+)</b>", steps), ["Pay the first premium.", "Disagree with a marketplace decision?", "Get free help."])

    def test_points_down_to_the_enrollment_questions(self):
        self.assertIn('<a href="#enroll-questions">Enrollment questions</a>', self.section)
        self.assertIn(
            '<p class="dw-how"><a href="#method-enroll">How we got these numbers<span class="fhi-visually-hidden">: Open enrollment</span></a></p>',
            self.section,
        )

    def test_top_stays_short(self):
        # The enrollment copy had grown far longer than the rest of Denial Watch: 273 words, plus a little room.
        self.assertLessEqual(_words(self.section), 280)

    def test_sections_are_named_by_their_headings(self):
        self.assertIn('<section class="fhi-stack" id="enroll-questions" aria-labelledby="enroll-questions-title">', self.content)
        self.assertIn('<h2 id="enroll-questions-title">Enrollment questions</h2>', self.content)
        self.assertIn('<section class="fhi-stack" id="sources" aria-labelledby="sources-title">', self.content)


class TestOpenEnrollmentMarketplace(TestCase):
    """The marketplace numbers, up top and in the questions below, agree with the data and say where they come from."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll()
        cls.section = _top(cls.content)
        cls.questions = _questions(cls.content)
        cls.text = _text(cls.section)
        cls.answers = _text(cls.questions)
        method = _between(cls.content, 'id="method-enroll"', "</div>")
        cls.method = _text(method[method.index("<b>Marketplace coverage:</b>") : method.index("</ol>")])
        cls.method_html = method
        cls.script = _between(OE_JS, "/* marketplace coverage:", "})();")

    def test_chart_sits_at_the_top_before_the_questions(self):
        self.assertIn('id="mkline"', self.section)
        self.assertLess(self.content.index('id="mkline"'), self.content.index('id="enroll-questions"'))
        parents = _section_parents(self.content)
        self.assertIsNone(parents["enroll-questions"])
        self.assertIsNone(parents["sources"])

    def test_no_id_is_used_twice_in_season_or_out(self):
        for day in (IN_SEASON, AFTER_SEASON):
            ids = re.findall(r'\sid="([^"]+)"', _body(_enroll(day)))
            self.assertEqual(len(ids), len(set(ids)), sorted(i for i in ids if ids.count(i) > 1))

    def test_heading_is_neutral_and_the_caption_states_the_change_from_the_data(self):
        # The chart's heading names what it shows; the change is stated in its caption, from the data.
        changes, feb = _yearly_changes(), _february()
        a, b = MK["feb"][-2], MK["feb"][-1]
        self.assertEqual(min(changes, key=changes.get), b["y"])
        self.assertLess(b["n"], a["n"])
        self.assertIn('<h2 class="fhi-card-title" id="mk-title">People with marketplace coverage each February</h2>', self.section)
        caption = _text(_between(self.section, 'id="mknote"', "</p>"))
        self.assertIn(
            f"then went from {_millions(a['n'])} in February {a['y']} to {_millions(b['n'])} in February {b['y']}, "
            f"{round(-changes[b['y']] * 100)}% lower.",
            caption,
        )
        self.assertEqual(feb[b["y"]], b["n"])

    def test_the_only_other_drop_is_small(self):
        changes, first, last = _yearly_changes(), MK["feb"][0]["y"], MK["feb"][-1]["y"]
        others = [y for y, c in changes.items() if c < 0 and y != last]
        self.assertEqual(len(others), 1, others)
        self.assertLess(abs(changes[others[0]]), 0.01)
        caption = _text(_between(self.section, 'id="mknote"', "</p>"))
        # The caption names CMS, since the tiles and chart sit above the sources.
        self.assertIn(f"CMS's count rose every year from {first} to {last - 1} except {others[0]}, when it dipped less than 1%,", caption)

    def test_intro_gives_the_counts(self):
        # Why February, in the questions; the counts themselves are in the chart's caption (above).
        b = MK["feb"][-1]
        self.assertIn(f"February is the latest month CMS has published for {b['y']}, so every year is compared on that month.", self.answers)
        self.assertIn(
            "What does CMS's count include? Everyone with a HealthCare.gov or state-run marketplace plan in effect in February, nationwide.",
            self.answers,
        )

    def test_bridge_says_the_denial_rates_scope_and_year(self):
        year = DATA["trend"][-1]["year"]
        bridge = _text(_between(self.questions, '<p id="denials-bridge">', "</p>"))
        self.assertEqual(
            bridge,
            "Partly. Denial Watch's denial rates, on its state map and in its insurer table, "
            f"are for marketplace plans too, but only for HealthCare.gov states and insurers in {year}, "
            "while CMS's count covers every marketplace nationwide.",
        )
        self.assertIn("Do Denial Watch's denial rates cover the same plans?", self.answers)
        # Denial Watch's map and insurer table say the same year.
        self.assertIn(f"Share of in-network claims denied by HealthCare.gov insurers in each state, {year}.", _page())

    def test_bridge_links_land_on_denial_watchs_sections(self):
        bridge = _between(self.questions, 'id="denials-bridge"', "</p>")
        dw = reverse("denial-watch")
        targets = re.findall(rf'href="{dw}#([\w-]+)"', bridge)
        self.assertEqual(targets, ["map-denials", "insurers"])
        page = _page()
        for target in targets:
            self.assertIn(f'id="{target}"', page)

    def test_bridge_has_no_causal_words(self):
        bridge = _text(_between(self.questions, '<p id="denials-bridge">', "</p>")).lower()
        for words in ("because", "after", "led to", "due to", "caused", "result", "drove", "since", "so "):
            self.assertNotIn(words, bridge)
        self.assertNotIn(chr(0x2014), bridge)

    def test_chart_label_before_the_script_runs_names_the_years(self):
        first, last, prelim = MK["feb"][0]["y"], MK["feb"][-1]["y"], MK["prelim"]
        self.assertIn(
            f'aria-label="Line chart of people with marketplace coverage in effect each February, {first} to {last}. {prelim} is preliminary"',
            self.section,
        )

    def test_latest_year_is_marked_preliminary(self):
        prelim = MK["prelim"]
        self.assertIn(f"The hollow point, {prelim}, is preliminary.", _text(_between(self.section, 'id="mknote"', "</p>")))
        self.assertIn(f"The {prelim} count isn't final yet", self.answers)
        self.assertIn(f"so {prelim} is preliminary and may change", self.method)
        self.assertIn("open:p=>p.x===MK.prelim", self.script)
        self.assertIn('fill="${o?paper:color}" stroke="${o?color:paper}"', CHARTS_JS)
        self.assertIn("(preliminary)", self.script)

    def test_chart_starts_at_zero_and_fits_every_year(self):
        call = re.search(r"line\(\$\('mkline'\),.*?\{ymin:(\d+),ymax:([\d.e]+),ticks:\[([^\]]+)\]", self.script)
        self.assertIsNotNone(call)
        ymin, ymax = float(call.group(1)), float(call.group(2))
        self.assertEqual(ymin, 0)
        self.assertEqual(float(call.group(3).split(",")[-1]), ymax)
        self.assertTrue(all(r["n"] <= ymax for r in MK["feb"]))
        self.assertIn("color:tok('--dw-count')", self.script)

    def test_chart_and_table_use_each_february_count(self):
        # A source check; the browser test reads the drawn points and cells back against the bundle.
        self.assertIn("const F=MK.feb,", self.script)
        self.assertIn("line($('mkline'),F.map(r=>({x:r.y,y:r.n})),", self.script)
        self.assertIn("const chg=i=>{const c=F[i].n/F[i-1].n-1;", self.script)
        self.assertIn("<td class=\"dw-r dw-num\">${fmt(r.n)}</td><td class=\"dw-r dw-num\">${i?chg(i):''}</td>", self.script)

    def test_script_draws_only_with_data(self):
        self.assertIn("const MK=D.marketplace;\nif(MK){", OE_JS)
        self.assertEqual(OE_JS.index("/* marketplace coverage:"), OE_JS.rindex("/* "))

    def test_tables_are_accessible(self):
        picks = _between(self.questions, 'id="mkpicks"', "</table>")
        self.assertIn('<caption class="fhi-visually-hidden">', picks)
        self.assertEqual(picks.count('<th scope="col"'), 3)
        self.assertEqual(picks.count('<th scope="row">'), 2)
        self.assertIn('<table class="dw-table" id="mktable"></table>', self.section)
        self.assertIn('<caption class="fhi-visually-hidden">People with marketplace coverage in effect each February', self.script)
        self.assertIn('<th scope="col">February</th>', self.script)
        self.assertIn('<tr><th scope="row">${r.y}', self.script)
        self.assertIn('<svg class="dw-chart" id="mkline" viewBox="0 0 560 270" role="img" aria-label="', self.section)

    def test_plan_picks_table_matches_the_data(self):
        feb, picks, prelim = _february(), _picks(), MK["prelim"]
        years = sorted(picks)

        def covered(y):
            return f"{feb[y]:,} ({_pct(feb[y] / picks[y])}{', preliminary' if y == prelim else ''})"

        table = _between(self.questions, 'id="mkpicks"', "</table>")
        self.assertEqual(
            _table_rows(table),
            [
                ["Count", *map(str, years)],
                ["People with a plan picked in open enrollment", *(f"{picks[y]:,}" for y in years)],
                ["People with coverage in effect in February", *map(covered, years)],
            ],
        )
        self.assertIn(f"with coverage in effect that February, {years[0]} and {years[1]}</caption>", table)

    def test_coverage_shares_in_the_words_match_the_data(self):
        feb, picks = _february(), _picks()
        (y0, s0), (y1, s1) = sorted((y, _pct(feb[y] / n)) for y, n in picks.items())
        self.assertIn(f"Coverage in effect in February came to {s0} of those people for {y0}, and {s1} for {y1}.", self.answers)
        self.assertIn(f"The {s0} and {s1} divide each February's coverage by that year's plan picks.", self.method)
        self.assertIn(f"CMS's early 2025 report gave {_pct(EARLY_FEB_2025 / picks[2025])}, from its earlier February count.", self.method)

    def test_picking_a_plan_is_explained_and_cms_quoted(self):
        self.assertIn(
            "Is picking a plan the same as having coverage? No. A plan picked in open enrollment only becomes coverage "
            "once the first month's premium is paid, if there is one.",
            self.answers,
        )
        self.assertIn(f'"{CMS_PREMIUM}"', self.method)
        self.assertIn('href="https://www.cms.gov/files/document/2026-public-use-files-faqs.pdf"', self.method_html)

    def test_kff_sentence_is_quoted_exactly_and_linked(self):
        quote = _between(self.questions, '<blockquote class="dw-quote"', "</blockquote>")
        self.assertIn(f'cite="{KFF_MARKET}"><p>{KFF_SENTENCE}</p>', quote)
        self.assertIn(f'href="{KFF_MARKET}"', self.questions)
        self.assertIn(f"From KFF's analysis of {KFF_MARKET_DATE}.", self.answers)
        self.assertIn(f"(KFF, {KFF_MARKET_DATE})", self.method)
        self.assertIn("KFF, a nonprofit health policy research, polling, and news organization,", self.answers)

    def test_kff_counts_agree_with_its_quoted_drop(self):
        june25, june26 = KFF_JUNE
        self.assertIn(f"{june25} million in June 2025 and {june26} million in June 2026", self.method)
        # The quote rounds to a whole percent, the counts to a tenth of a million: their ranges must overlap.
        drop = int(re.search(r"fell (\d+)%", KFF_SENTENCE).group(1)) / 100
        low, high = 1 - (june26 + 0.05) / (june25 - 0.05), 1 - (june26 - 0.05) / (june25 + 0.05)
        self.assertTrue(low < drop + 0.005 and high >= drop - 0.005, (low, high))

    def test_plan_picks_count_people_including_automatic_renewals(self):
        # CMS's count is of people, and it includes people renewed without picking again.
        self.assertIn("CMS counts everyone with a plan picked, including people renewed into one automatically.", self.answers)
        self.assertIn("CMS counts people, not plans, including people renewed into a plan automatically.", self.method)
        self.assertNotIn("of the plans picked", self.text + self.answers)
        rows = re.findall(r'<th scope="row">([^<]+)</th>', _between(self.questions, 'id="mkpicks"', "</table>"))
        self.assertEqual(rows, ["People with a plan picked in open enrollment", "People with coverage in effect in February"])

    def test_kff_is_described_the_way_it_asks(self):
        self.assertNotIn("independent health policy research group", self.content)

    def test_kff_differences_are_named(self):
        self.assertIn("on the marketplace or off it", self.answers)
        self.assertIn("compares June with June", self.answers)
        self.assertIn("so the two numbers aren't meant to match", self.answers)

    def test_no_link_to_a_kff_pdf(self):
        for href in re.findall(r'href="(https://www\.kff\.org/[^"]*)"', self.content):
            self.assertFalse(href.lower().endswith(".pdf"), href)

    def test_survey_figures_say_who_was_asked(self):
        self.assertIn(f'href="{KFF_SURVEY}"', self.questions)
        self.assertIn("adults who had a marketplace plan in 2025, taken in February and March 2026, 22% said they had moved to other coverage", self.answers)
        self.assertIn("and 9% said they were uninsured.", self.answers)
        # KFF's own caveat on those plans, so the note doesn't reassure more than KFF does.
        self.assertIn("KFF notes that some plans sold outside the marketplace may cover less and have fewer consumer protections.", self.answers)
        self.assertIn("1,117 adults who had marketplace plans in 2025", self.method)
        self.assertIn("plus or minus 3.8 points", self.method)

    def test_questions_say_what_to_do_after_losing_coverage(self):
        line = _between(self.questions, 'id="lost-coverage"', "</p>")
        self.assertIn(
            "Lost coverage, or about to? Losing health coverage can open a special enrollment period, usually from "
            "60 days before it ends to 60 days after (90 days after, for Medicaid or CHIP), and you may need to send "
            "proof that you lost it. Medicaid and CHIP take applications any time of year.",
            _text(line),
        )
        # Its own question, second, after renewing.
        self.assertIn('<details class="fhi-accordion-item" id="lost-coverage">', self.questions)
        self.assertLess(self.questions.index('id="enroll-renewing"'), self.questions.index('id="lost-coverage"'))

    def test_closing_line_links_special_enrollment_and_local_help(self):
        line = _between(self.questions, 'id="lost-coverage"', "</p>")
        for href in (
            "https://www.healthcare.gov/coverage-outside-open-enrollment/special-enrollment-period/",
            "https://www.healthcare.gov/find-local-help/",
        ):
            self.assertIn(f'href="{href}"', line)

    def test_closing_line_has_no_em_dash(self):
        self.assertNotIn(chr(0x2014), _between(self.questions, 'id="lost-coverage"', "</p>"))

    def test_census_figures_say_their_year_and_where_they_are(self):
        self.assertIn(f"The Census uninsured figures on Denial Watch are for {_census_year()}, before the {MK['prelim']} drop in marketplace coverage.", self.answers)
        self.assertIn(f'<a href="{reverse("denial-watch")}#coverage">Census uninsured figures on Denial Watch</a>', self.questions)

    def test_causes_stay_off_the_page(self):
        # Why enrollment fell is contested, so the page shows what changed, not why.
        for words in ("tax credit", "subsid", "fraud", "improper", "because", "caused", "blame", "enhanced"):
            self.assertNotIn(words, (self.text + self.answers + self.method).lower())

    def test_no_insurer_is_named(self):
        names = {_short(i["n"]) for i in DATA["issuers"]} | {r["n"] for r in DATA["prior_auth"]}
        for name in names:
            self.assertNotIn(name, self.text + self.answers + self.method)

    def test_no_em_dashes_in_the_copy(self):
        for part in (self.section, self.questions, self.method, self.script):
            self.assertNotIn("—", part)

    def test_method_note_names_the_file_its_date_and_minnesota(self):
        a, b = MK["feb"][-2], MK["feb"][-1]
        without = (b["n"] - b["mn"]) / (a["n"] - a["mn"]) - 1
        self.assertIn(f'<a href="{CMS_EFFECTUATED}">CMS Health Insurance Exchanges Monthly Effectuated Enrollment</a>', self.method_html)
        asof = date.fromisoformat(MK["as_of"])
        self.assertIn(
            f"the file with data as of {asof:%B} {asof.day}, {asof.year} (Data_asof_{asof:%Y%m%d}), which runs to February {b['y']}",
            self.method,
        )
        self.assertIn(f"without Minnesota, the {b['y']} drop is {-without * 100:.1f}%", self.method)
        # CMS redid 2021 onward; the years before that start with the file's first.
        self.assertIn(f"don't say whether {MK['feb'][0]['y']} to 2020 were counted again the same way", self.method)
        for p in MK["picks"]:
            self.assertIn(f'<a href="{p["url"]}">{p["y"]}</a>', self.method_html)

    def test_method_note_compares_the_early_count_with_this_file(self):
        self.assertIn(
            f"its early 2025 report had {_millions(EARLY_FEB_2025)} for February 2025 (counted March 15, 2025), "
            f"against {_millions(_february()[2025])} in this file, so mixing reports would make the drop look bigger.",
            self.method,
        )


class TestOpenEnrollmentQuestions(TestCase):
    """The longer enrollment answers, one closed question each, between the top of the page and the sources."""

    QUESTIONS = [
        ("enroll-renewing", "Already have a marketplace plan?"),
        ("lost-coverage", "Lost coverage, or about to?"),
        ("enroll-dates", "When can I sign up for a 2027 HealthCare.gov plan?"),
        ("picks-vs-coverage", "Is picking a plan the same as having coverage?"),
        ("cms-count", "What does CMS's count include?"),
        ("kff-count", "How does CMS's count compare with KFF's?"),
        ("leaving-the-marketplace", "Does leaving the marketplace mean going uninsured?"),
        ("enroll-and-denials", "Do Denial Watch's denial rates cover the same plans?"),
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.part = _questions(cls.content)

    @staticmethod
    def _questions(part):
        return re.findall(
            r'<details class="fhi-accordion-item" id="([\w-]+)">\s*<summary><h3 class="fhi-accordion-question">([^<]+)</h3></summary>',
            part,
        )

    def test_questions_in_season(self):
        self.assertIn('<h2 id="enroll-questions-title">Enrollment questions</h2>', self.part)
        self.assertEqual(self._questions(self.part), self.QUESTIONS)

    def test_dates_question_only_in_season(self):
        part = _questions(_enroll(AFTER_SEASON))
        self.assertEqual(self._questions(part), [q for q in self.QUESTIONS if q[0] != "enroll-dates"])

    def test_every_question_starts_closed(self):
        tags = re.findall(r"<details[^>]*>", self.part)
        self.assertEqual(len(tags), len(self.QUESTIONS))
        for tag in tags:
            self.assertNotIn(" open", tag)

    def test_sits_between_the_top_and_the_sources(self):
        order = ['id="enroll"', 'id="enroll-questions"', 'id="sources"']
        at = [self.content.index(m) for m in order]
        self.assertEqual(at, sorted(at))

    def test_answers_are_short(self):
        for ident, _ in self.QUESTIONS:
            item = _between(self.part, f'id="{ident}"', "</details>")
            # The two-year table of counts isn't reading.
            answer = re.sub(r"<table.*?</table>", "", item[item.index("</summary>") :], flags=re.S)
            self.assertLessEqual(_words(answer), 85, ident)

    def test_questions_link_their_method(self):
        self.assertIn(
            '<p class="dw-how"><a href="#method-enroll">How we got these numbers<span class="fhi-visually-hidden">: Enrollment questions</span></a></p>',
            self.part,
        )


class TestOpenEnrollmentSourcesFold(TestCase):
    """The page's own sources and method, the enrollment group alone, in one closed <details> that a link opens."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.sources = _between(cls.content, 'id="sources"', "</section>")
        cls.fold = _between(cls.sources, "<details", "</details>")

    def test_sources_sit_in_one_closed_details_with_the_heading_as_its_summary(self):
        self.assertEqual(self.sources.count("<details"), 1)
        tag = re.search(r"<details[^>]*>", self.sources).group(0)
        self.assertEqual(tag, '<details class="fhi-accordion-item" id="sources-details">')
        # The summary comes first, so a reader without the script opens it with a click or a key.
        self.assertRegex(
            self.sources,
            r'<details class="fhi-accordion-item" id="sources-details">\s*<summary><h2 class="fhi-accordion-question" id="sources-title">Sources and method</h2></summary>',
        )

    def test_the_fold_holds_the_enrollment_method_alone(self):
        self.assertEqual(re.findall(r'<div class="dw-method" id="([\w-]+)">', self.sources), ["method-enroll"])
        self.assertIn('id="method-enroll"', self.fold)

    def test_every_link_into_the_sources_lands_inside_the_fold(self):
        targets = set(re.findall(r'href="#(method-[\w-]+)"', _body(self.content) + OE_JS))
        self.assertEqual(targets, {"method-enroll"})
        self.assertIn('id="method-enroll"', self.fold)

    def test_enrollment_method_links_back_up(self):
        group = _between(self.fold, 'id="method-enroll"', "</div>")
        self.assertIn('<p class="dw-back"><a href="#enroll">Back to Open enrollment for 2027</a></p>', group)
        for item in ("<b>Marketplace coverage:</b>", "<b>Plan picks:</b>", "<b>KFF:</b>", "<b>Open enrollment:</b>"):
            self.assertIn(item, group)
        after = _between(_enroll(AFTER_SEASON), 'id="method-enroll"', "</div>")
        self.assertIn('<p class="dw-back"><a href="#enroll">Back to Signing up for marketplace coverage</a></p>', after)

    def test_medicare_dates_cite_their_sources(self):
        for href in (
            "https://www.cms.gov/newsroom/press-releases/medicare-advantage-medicare-prescription-drug-programs-expected-remain-stable-2027",
            "https://www.medicare.gov/basics/get-started-with-medicare/get-more-coverage/joining-a-plan",
            "https://www.cms.gov/files/document/ftr-requirement-py26-27-statement.pdf",
        ):
            self.assertIn(f'href="{href}"', self.fold)

    def test_script_opens_the_folds_before_reading_the_data(self):
        # A problem with the data, or a chart that throws, can't leave a link stuck on a closed fold.
        self.assertLess(OE_JS.index("foldLinks();"), OE_JS.index("JSON.parse"))


class TestOpenEnrollmentLinksAndStyle(TestCase):
    """Every outside link is https, every jump lands below the sticky header, and the copy has no em dashes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.body = _body(cls.content)

    def test_outside_links_are_https(self):
        for href in re.findall(r'href="([^"]+)"', self.body):
            # Our own pages, anchors and phone numbers; "//host" is an outside link.
            if href.startswith(("#", "tel:+1")) or (href.startswith("/") and not href.startswith("//")):
                continue
            self.assertTrue(href.startswith("https://"), href)

    def test_no_em_dashes(self):
        for day in (IN_SEASON, AFTER_SEASON):
            self.assertNotIn("—", _body(_enroll(day)))

    def test_every_jump_lands_below_the_sticky_header(self):
        style = _between(self.content, "<style>", "</style>")
        rules = re.findall(r"([^{}\n]+)\{ scroll-margin-top:", style)
        self.assertEqual([r.split("{")[-1].strip() for r in rules], [".dw [id]", ".dw [id]"])
        for day in (IN_SEASON, AFTER_SEASON):
            body = _body(_enroll(day))
            for target in set(re.findall(r'href="#([\w-]+)"', body + OE_JS)):
                self.assertIn(f'id="{target}"', body, (day, target))


class TestOpenEnrollmentColours(TestCase):
    """Counts are blue, not red or green, the share bar's two parts differ in lightness, and each has words beside it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _enroll(IN_SEASON)
        cls.section = _top(cls.content)

    def test_share_bar_is_coverage_on_a_track_of_plan_picks(self):
        self.assertIn("background-color: var(--dw-count-tint); box-shadow: inset 0 0 0 1px var(--dw-count); }", _between(self.content, ".dw-meter {", "\n"))
        self.assertIn(".dw-meter i { display: block; height: 100%; background-color: var(--dw-count); }", self.content)
        self.assertNotIn("var(--dw-track)", _between(self.content, ".dw-meter {", "\n"))

    def test_share_bar_has_a_key_in_words(self):
        tile = _between(self.section, '<span class="dw-meter"', "</div>")
        key = _between(tile, '<span class="dw-key" aria-hidden="true">', "</span></span>")
        self.assertEqual(re.findall(r'<i class="dw-sw ([\w-]+)"></i>([^<]+)', key), [("dw-sw-count", "had coverage"), ("dw-sw-picks", "picked a plan")])

    def test_chart_key_names_the_count_and_the_hollow_point(self):
        key = _between(self.section, '<p class="dw-key" id="mkkey">', "</p>")
        self.assertEqual(
            re.findall(r'<i class="dw-sw ([\w-]+)"></i>([^<]+)', key),
            [("dw-sw-count", "Coverage in effect"), ("dw-sw-open", "Preliminary")],
        )
        self.assertLess(self.section.index('id="mkline"'), self.section.index('id="mkkey"'))

    def test_no_red_or_green_on_the_page(self):
        for colour in ("--dw-bad", "--dw-good"):
            self.assertNotIn(f"var({colour}", _between(self.content, '<div class="fhi-page-wide dw">', '<script id="dw-data"'))
            self.assertNotIn(colour, OE_JS)

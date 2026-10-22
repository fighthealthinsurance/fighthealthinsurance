"""Tests for the Denial Watch public-data page.

The page is staged behind DENIAL_WATCH_PAGE_ENABLED (on in Dev/Test, off in
production until launch). Its numbers come from static/denial_watch.json, and
some of them are also written into the page's sentences, so these tests check
that the copy still agrees with the data after a refresh.
"""

import contextlib
import csv
import hashlib
import importlib.util
import json
import pathlib
import re
import tempfile
from html.parser import HTMLParser
from datetime import date, datetime, timezone as dt_timezone
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

REPO = pathlib.Path(__file__).resolve().parents[2]
DATA = json.loads(
    (REPO / "fighthealthinsurance" / "static" / "denial_watch.json").read_text()
)


def _load_builder():
    path = REPO / "scripts" / "denial_watch" / "build_bundle.py"
    spec = importlib.util.spec_from_file_location("denial_watch_build", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _millions(x: float) -> str:
    return f"{x / 1e6:.1f} million"


def _without_data(html: str) -> str:
    """The page as a reader sees it, without the embedded JSON."""
    return re.sub(r'<script id="dw-data".*?</script>', "", html, flags=re.S)


def _between(html: str, start: str, end: str) -> str:
    """The part of the page from one marker up to the next, so a check stays in its section."""
    i = html.index(start)
    return html[i : html.index(end, i)]


def _text(html: str) -> str:
    """Visible text, tags dropped and spaces collapsed, so a check isn't tied to markup."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ")).strip()


def _page(day: date | None = None) -> str:
    """The rendered page, with today pinned when a day is given."""
    if day is None:
        return Client().get(reverse("denial-watch")).content.decode()
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        return Client().get(reverse("denial-watch")).content.decode()


KFF_PA = "https://www.kff.org/patient-consumer-protections/prior-authorization-metrics-provide-new-insights-into-insurer-practices-but-gaps-remain/"

SHOWN_DRUGS = [r for tab in ("top", "per", "growth", "cheap") for r in DATA["drugs"][tab]]


class TestDenialWatchPage(TestCase):
    """The page renders with its data embedded."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        response = Client().get(reverse("denial-watch"))
        cls.status_code = response.status_code
        cls.content = response.content.decode()

    def test_page_renders(self):
        self.assertEqual(self.status_code, 200)

    def test_data_is_embedded_for_the_script(self):
        self.assertIn('id="dw-data"', self.content)
        self.assertIn("js/denial_watch.js", self.content)

    def test_page_links_to_starting_an_appeal(self):
        self.assertIn(reverse("scan"), self.content)

    def test_counter_is_labeled_as_an_illustration(self):
        # Reviewers flagged a running count as reading like a live feed.
        self.assertIn("An illustration, not a live count", self.content)
        # Screen readers hear the group's label first.
        self.assertNotIn("Estimated denials today", self.content)

    def test_appeal_deadlines_match_the_glossary(self):
        # Medicare Advantage is 60 days from receipt, presumed 5 days after the notice.
        self.assertIn("Medicare Advantage gives 65 days from the date on the notice", self.content)
        self.assertIn("Medicaid plans give 60", self.content)

    def test_claim_file_is_free_except_copying_in_medicare_plans(self):
        # CMS's Parts C and D appeals guidance, section 50.5.2, lets those plans charge for copying and mailing.
        step = _text(_between(self.content, "1. Find out why", "</div>"))
        self.assertIn("Employer, marketplace and Medicaid plans have to give you the claim file for free.", step)
        self.assertIn("Medicare Advantage and Medicare drug plans have to give it to you too, but can charge for copying and mailing it.", step)
        self.assertNotIn("They have to give you the claim file for free", self.content)

    def test_page_names_its_sources(self):
        # Checked outside the embedded JSON, which carries every URL anyway.
        visible = _without_data(self.content)
        for source in DATA["sources"]:
            self.assertIn(f'href="{source["url"]}"', visible)

    def test_page_sits_on_the_wide_column_without_its_own_stylesheet(self):
        self.assertIn('class="fhi-page-wide dw"', self.content)
        self.assertNotIn("denial_watch.css", self.content)


class TestDenialWatchCopyMatchesData(TestCase):
    """Numbers written into sentences must match the JSON they came from."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = Client().get(reverse("denial-watch")).content.decode()

    def test_state_range_headline(self):
        rates = [s["rate"] for s in DATA["states"].values()]
        self.assertIn(
            # "about": the lowest state (South Dakota, 6.55%) rounds to 7% on the
            # map legend but shows as 6.5% in its tooltip.
            f"Denial rates run from about {_pct(min(rates))} to {_pct(max(rates))}",
            self.content,
        )

    def test_state_range_headline_says_which_states(self):
        # The map covers only states whose insurers report to HealthCare.gov, not every state.
        self.assertIn(f"across {len(DATA['states'])} HealthCare.gov states</h3>", self.content)
        self.assertNotIn("depending on your state", self.content)

    def test_california_review_rates(self):
        by_year = {r["y"]: r["r"] for r in DATA["imr_trend"]}
        self.assertIn(f"overturned {_pct(by_year[2025])} of the denials", self.content)
        self.assertIn(f"up from {_pct(by_year[2017])} in 2017", self.content)

    def test_appeal_win_rate_is_not_compared_with_2023(self):
        # The insurers reporting changed between years, so it isn't like for like.
        prev = next(t for t in DATA["trend"] if t["year"] == 2023)
        self.assertIn(
            "In 2024, HealthCare.gov insurers reversed about a third of the internal appeals people filed",
            self.content,
        )
        self.assertIn("about a third of appeals win.", self.content)
        self.assertNotIn(_pct(prev["overturned"] / prev["appeals"]), self.content)

    def test_method_note_explains_the_won_counts_left_out(self):
        self.assertIn("Nine insurers' 2024 appeals-won counts look like reporting errors", self.content)
        self.assertIn("With them, the 2024 rate would be 33.6%.", self.content)

    def test_share_of_denials_appealed_in_the_steps(self):
        latest = DATA["trend"][-1]
        ratio = latest["in_denied"] / latest["appeals_all"]
        self.assertIn(
            f"about 1 appeal for every {round(ratio, -1):.0f} in-network denials",
            self.content,
        )

    def test_headline_counts_in_network_claims(self):
        # One basis for the headline: in-network, as KFF and the map count.
        latest = DATA["trend"][-1]
        self.assertEqual(round(1 / latest["in_rate"]), 5)
        self.assertIn(
            "about one in five in-network claims in 2024, "
            f"{_millions(latest['in_denied'])} among reporting insurers",
            self.content,
        )

    def test_method_note_gives_both_counting_bases(self):
        latest = DATA["trend"][-1]
        ratio = latest["in_denied"] / latest["appeals_all"]
        self.assertIn(
            f"{_millions(latest['in_denied'])} denials in 2024, "
            f"{_pct(latest['in_rate'])} of in-network claims, and "
            f"{latest['appeals_all']:,.0f} internal appeals, "
            f"about 1 for every {round(ratio, -1):.0f} of those denials",
            self.content,
        )
        self.assertIn(
            f"In 2024 insurers denied {_millions(latest['out_denied'])} of "
            f"{_millions(latest['out_claims'])} out-of-network claims ({_pct(latest['out_rate'])}) and "
            f"{_millions(latest['all_denied'])} of {_millions(latest['all_claims'])} claims overall "
            f"({_pct(latest['all_rate'])}), the same national rates KFF reports",
            self.content,
        )

    def test_about_doubled_matches_in_network_ratio(self):
        by_year = {t["year"]: t for t in DATA["trend"]}
        ratio = by_year[2024]["in_denied"] / by_year[2020]["in_denied"]
        self.assertTrue(1.9 <= ratio < 2.1, ratio)
        self.assertIn(
            "In-network denials about doubled from 2020 to 2024, to "
            f"{_millions(by_year[2024]['in_denied'])}",
            self.content,
        )

    def test_trend_copy_uses_the_in_network_series(self):
        # The yearly in-network rates are in the table beside the chart, not read out in the intro.
        latest = DATA["trend"][-1]
        self.assertEqual(round(latest["out_rate"] / latest["in_rate"]), 2)
        self.assertIn(
            f"denied {_pct(latest['out_rate'])} of them in 2024, about twice the in-network rate",
            self.content,
        )

    def test_trend_copy_starts_the_other_views_in_2022(self):
        first = min(t["year"] for t in DATA["trend"] if t["out_rate"] is not None)
        self.assertEqual(first, 2022)
        self.assertIn(f"Insurers have reported out-of-network claims only since {first}", self.content)

    def test_trend_copy_doesnt_say_out_of_network_was_reported_separately(self):
        # The 2020 and 2021 files have no out-of-network count at all, not a combined one.
        self.assertNotIn("separately only since", self.content)
        self.assertNotIn("separately only since", (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text())

    def test_not_medically_necessary_share_of_reasons(self):
        total = sum(r["v"] for r in DATA["reasons"])
        parts = [r["v"] for r in DATA["reasons"] if "medically necessary" in r["k"]]
        self.assertEqual(len(parts), 2)
        self.assertIn(
            f"is {sum(parts) / total:.1%} of all reasons "
            f"({sum(parts) / 1e6:.1f} million of {total / 1e6:.1f} million)",
            self.content,
        )

    def test_california_rate_in_the_steps(self):
        by_year = {r["y"]: r["r"] for r in DATA["imr_trend"]}
        self.assertIn(f"in {_pct(by_year[2025])} of the cases they decided in 2025", self.content)

    def test_correlations(self):
        corr = DATA["corr"]
        self.assertIn(f"correlation {corr['fair_poor_health']:.2f}", self.content)
        self.assertIn(f"diabetes ({corr['diabetes']:.2f})", self.content)


class TestDenialWatchStaging(TestCase):
    """The staging flag controls both routing and discoverability."""

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_page_serves_404_when_disabled(self):
        response = Client().get(reverse("denial-watch"))
        self.assertEqual(response.status_code, 404)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_options_does_not_reveal_the_route_when_disabled(self):
        response = Client().options(reverse("denial-watch"))
        self.assertEqual(response.status_code, 404)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_page_is_in_the_static_sitemap_when_enabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertIn("denial-watch", StaticViewSitemap().items())

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_page_is_hidden_from_the_sitemap_when_disabled(self):
        from fighthealthinsurance.sitemap import StaticViewSitemap

        self.assertNotIn("denial-watch", StaticViewSitemap().items())


class TestDenialWatchData(TestCase):
    """The shipped JSON keeps the rules the build promises."""

    def test_rates_are_proportions(self):
        for state, s in DATA["states"].items():
            self.assertTrue(0 < s["rate"] < 1, state)
            if s["won"] is not None:
                self.assertTrue(0 <= s["won"] <= 1, state)
                self.assertGreaterEqual(s["appeals"], 50, state)

    def test_insurer_rows_meet_the_thresholds(self):
        for row in DATA["issuers"]:
            self.assertGreaterEqual(row["c"], 50_000, row["n"])
            if row["a"] is not None:
                self.assertGreaterEqual(row["a"], 30, row["n"])
                if row["o"] is not None:
                    self.assertLessEqual(row["o"], row["a"], row["n"])
            else:
                self.assertIsNone(row["o"], row["n"])

    def test_reasons_leave_out_the_out_of_network_column(self):
        # CMS defines it as closed-network denials; KFF leaves it out too.
        self.assertNotIn("Out of network", [r["k"] for r in DATA["reasons"]])
        self.assertEqual(
            DATA["reason_meta"]["entries"], sum(r["v"] for r in DATA["reasons"])
        )

    def test_no_2020_drug_total(self):
        # The 2024 file lists only drugs still dispensed in 2024.
        self.assertEqual(set(DATA["drugs"]["total"]), {"y2024"})

    def test_trend_is_in_year_order_with_leap_years(self):
        years = [t["year"] for t in DATA["trend"]]
        self.assertEqual(years, sorted(years))
        for t in DATA["trend"]:
            self.assertEqual(t["days"], 366 if t["year"] % 4 == 0 else 365)

    def test_win_rates_use_matched_counts(self):
        for t in DATA["trend"]:
            self.assertLessEqual(t["appeals"], t["appeals_all"])
            self.assertLessEqual(t["overturned"], t["appeals"])


class TestDenialWatchBuilder(TestCase):
    """Pure helpers in scripts/denial_watch/build_bundle.py (no pandas needed)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    def test_suppressed_cells_are_missing_not_zero(self):
        for cell in ("*", "**", "***", "N/A", None, ""):
            self.assertIsNone(self.b.num(cell))
        self.assertEqual(self.b.num("1,234"), 1234.0)
        self.assertEqual(self.b.num(7), 7.0)
        self.assertIsNone(self.b.num(True))

    def test_matched_appeals_skip_issuers_missing_either_count(self):
        issuers = [
            dict(ap_filed=100.0, ap_over=40.0),
            dict(ap_filed=50.0, ap_over=None),  # overturned count suppressed
            dict(ap_filed=None, ap_over=5.0),
        ]
        self.assertEqual(self.b.matched_appeals(issuers), (100.0, 40.0))

    def test_doubtful_won_counts_are_blanked_and_filed_kept(self):
        issuers = {k: dict(ap_filed=500.0, ap_over=0.0) for k in self.b.DOUBTFUL_WON[2026]}
        self.b.drop_doubtful_won(issuers, 2026)
        for row in issuers.values():
            self.assertEqual(row, dict(ap_filed=500.0, ap_over=None))

    def test_doubtful_won_list_is_only_for_its_plan_year(self):
        key = sorted(self.b.DOUBTFUL_WON[2026])[0]
        issuers = {key: dict(ap_filed=500.0, ap_over=0.0)}
        self.b.drop_doubtful_won(issuers, 2025)
        self.assertEqual(issuers[key]["ap_over"], 0.0)

    def test_all_claims_needs_both_sides(self):
        issuer = dict(rec_in=90.0, rec_out=10.0, den_in=9.0, den_out=5.0)
        self.assertEqual(self.b.side(issuer, "all"), (100.0, 14.0))
        issuer["den_out"] = None
        self.assertIsNone(self.b.side(issuer, "all"))

    def test_state_win_rate_needs_enough_appeals(self):
        base = dict(state="XX", rec_in=1000.0, den_in=100.0)
        few = self.b.state_rates([dict(base, ap_filed=10.0, ap_over=5.0)])
        self.assertIsNone(few["XX"]["won"])
        many = self.b.state_rates([dict(base, ap_filed=60.0, ap_over=30.0)])
        self.assertEqual(many["XX"]["won"], 0.5)
        self.assertEqual(many["XX"]["rate"], 0.1)


class TestDenialWatchPriorAuth(TestCase):
    """Prior-authorization rows are copied as posted, each with its source."""

    def test_every_row_links_its_source(self):
        rows = DATA["prior_auth"]
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertTrue(r["url"].startswith("https://"), r["n"])
            self.assertIn(r["line"], {"ma", "mcd", "mkt", "ffs"}, r["n"])
            if r["std"] is not None:
                self.assertTrue(0 <= r["std"] <= 1, r["n"])

    def test_every_row_says_what_it_counts(self):
        for r in DATA["prior_auth"]:
            self.assertIn(r["unit"], {"requests", "procedure codes"}, r["n"])

    def test_source_links_are_https_only(self):
        # The page refuses any other scheme too; this keeps the data honest.
        for r in DATA["prior_auth"]:
            self.assertRegex(r["url"], r"^https://[^\s\"<>]+$", r["n"])

    def test_builder_reads_the_curated_file(self):
        rows = _load_builder().build_prior_auth()
        self.assertEqual(rows, DATA["prior_auth"])

    def test_mississippi_row_says_its_site_was_down(self):
        # medicaid.ms.gov was in a vendor outage at launch; drop this note and test once its report opens again.
        row = next(r for r in DATA["prior_auth"] if r["n"] == "Mississippi Medicaid")
        self.assertIn("Mississippi's Medicaid website was down when we checked on October 19, 2026", row["note"])


class TestDenialWatchDrugUses(TestCase):
    """Each listed drug says what it's for, from a checked source."""

    def test_every_shown_drug_names_its_use(self):
        for tab in ("top", "per", "growth", "cheap"):
            for r in DATA["drugs"][tab]:
                self.assertTrue(1 <= len(r["use"].split()) <= 3, r["Brnd_Name"])

    def test_every_use_cites_medlineplus_or_the_fda_label(self):
        path = REPO / "scripts" / "denial_watch" / "drug_uses.csv"
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                self.assertRegex(
                    r["source_url"],
                    r"^https://(medlineplus\.gov|dailymed\.nlm\.nih\.gov|www\.accessdata\.fda\.gov)/",
                    r["brand"],
                )

    def test_builder_labels_from_the_curated_file(self):
        uses = _load_builder().load_drug_uses()
        for tab in ("top", "per", "growth", "cheap"):
            for r in DATA["drugs"][tab]:
                self.assertEqual(uses[r["Brnd_Name"]], r["use"])

    def test_builder_stops_on_an_unlabelled_drug(self):
        with self.assertRaises(SystemExit):
            _load_builder().attach_drug_uses({"top": [{"Brnd_Name": "Unlisted"}]}, {})


class TestDenialWatchIsEasyToFind(TestCase):
    """Once the page is live it is one click from everywhere; before, nowhere."""

    def _pages(self):
        return {
            name: Client().get(reverse(name)).content.decode()
            for name in ("root", "explain_denial", "about")
        }

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_every_page_links_to_it_when_live(self):
        link = f'href="{reverse("denial-watch")}"'
        for name, html in self._pages().items():
            self.assertIn(link, html, f"{name} has no link to Denial Watch")

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_each_place_links_to_it_when_live(self):
        # One check per place, so the footer can't stand in for the others.
        href = reverse("denial-watch")
        about = Client().get(reverse("about")).content.decode()
        home = Client().get(reverse("root")).content.decode()
        explain = Client().get(reverse("explain_denial")).content.decode()
        self.assertIn(f'<a href="{href}" class="fhi-nav-link" id="denialwatchlink">', about)
        self.assertIn(f'<a href="{href}">Denial Watch</a>', about)
        self.assertRegex(home, rf'(?s)id="denial-watch-home".*?href="{href}"')
        self.assertIn(f'<a href="{href}">See how often, on Denial Watch</a>', explain)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_home_strip_states_the_latest_number(self):
        latest = DATA["trend"][-1]
        html = Client().get(reverse("root")).content.decode()
        self.assertIn(
            f"Insurers denied about 1 in {round(1 / latest['in_rate'])} in-network claims on "
            f"HealthCare.gov plans in {latest['year']}",
            html,
        )

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_home_hero_announces_it_with_the_counters_pace(self):
        latest = DATA["trend"][-1]
        per_day = round(latest["in_denied"] / latest["days"])
        # 9am UTC on November 3 is still November 2, its last day, in Hawaii.
        last = datetime(2026, 11, 3, 9, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=last):
            html = Client().get(reverse("root")).content.decode()
        hero = _between(html, 'id="denial-watch-hero"', "</a>")
        self.assertIn(f'href="{reverse("denial-watch")}"', _between(html, '<a class="hero-announce"', ">"))
        self.assertIn(f'data-per-day="{per_day}"', hero)
        # It names insurers as the ones denying, so it can't read as Fight Health Insurance's denials.
        # Without script it gives the yearly pace; the script swaps in today's count.
        self.assertIn(
            f"Denial Watch: insurers denied {per_day:,} HealthCare.gov claims a day in {latest['year']}. See the numbers",
            _text(hero.split(">", 1)[1]),
        )
        self.assertIn('"HealthCare.gov claims today, at " + a.getAttribute("data-year")', html)
        # The label and the count change together, or "so far today" would sit beside the day's average.
        self.assertIn('a.querySelector(".hero-announce-count").textContent = count.toLocaleString("en-US");', html)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_home_hero_hands_off_to_the_strip_after_november_2(self):
        after = datetime(2026, 11, 3, 11, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=after):
            html = Client().get(reverse("root")).content.decode()
        self.assertNotIn('id="denial-watch-hero"', html)
        self.assertIn('id="denial-watch-home"', html)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_nav_marks_it_new_through_december_15_in_hawaii(self):
        # 9am UTC on December 16 is still December 15 in Hawaii.
        last = datetime(2026, 12, 16, 9, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=last):
            html = Client().get(reverse("about")).content.decode()
        self.assertIn('Denial Watch <span class="fhi-nav-new">New</span></a>', html)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_nav_drops_new_after_december_15(self):
        after = datetime(2026, 12, 16, 11, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=after):
            html = Client().get(reverse("about")).content.decode()
        self.assertIn('id="denialwatchlink">Denial Watch</a>', html)
        self.assertNotIn("fhi-nav-new", html)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_a_missing_data_file_hides_the_links_instead_of_breaking_pages(self):
        from fighthealthinsurance import views

        for cached in (views._denial_watch_data, views.denial_watch_teaser):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        with mock.patch.object(views, "read_static_text", return_value=None):
            for name in ("root", "explain_denial", "about"):
                response = Client().get(reverse(name))
                self.assertEqual(response.status_code, 200, name)
                self.assertNotIn(
                    f'href="{reverse("denial-watch")}"', response.content.decode(), name
                )

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=False)
    def test_nothing_links_to_it_before_launch(self):
        link = f'href="{reverse("denial-watch")}"'
        for name, html in self._pages().items():
            self.assertNotIn(link, html, f"{name} links to an unlaunched page")
            self.assertNotIn("Denial Watch", html, name)


class TestDenialWatchScriptCountsInNetwork(TestCase):
    """The tests above never run the script, so read which fields it uses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = (
            REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js"
        ).read_text()

    def test_counter_spreads_in_network_denials(self):
        self.assertIn("const perDay=T.in_denied/T.days", self.js)

    def test_appeal_ratio_uses_in_network_denials(self):
        self.assertIn("apShare=T.appeals_all/T.in_denied", self.js)

    def test_appeal_ratio_tile_rounds_to_tens_like_the_text(self):
        self.assertIn("fmt(Math.round(1/apShare/10)*10)", self.js)

    def test_win_rate_shows_the_latest_year_alone(self):
        # No tile or table column sets the win rate beside an earlier year's.
        self.assertIn("const T=D.trend[D.trend.length-1]", self.js)
        self.assertNotIn("down from", self.js)
        self.assertNotIn('<th class="dw-r">Won</th>', self.js)

    def test_insurer_table_shows_the_state_picked_on_the_map(self):
        # Rates are compared within a state, so the table shows one state, the map's, and no all-states ranking.
        self.assertNotIn("All states", self.js)
        self.assertIn("$('istate').value=BYST[dsel]?dsel:states[0];", self.js)
        self.assertIn("$('dmap')._onPin=s=>{ipin=s;$('istate').value=BYST[s]?s:'';iDraw();};", self.js)
        # A state with no insurers in the table says so, instead of leaving the last state's rows.
        self.assertIn("isn't in this data: it runs its own marketplace, or had no HealthCare.gov insurers reporting.", self.js)
        self.assertIn("if(svg._onPin)svg._onPin(g.dataset.s);", self.js)
        self.assertNotIn("highest and lowest 10", self.js)
        self.assertNotIn("slice(0,10)", self.js)

    def test_map_tiles_show_each_states_insurer_range_in_network_only(self):
        # The heatmap carries what the ranked bars did; the range is of in-network insurer rates.
        self.assertEqual(self.js.count("sub:spanLab"), 1)
        self.assertIn("in:{v:s=>ST[s]?ST[s].rate:null,b:RB,c:DC,l:x=>pct(x,0),t:x=>pct(x,1),sub:spanLab,", self.js)
        # The range covers the insurers in the table only, so both lines say so.
        self.assertIn("The small type on a tile is the lowest to highest rate among the state's insurers in the table below.", self.js)
        self.assertIn("Its insurers in the table: <b class=\"dw-num\">${pct(span(s)[0])}</b> to", self.js)
        self.assertIn("g.classList.toggle('dw-3',!!sub);", self.js)
        self.assertNotIn("dBars", self.js)

    def test_insurer_table_says_when_cms_withheld_the_win_count(self):
        self.assertIn("not reported (", self.js)

    def test_each_headline_number_has_a_label(self):
        for label in ("'claims denied'", "'denials per appeal'", "'appeals won'", "'reviews won'"):
            self.assertIn(label, self.js)

    def test_counter_says_what_it_counts(self):
        page = Client().get(reverse("denial-watch")).content.decode()
        self.assertIn("HealthCare.gov claims denied today, at 2024's pace", page)
        self.assertIn("denied per day in 2024, on average", page)

    def test_counter_can_be_paused(self):
        self.assertIn("prefers-reduced-motion", self.js)
        self.assertIn('id="tickpause"', Client().get(reverse("denial-watch")).content.decode())


class TestDenialWatchEnrollment(TestCase):
    """The open-enrollment box shows only in season; tests pin the date."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fighthealthinsurance.views import denial_watch_enrollment

        cls.season = staticmethod(denial_watch_enrollment)

    def test_medicare_line_runs_october_15_to_december_7(self):
        self.assertTrue(self.season(date(2026, 10, 15))["medicare"])
        self.assertTrue(self.season(date(2026, 12, 7))["medicare"])

    def test_box_stays_for_healthcare_gov_after_medicare_closes(self):
        self.assertFalse(self.season(date(2026, 12, 8))["medicare"])
        self.assertFalse(self.season(date(2027, 1, 15))["medicare"])

    def test_box_is_for_next_years_plans(self):
        season = self.season(date(2026, 10, 18))
        self.assertEqual((season["starts"], season["ends"], season["plan_year"]), (date(2026, 10, 15), date(2026, 12, 7), 2027))

    def test_box_is_hidden_out_of_season(self):
        self.assertIsNone(self.season(date(2026, 10, 14)))
        self.assertIsNone(self.season(date(2027, 1, 16)))

    def test_page_shows_the_box_in_season(self):
        html = _page(date(2026, 10, 18))
        box = _between(html, 'id="enroll"', "</section>")
        self.assertIn("Medicare open enrollment runs from October 15 to December 7, 2026", _text(box))
        self.assertIn('href="https://www.medicare.gov/plan-compare/"', box)
        self.assertIn('href="https://www.healthcare.gov/"', box)
        self.assertIn('href="#insurers"', box)
        self.assertIn('href="#prior-auth"', box)

    def test_box_sits_near_the_top(self):
        html = _page(date(2026, 10, 18))
        self.assertLess(html.index('id="enroll"'), html.index('class="fhi-jump-links"'))

    def test_insurer_section_points_to_healthcare_gov_in_season(self):
        html = _page(date(2026, 10, 18))
        section = _between(html, 'id="insurers"', "</section>")
        self.assertIn("Picking a 2027 HealthCare.gov plan?", section)

    def test_oregon_signs_up_at_its_own_marketplace(self):
        # From November 1, 2026, Oregon enrolls through ExploreHealthOR.gov, not HealthCare.gov.
        html = _page(date(2026, 10, 18))
        link = '<a href="https://explorehealthor.gov/">ExploreHealthOR.gov</a>'
        box = _between(html, 'id="enroll"', "</section>")
        self.assertIn(f"Oregon now has its own marketplace: for 2027 plans, Oregon residents sign up at {link}.", box)
        notice = _between(_between(html, 'id="insurers"', "</section>"), "Picking a 2027 HealthCare.gov plan?", "</p>")
        self.assertIn(f"Oregon residents compare 2027 plans at {link} instead.", notice)
        sources = _between(html, 'id="sources"', "</section>")
        self.assertIn('href="https://www.healthcare.gov/marketplace-in-your-state/"', sources)

    def test_shoppers_hear_that_some_insurers_have_left(self):
        # The table is 2024 data, and some of its insurers don't sell 2027 plans; no insurer is named.
        html = _page(date(2026, 10, 18))
        box = _text(_between(html, 'id="enroll"', "</section>"))
        self.assertIn("denied claims in 2024", box)
        self.assertIn("some of those insurers don't sell plans for 2027", box)
        notice = _between(_between(html, 'id="insurers"', "</section>"), "Picking a 2027 HealthCare.gov plan?", "</p>")
        self.assertIn("its insurers' 2024 rates", notice)
        self.assertIn("Some of these insurers don't sell plans for 2027, so check which ones do.", notice)
        self.assertNotIn("Cigna", box + notice)
        sources = _between(html, 'id="sources"', "</section>")
        self.assertIn("tracking-insurer-participation-changes-in-the-aca-marketplaces-in-2027", sources)

    def test_box_prints_no_healthcare_gov_dates(self):
        # The 2027 marketplace window is in court, so the box links out instead.
        box = _between(_page(date(2026, 10, 18)), 'id="enroll"', "</section>")
        for day in ("November 1", "December 15", "January 15"):
            self.assertNotIn(day, box)

    def test_page_drops_the_medicare_line_after_december_7(self):
        html = _page(date(2026, 12, 8))
        self.assertIn('id="enroll"', html)
        self.assertNotIn("Medicare open enrollment runs", html)

    def test_page_drops_medicare_plan_finder_after_december_7(self):
        box = _between(_page(date(2026, 12, 8)), 'id="enroll"', "</section>")
        self.assertNotIn("plan-compare", box)
        self.assertIn("Then compare plans and costs on HealthCare.gov.", _text(box))

    def test_box_dates_and_year_come_from_the_constants(self):
        # Next fall's update is two constants in views.py, not a hunt through the template.
        with (
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_MEDICARE_ENROLLMENT", (date(2027, 10, 15), date(2027, 12, 7))),
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_ENROLLMENT_BOX_ENDS", date(2028, 1, 15)),
        ):
            html = _page(date(2027, 10, 20))
        box = _text(_between(html, 'id="enroll"', "</section>"))
        self.assertIn("Picking a plan for 2028?", box)
        self.assertIn("runs from October 15 to December 7, 2027,", box)
        self.assertIn("Picking a 2028 HealthCare.gov plan?", _between(html, 'id="insurers"', "</section>"))
        self.assertNotIn("2026", box.replace("checked October 19, 2026", ""))

    def test_last_day_counts_in_hawaii_not_utc(self):
        # 5am UTC on December 8 is still December 7 in every state.
        late = datetime(2026, 12, 8, 5, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=late):
            html = Client().get(reverse("denial-watch")).content.decode()
        self.assertIn("Medicare open enrollment runs", html)

    def test_page_has_no_enrollment_box_after_the_season(self):
        html = _page(date(2027, 1, 16))
        self.assertNotIn('id="enroll"', html)
        self.assertNotIn("Picking a 2027 HealthCare.gov plan?", html)
        self.assertNotIn("<b>Open enrollment:</b>", html)


class TestDenialWatchWhoToCall(TestCase):
    """Where to turn, by kind of coverage, and from each state on the map."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        cls.js = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()

    def test_card_sits_in_what_you_can_do(self):
        section = _between(self.content, 'id="act"', "</section>")
        self.assertIn('id="who-to-call"', section)

    def test_card_links_each_office(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        for href in (
            "https://askebsa.dol.gov/",
            "https://www.medicare.gov/my/medicare-complaint",
            "https://content.naic.org/state-insurance-departments",
            "https://www.cms.gov/initiatives/your-patient-rights/medical-bill-rights",
            "https://www.shiphelp.org/",
            reverse("state_help_index"),
        ):
            self.assertIn(f'href="{href}"', card)

    def test_card_gives_each_phone_number(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        for phone in ("1-866-444-3272", "1-800-633-4227", "1-800-985-3059", "1-877-839-2675"):
            self.assertIn(phone, card)

    def test_medicare_denials_go_to_appeal_not_the_complaint_form(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        self.assertIn("To fight a denial, appeal with the steps in your notice.", card)

    def test_map_readout_links_to_the_state_help_pages(self):
        self.assertIn(f'data-state-help="{reverse("state_help_index")}"', self.content)
        self.assertIn("$('dread').innerHTML=h+helpLink(s)", self.js)

    def test_script_builds_the_slug_this_way(self):
        # The next test checks these slugs, so it has to be the script's rule.
        self.assertIn("NAMES[s].toLowerCase().replace(/ /g,'-')+'/'", self.js)

    def test_focus_alone_does_not_pick_a_map_state(self):
        # Tabbing out of the map would otherwise pick every tile after yours.
        for hook in ("'focus'", '"focus"', "focusin", "onfocus"):
            self.assertNotIn(hook, self.js)

    def test_enter_or_space_picks_a_map_state(self):
        # A keyboard pick pins the state, as a click does.
        self.assertIn("g.addEventListener('click',pin);", self.js)
        self.assertIn("g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();pin();}});", self.js)

    def test_leaving_the_map_goes_back_to_the_picked_state(self):
        # So the picked state's readout link can be reached after a hover.
        self.assertIn("svg.addEventListener('mouseleave',()=>{if(svg._pinned!==svg._sel)svg._pick(svg._pinned);});", self.js)

    def test_every_jump_lands_below_the_sticky_header(self):
        # Card and paragraph anchors, like #who-to-call, need the offset as much as sections do.
        style = _between(self.content, "<style>", "</style>")
        rules = re.findall(r"([^{}\n]+)\{ scroll-margin-top:", style)
        self.assertEqual([r.split("{")[-1].strip() for r in rules], [".dw [id]", ".dw [id]"])
        page = _between(self.content, '<div class="fhi-page-wide dw"', '<script id="dw-data"')
        for target in set(re.findall(r'href="#([\w-]+)"', page + self.js)):
            self.assertIn(f'id="{target}"', page, target)

    def test_phone_numbers_can_be_dialled(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        for href, shown in re.findall(r'href="tel:\+1(\d{10})">([^<]+)</a>', card):
            self.assertEqual(href, re.sub(r"\D", "", shown)[-10:], shown)
        self.assertEqual(len(re.findall(r'href="tel:', card)), 5)

    def test_ship_is_spelled_out(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        self.assertIn("State Health Insurance Assistance Program (SHIP)", card)

    def test_every_map_state_has_a_state_help_page(self):
        from fighthealthinsurance.state_help import load_state_help

        names = dict(re.findall(r"([A-Z]{2}):'([^']+)'", re.search(r"const NAMES=\{(.*?)\};", self.js).group(1)))
        self.assertEqual(len(names), 51)
        pages = load_state_help()
        for code, name in names.items():
            # The script's slug: the name, lower case, hyphens for spaces.
            slug = name.lower().replace(" ", "-")
            self.assertIn(slug, pages, code)
            self.assertEqual(reverse("state_help", kwargs={"slug": slug}), f"{reverse('state_help_index')}{slug}/")


class TestDenialWatchPlanClockAndPriorAuth(TestCase):
    """The plan's deadlines sit with prior authorization, which says it isn't every plan."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.section = _between(_page(), 'id="prior-auth"', "</section>")

    def test_clock_gives_the_deadlines_and_who_they_cover(self):
        clock = _text(_between(self.section, 'id="plan-clock"', "</div>"))
        for words in (
            "Starting in 2026, Medicare Advantage, Medicaid and Children's Health Insurance Program (CHIP) plans",
            "Urgent requests: 72 hours.",
            "Other requests: 7 days.",
            "up to 14 more days",
            "it counts as a denial you can appeal",
            "the specific reason",
            "15 days (72 hours if urgent)",
            "don't cover prescription drugs",
        ):
            self.assertIn(words, clock)

    def test_clock_names_who_can_opt_out_or_is_left_out(self):
        clock = _text(_between(self.section, 'id="plan-clock"', "</div>"))
        self.assertIn("A state-run CHIP program may follow its state's law instead.", clock)
        self.assertIn("state-run marketplaces (even those that sign people up through HealthCare.gov)", clock)

    def test_clock_cites_the_cms_rule(self):
        self.assertIn(
            'href="https://www.cms.gov/newsroom/fact-sheets/cms-interoperability-prior-authorization-final-rule-cms-0057-f"',
            self.section,
        )

    def test_section_says_it_shows_the_largest_plans_found(self):
        caveat = _between(self.section, 'id="pa-not-every-plan"', "</p>")
        self.assertIn("<b>Not every plan.</b>", caveat)
        self.assertIn(f"these {len(DATA['prior_auth'])} are the largest we found", caveat)

    def test_caveat_comes_before_the_bars(self):
        self.assertLess(self.section.index('id="pa-not-every-plan"'), self.section.index('id="pabars"'))

    def test_section_points_to_the_fuller_collections(self):
        caveat = _between(self.section, 'id="pa-not-every-plan"', "</p>")
        self.assertIn('<a href="https://www.authdenied.com/">AuthDenied</a> has a much fuller collection', caveat)
        # KFF covers the largest insurers, not more plans than ours.
        self.assertIn(
            f'<a href="{KFF_PA}">KFF</a> compares the largest insurers',
            caveat,
        )


class TestDenialWatchOthersAppeals(TestCase):
    """Lookups of how others' appeals went sit with independent review."""

    def test_lookups_link_california_new_york_and_propublica(self):
        section = _between(_page(), 'id="appeals"', "</section>")
        card = _between(section, 'id="others-appeals"', "</div>")
        for href in (
            "https://data.chhs.ca.gov/dataset/independent-medical-review-imr-determinations-trend",
            "https://www.dfs.ny.gov/public-appeal/search",
            "https://projects.propublica.org/claimfile/",
        ):
            self.assertIn(f'href="{href}"', card)

    def test_claim_file_helper_is_for_your_own_appeal(self):
        card = _between(_page(), 'id="others-appeals"', "</div>")
        self.assertIn("For your own appeal, <a", card)


class TestDenialWatchMedicareDrugs(TestCase):
    """Medicare drug help and the negotiated-price tags, from the curated file."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()
        cls.prices = cls.b.load_medicare_prices()
        cls.costs = _between(_page(), 'id="costs"', "</section>")

    def test_tags_match_the_curated_file(self):
        for r in SHOWN_DRUGS:
            if r["Brnd_Name"] in self.prices:
                self.assertEqual(r["medicare_price"], self.prices[r["Brnd_Name"]], r["Brnd_Name"])
            else:
                self.assertNotIn("medicare_price", r, r["Brnd_Name"])

    def test_every_curated_brand_is_a_shown_drug(self):
        # A misspelt brand would tag nothing.
        self.assertLessEqual(set(self.prices), {r["Brnd_Name"] for r in SHOWN_DRUGS})

    def test_every_price_row_cites_cms(self):
        with open(REPO / "scripts" / "denial_watch" / "medicare_prices.csv", newline="") as f:
            for r in csv.DictReader(f):
                self.assertRegex(r["source_url"], r"^https://www\.cms\.gov/", r["brand"])

    def _load_rows(self, *rows):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            f.write("brand,price_from,price_until,price_published,source_url,accessed,note\n")
            f.writelines(row + "\n" for row in rows)
        self.addCleanup(pathlib.Path(f.name).unlink)
        return self.b.load_medicare_prices(pathlib.Path(f.name))

    def test_loader_stops_on_a_published_flag_it_cant_read(self):
        with self.assertRaises(SystemExit):
            self._load_rows("Eliquis,2026,,Yes,https://www.cms.gov/x,2026-10-18,")

    def test_loader_stops_when_a_price_ends_before_it_starts(self):
        with self.assertRaises(SystemExit):
            self._load_rows("Xarelto,2027,2026,yes,https://www.cms.gov/x,2026-10-18,")

    def test_loader_reads_a_good_row(self):
        self.assertEqual(
            self._load_rows("Eliquis,2026,,yes,https://www.cms.gov/x,2026-10-18,"),
            {"Eliquis": {"from": 2026, "until": None, "label": "Medicare price from 2026"}},
        )

    def test_tags_say_the_year(self):
        label = self.b.medicare_price_label
        self.assertEqual(label(2026, None, True), "Medicare price from 2026")
        self.assertEqual(label(2026, 2026, True), "Medicare price in 2026 only")
        self.assertEqual(label(2028, None, False), "Picked for a Medicare price in 2028")

    def test_xarelto_price_ends_after_2026(self):
        # CMS removed it from 2027 because a generic is marketed.
        self.assertEqual(self.prices["Xarelto"]["label"], "Medicare price in 2026 only")

    def test_script_shows_the_tag_escaped_on_its_own_row(self):
        js = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()
        self.assertIn('<span class="dw-chip dw-bar-chip">${esc(d.medicare_price.label)}</span>', js)

    def test_script_hides_the_tag_note_where_no_drug_is_tagged(self):
        js = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()
        self.assertIn("$('dgprice').hidden=!rows.some(d=>d.medicare_price);", js)

    def test_tag_note_comes_before_the_list(self):
        self.assertLess(self.costs.index('id="dgprice"'), self.costs.index('id="dgbars"'))

    def test_tag_colours_are_readable(self):
        from tests.sync.test_contrast import contrast_ratio, parse_colour

        style = (REPO / "fighthealthinsurance" / "templates" / "denial_watch.html").read_text()
        rule = re.search(r"\.dw-chip \{([^}]*)\}", style).group(1)
        self.assertIn("background-color: var(--dw-u0)", rule)
        self.assertIn("color: var(--dw-u5)", rule)
        ink, ground = (parse_colour(re.search(rf"--{name}: (#[0-9a-f]{{6}})", style).group(1)) for name in ("dw-u5", "dw-u0"))
        self.assertGreaterEqual(contrast_ratio(ink[:3], ground[:3]), 4.5)

    def test_ending_price_note_shows_only_with_a_drug_whose_price_ends(self):
        js = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()
        self.assertIn("$('dgends').hidden=!rows.some(d=>d.medicare_price&&d.medicare_price.until);", js)
        self.assertIn('<span id="dgends">', self.costs)

    def test_page_says_the_negotiated_price_is_not_your_copay(self):
        note = _between(self.costs, 'id="dgprice"', "</p>")
        self.assertIn("not your copay", note)
        self.assertIn("or picked for a price that starts in 2028", note)
        self.assertIn("plus a pharmacy fee", note)
        self.assertIn(
            'href="https://www.medicare.gov/health-drug-plans/part-d/what-drug-plans-cover/how-drug-plans-work"', note
        )

    def test_drug_help_gives_the_cap_and_where_to_get_help(self):
        card = _between(self.costs, 'id="drug-help"', "</div>")
        self.assertIn("$2,100 in 2026 and $2,400 in 2027", card)
        for href in (
            "https://www.medicare.gov/basics/costs/help/drug-costs",
            "https://www.medicare.gov/prescription-payment-plan",
            "https://www.shiphelp.org/",
        ):
            self.assertIn(f'href="{href}"', card)


class TestDenialWatchMoreData(TestCase):
    """Where to find more data like this, just before the sources."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        cls.section = _between(cls.content, 'id="more-data"', "</section>")

    def test_listing_comes_just_before_sources(self):
        self.assertLess(self.content.index('id="gaps"'), self.content.index('id="more-data"'))
        self.assertLess(self.content.index('id="more-data"'), self.content.index('id="sources"'))

    def test_listing_links_authdenied_plaininsurer_and_kff(self):
        for href in (
            "https://www.authdenied.com/",
            "https://plaininsurer.com/states",
            "https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/",
        ):
            self.assertIn(f'href="{href}"', self.section)

    def test_each_group_name_is_a_link(self):
        names = re.findall(r'<h3 class="fhi-card-title"><a href="(https://[^"]+)">([^<]+)</a></h3>', self.section)
        self.assertEqual([n for _, n in names], ["AuthDenied", "PlainInsurer", "KFF"])

    def test_jump_menu_reaches_the_listing(self):
        nav = _between(self.content, 'class="fhi-jump-links"', "</nav>")
        self.assertIn('<a href="#more-data">More data and sources</a>', nav)

    def test_plaininsurer_line_says_why_its_rates_differ(self):
        self.assertIn("outside the plan's network", self.section)

    def test_join_forces_note_links_to_contact(self):
        self.assertIn('<p class="fhi-notice fhi-notice-info" id="join-forces">', self.section)
        note = _between(self.section, 'id="join-forces"', "</p>")
        self.assertIn("Know a public data source we should include? Send it our way.", note)
        self.assertIn("join forces", note)
        self.assertIn(f'href="{reverse("contact")}"', note)


class TestDenialWatchLinksAndStyle(TestCase):
    """Every outside link on the page is https, and the copy has no em dashes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        html = _page(date(2026, 10, 18))
        cls.body = _between(html, '<div class="fhi-page-wide dw"', '<script id="dw-data"')

    def test_outside_links_are_https(self):
        for href in re.findall(r'href="([^"]+)"', self.body):
            # Our own pages, anchors and phone numbers; "//host" is an outside link.
            if href.startswith(("#", "tel:+1")) or (href.startswith("/") and not href.startswith("//")):
                continue
            self.assertTrue(href.startswith("https://"), href)

    def test_no_em_dashes(self):
        self.assertNotIn("—", self.body)

    def test_new_facts_are_in_the_sources(self):
        sources = _between(self.body, 'id="sources"', "</section>")
        for href in (
            "https://www.cms.gov/files/document/2027-announcement.pdf",
            "https://www.cms.gov/files/document/ipay-2028-final-guidance.pdf",
            "https://www.federalregister.gov/documents/2024/02/08/2024-00895/medicare-and-medicaid-programs-patient-protection-and-affordable-care-act-advancing-interoperability",
            "https://www.cms.gov/newsroom/press-releases/medicare-advantage-medicare-prescription-drug-programs-expected-remain-stable-2027",
            "https://www.cms.gov/Medicare/Appeals-and-Grievances/MMCAG/Downloads/Parts-C-and-D-Enrollee-Grievances-Organization-Coverage-Determinations-and-Appeals-Guidance.pdf",
            "https://www.ecfr.gov/current/title-29/subtitle-B/chapter-XXV/subchapter-G/part-2560/section-2560.503-1",
            "https://www.ecfr.gov/current/title-42/chapter-IV/subchapter-C/part-438/subpart-F/section-438.406",
        ):
            self.assertIn(f'href="{href}"', sources)


JS = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()


def _short(name: str) -> str:
    """An insurer's name as the copy writes it, without ', Inc.'."""
    return re.sub(r",? Inc\.?$", "", name)


class TestDenialWatchNetworkData(TestCase):
    """In-network, out-of-network and all-claims figures, and why any is left out."""

    def test_2020_and_2021_are_in_network_only(self):
        # CMS's PY2023 data dictionary: that one claims count "Does not include out-of-network claims".
        for t in DATA["trend"]:
            self.assertIsNotNone(t["in_rate"], t["year"])
            for net in ("out", "all"):
                self.assertEqual(t[f"{net}_rate"] is not None, t["year"] >= 2022, (t["year"], net))

    def test_old_trend_keys_are_gone(self):
        # Nothing can read the series that joined in-network years to all-claims years again.
        for t in DATA["trend"]:
            self.assertFalse({"claims", "denied", "rate"} & set(t), t["year"])

    def test_national_rates_round_like_kff(self):
        latest = DATA["trend"][-1]
        self.assertEqual(
            (_pct(latest["in_rate"]), _pct(latest["out_rate"]), _pct(latest["all_rate"])),
            ("19%", "37%", "20%"),
        )

    def test_no_rate_above_one_in_any_view(self):
        rates = [t[f"{n}_rate"] for t in DATA["trend"] for n in ("in", "out", "all")]
        rates += [s["all"]["rate"] for s in DATA["states"].values()]
        rates += [i[n]["r"] for i in DATA["issuers"] for n in ("out", "all")]
        for r in rates:
            if r is not None:
                self.assertTrue(0 < r <= 1, r)

    def test_hidden_rates_say_why(self):
        cells = [i[n] for i in DATA["issuers"] for n in ("out", "all")]
        cells += [s["all"] for s in DATA["states"].values()]
        for c in cells:
            rate = c["r"] if "r" in c else c["rate"]
            self.assertEqual(rate is None, c["why"] is not None, c)

    def test_every_view_keeps_the_same_insurer_rows(self):
        # Switching views changes the numbers, never which insurers are listed.
        for i in DATA["issuers"]:
            self.assertGreaterEqual(i["c"], 50_000, i["n"])
            for net in ("out", "all"):
                self.assertEqual(set(i[net]), {"r", "c", "d", "why"}, i["n"])

    def test_out_of_network_rates_only_for_insurers_that_also_sell_ppo_or_pos(self):
        shown = [i for i in DATA["issuers"] if i["out"]["r"] is not None]
        self.assertGreater(len(shown), 0)
        for i in shown:
            self.assertIs(i["net_only"], False, i["n"])
            self.assertTrue({"PPO", "POS"} & set(i["pt"]), i["n"])
            self.assertGreaterEqual(i["out"]["c"], DATA["oon_meta"]["min_claims"], i["n"])

    def test_network_only_rows_say_so(self):
        for i in DATA["issuers"]:
            if i["net_only"] and i["out"]["why"] != "error":
                self.assertEqual(i["out"]["why"], "net_only", i["n"])

    def test_network_only_matches_plan_types(self):
        for i in DATA["issuers"]:
            expected = set(i["pt"]) <= {"HMO", "EPO"} if i["pt"] else None
            self.assertEqual(i["net_only"], expected, i["n"])

    def test_network_only_counts_add_up(self):
        meta = DATA["oon_meta"]
        counted = meta["net_only"]["issuers"] + meta["other"]["issuers"] + len(meta["unknown_plan_types"])
        self.assertEqual(counted, DATA["trend"][-1]["out_issuers"])

    def test_both_plan_type_groups_are_present(self):
        # The map's note reads both, and a null group would stop the whole script.
        for group in ("net_only", "other"):
            self.assertIsNotNone(DATA["oon_meta"][group], group)

    def test_state_plan_type_counts_fit_the_insurers(self):
        for st, s in DATA["states"].items():
            self.assertLessEqual(s["out"]["net_only"] + s["out"]["other"], s["out"]["issuers"], st)

    def test_closed_network_share_is_about_half(self):
        # The copy says "about half"; a refresh that moves it far needs new words.
        closed, latest = DATA["oon_meta"]["closed_network"], DATA["trend"][-1]
        self.assertAlmostEqual(closed["share"], closed["denied"] / latest["out_denied"])
        self.assertTrue(0.4 < closed["share"] < 0.6, closed["share"])

    def test_trend_rates_fit_the_chart_ranges(self):
        # The chart's y-axis is fixed per view, so a rate outside it would draw off the chart.
        ranges = dict(
            (view, (float(lo), float(hi)))
            for view, lo, hi in re.findall(r"(\w+):\{lab:'[^']+',ymin:([.\d]+),ymax:([.\d]+)", JS)
        )
        self.assertEqual(set(ranges), {"in", "out", "all"})
        for t in DATA["trend"]:
            for view, (lo, hi) in ranges.items():
                if t[f"{view}_rate"] is not None:
                    self.assertTrue(lo <= t[f"{view}_rate"] <= hi, (t["year"], view))

    def test_left_out_insurers_are_the_known_ones(self):
        b = _load_builder()
        for py, cy in b.CLAIMS_YEAR.items():
            t = next(t for t in DATA["trend"] if t["year"] == cy)
            self.assertEqual({(x["s"], x["id"]) for x in t["left_out"]}, b.KNOWN_DENIED_OVER_CLAIMS.get(py, set()), cy)
            for x in t["left_out"]:
                self.assertGreater(x["denied"], x["claims"], x["n"])

    def test_left_out_insurers_rows_say_why(self):
        latest = {(x["s"], x["n"]) for x in DATA["trend"][-1]["left_out"]}
        rows = [i for i in DATA["issuers"] if (i["s"], i["n"]) in latest]
        self.assertEqual(len(rows), len(latest))
        for i in rows:
            self.assertEqual((i["out"]["why"], i["all"]["why"]), ("error", "error"), i["n"])

    def test_states_with_an_impossible_insurer_show_no_all_claims_rate(self):
        gone = {x["s"] for x in DATA["trend"][-1]["left_out"]}
        self.assertEqual(gone, {"AK", "ND"})
        for st, s in DATA["states"].items():
            self.assertEqual(s["all"]["why"] == "error", st in gone, st)
            self.assertEqual(bool(s["all"]["left_out"]), st in gone, st)

    def test_all_claims_rate_sits_between_in_and_out(self):
        for t in DATA["trend"]:
            if t["out_rate"] is not None:
                self.assertTrue(t["in_rate"] <= t["all_rate"] <= t["out_rate"], t["year"])
        for i in DATA["issuers"]:
            if i["all"]["r"] is not None and i["out"]["c"]:
                out = i["out"]["d"] / i["out"]["c"]
                lo, hi = sorted((i["r"], out))
                self.assertTrue(lo - 1e-4 <= i["all"]["r"] <= hi + 1e-4, i["n"])


class TestDenialWatchNetworkBuilder(TestCase):
    """The builder's network rules, on made-up insurers (no pandas needed)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    def _row(self, **cells):
        """An insurer as read_puf returns it."""
        row = dict(state="XX", id=1, issuer="Crumpler Health Plan", rec_in=1000.0, den_in=100.0,
                   rec_out=2000.0, den_out=800.0, ap_filed=None, ap_over=None, closed=None)
        row.update(cells)
        return row

    def test_unsplit_columns_read_as_in_network(self):
        cells = self.b.issuer_cells(
            {"state": "XX", "issuer_name": " Crumpler Health Plan ", "issuer_id": 1,
             "issuer_claims_received": "1,000", "issuer_claims_denials": 150}
        )
        self.assertEqual((cells["rec_in"], cells["den_in"], cells["rec_out"], cells["den_out"]), (1000.0, 150.0, None, None))
        self.assertEqual(cells["issuer"], "Crumpler Health Plan")

    def test_split_columns_read_each_side(self):
        cells = self.b.issuer_cells(
            {"state": "XX", "issuer_name": "Crumpler Health Plan", "issuer_id": 1,
             "issuer_claims_received_in_network": 900, "issuer_claims_denied_in_network": 90,
             "issuer_claims_received_out_of_network": 100, "issuer_claims_denied_out_of_network": "**"}
        )
        self.assertEqual((cells["rec_in"], cells["den_in"], cells["rec_out"], cells["den_out"]), (900.0, 90.0, 100.0, None))

    def test_side_rejects_missing_cells_and_zero_claims(self):
        self.assertEqual(self.b.side(self._row(), "in"), (1000.0, 100.0))
        self.assertIsNone(self.b.side(self._row(den_out=None), "out"))
        self.assertIsNone(self.b.side(self._row(rec_out=0.0, den_out=0.0), "out"))
        self.assertIsNone(self.b.side(self._row(rec_out=None, den_out=None), "all"))

    def test_total_leaves_impossible_rows_out_of_out_and_all(self):
        rows = [self._row(), self._row(id=2, rec_out=100.0, den_out=150.0)]
        self.assertEqual(self.b.total(rows, "in")["issuers"], 2)
        self.assertEqual(self.b.total(rows, "out"), dict(claims=2000.0, denied=800.0, rate=0.4, issuers=1))
        self.assertEqual(self.b.total(rows, "all")["issuers"], 1)

    def test_network_only_needs_every_plan_hmo_or_epo(self):
        f = self.b.network_only
        self.assertTrue(f({"HMO"}))
        self.assertTrue(f({"HMO", "EPO"}))
        self.assertFalse(f({"HMO", "PPO"}))
        self.assertFalse(f({"POS"}))
        self.assertIsNone(f(set()))
        self.assertIsNone(f(None))

    def test_network_only_insurer_gets_no_out_of_network_rate(self):
        self.assertEqual(self.b.rate_cell(self._row(), "out", True), dict(r=None, c=2000, d=800, why="net_only"))

    def test_insurer_missing_from_the_plan_list_gets_no_out_of_network_rate(self):
        self.assertEqual(self.b.rate_cell(self._row(), "out", None)["why"], "types_unknown")

    def test_out_of_network_rate_needs_enough_claims(self):
        few = self.b.rate_cell(self._row(rec_out=999.0, den_out=10.0), "out", False)
        self.assertEqual((few["r"], few["why"]), (None, "few"))
        self.assertEqual(self.b.rate_cell(self._row(), "out", False), dict(r=0.4, c=2000, d=800, why=None))

    def test_impossible_insurer_gets_no_out_or_all_rate(self):
        row = self._row(rec_out=100.0, den_out=150.0)
        for net in ("out", "all"):
            self.assertEqual(self.b.rate_cell(row, net, False)["why"], "error", net)

    def test_state_all_rate_hidden_when_an_insurer_is_impossible(self):
        rows = [self._row(), self._row(id=2, issuer="Blackwell Health", rec_out=100.0, den_out=150.0)]
        view = self.b.state_views(rows, {})["XX"]
        self.assertEqual(view["all"], dict(rate=None, claims=None, denied=None, issuers=0, why="error", left_out=["Blackwell Health"]))
        self.assertEqual(view["out"]["left_out"], ["Blackwell Health"])

    def test_state_counts_network_only_insurers(self):
        rows = [self._row(), self._row(id=2)]
        types = {("XX", 1): {"HMO"}, ("XX", 2): {"HMO", "PPO"}}
        self.assertEqual(self.b.state_views(rows, types)["XX"]["out"], dict(issuers=2, net_only=1, other=1, left_out=[]))

    def test_closed_network_reason_adds_up_across_plan_rows(self):
        issuers = {}
        plan = {"state": "XX", "issuer_name": "Crumpler Health Plan", "issuer_id": 1,
                "issuer_claims_received_out_of_network": 100, "plan_type": "HMO"}
        self.b.add_plan_row(issuers, dict(plan, plan_number_claims_denied_due_to_out_of_network=30))
        self.b.add_plan_row(issuers, dict(plan, plan_number_claims_denied_due_to_out_of_network="*", plan_type="PPO"))
        self.b.add_plan_row(issuers, dict(plan, plan_number_claims_denied_due_to_out_of_network=12))
        row = issuers[("XX", 1)]
        self.assertEqual((row["closed"], row["types"], row["rec_out"]), (42.0, {"HMO", "PPO"}, 100.0))

    def test_closed_network_reason_is_none_in_files_without_it(self):
        issuers = {}
        self.b.add_plan_row(issuers, {"state": "XX", "issuer_name": "Crumpler Health Plan", "issuer_id": 1})
        self.assertIsNone(issuers[("XX", 1)]["closed"])

    def test_closed_network_share_uses_the_national_out_of_network_rows(self):
        rows = [self._row(closed=400.0), self._row(id=2, closed=50.0, rec_out=100.0, den_out=150.0)]
        meta = self.b.oon_meta(rows, {("XX", 1): {"HMO"}})
        # The impossible row stays out, as it does from the out-of-network total.
        self.assertEqual(meta["closed_network"], dict(denied=400.0, share=0.5))

    def test_unexpected_impossible_row_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self.b.check_impossible([self._row(rec_out=100.0, den_out=150.0)], 2022)

    def test_known_impossible_row_gone_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self.b.check_impossible([], 2026)

    def test_denials_against_zero_claims_stop_the_build(self):
        for cells in (dict(rec_out=0.0, den_out=5.0), dict(rec_in=0.0, den_in=5.0)):
            with self.subTest(**cells), self.assertRaises(SystemExit):
                self.b.check_impossible([self._row(**cells)], 2022)

    def test_impossible_list_is_per_plan_year(self):
        rows = [self._row(state=s, id=i, rec_out=100.0, den_out=150.0) for s, i in self.b.KNOWN_DENIED_OVER_CLAIMS[2026]]
        self.b.check_impossible(rows, 2026)
        with self.assertRaises(SystemExit):
            self.b.check_impossible(rows, 2025)

    def test_more_in_network_denials_than_claims_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self.b.check_impossible([self._row(den_in=2000.0)], 2022)


class TestDenialWatchNetworkPage(TestCase):
    """In-network opens every view, and every left-out figure says why where the reader sees it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        cls.sources = _text(_between(cls.content, 'id="sources"', "</section>"))

    def test_in_network_is_the_default_everywhere(self):
        for group in ("dmode", "tmode"):
            box = _between(self.content, f'id="{group}"', "</div>")
            self.assertIn('aria-pressed="true" data-m="in">In-network</button>', box)
            self.assertEqual(box.count('aria-pressed="true"'), 1, group)
        for start in ("let dm='in'", "let tv='in'"):
            self.assertIn(start, JS)
        self.assertIn("The map opens on in-network claims", self.content)

    def test_insurer_table_shows_in_network_rates_only(self):
        # Its out-of-network and all-claims rates would mostly show plan type (the mislead review).
        section = _between(self.content, 'id="insurers"', "</section>")
        self.assertNotIn('id="iview"', section)
        self.assertIn("In-network claims only (why).", _text(section))
        self.assertLess(section.index('id="inote"'), section.index('id="itable"'))
        table_js = _between(JS, "/* insurers:", "$('istate').onchange")
        self.assertNotRegex(table_js, r"\bi\.(out|all)\b")

    def test_insurer_note_says_hmo_or_epo_and_why(self):
        # The note says in-network only and links the why, which is in the method section.
        self.assertIn("We don't show them insurer by insurer either", self.sources)
        self.assertIn("many had most of their out-of-network claims on HMO or EPO plans", self.sources)
        self.assertIn('<div class="dw-method" id="method-what-why">', self.content)
        self.assertNotIn("the map's Out-of-network view", JS)

    def test_map_offers_no_out_of_network_views(self):
        # Out-of-network rates are national only, on the trend chart.
        buttons = re.findall(r'data-m="(\w+)"', _between(self.content, 'id="dmode"', "</div>"))
        self.assertEqual(buttons, ["in", "all", "chg", "won"])
        trend = re.findall(r'data-m="(\w+)"', _between(self.content, 'id="tmode"', "</div>"))
        self.assertEqual(trend, ["in", "out", "all"])
        # Why there are no out-of-network change or appeals figures is in the method section.
        self.assertIn("There's no out-of-network change from 2023, which mostly reflects which insurers reported, and no out-of-network appeals figure.", self.sources)

    def test_map_has_no_out_of_network_state_figures(self):
        # A state's rate would mostly show plan type, so the map points to the trend chart instead.
        views = _between(JS, "const DM={", "\n};")
        self.assertNotIn("out:{", views)
        self.assertIn("we don't map out-of-network claims by state (see What are they denying? below)", self.sources)
        self.assertIn("we don't map them by state: in 2024, 120 of the 157 insurers reporting them sold only HMO or EPO plans", self.sources)

    def test_out_of_network_trend_note_carries_the_map_note(self):
        # Everything the map's out-of-network view used to say, now beside the chart that has the figures.
        note = _between(JS, "(tv==='out'?", ":`All claims")
        self.assertIn("about twice the in-network rate", note)
        self.assertIn("${closedLine}", note)
        self.assertIn("Look for a PPO or POS.", note)
        self.assertIn('<a href="#who-to-call">Who to call</a>', note)
        self.assertIn('id="who-to-call"', self.content)
        # Why there's no state map, and which insurers are left out, are in the method section.
        self.assertNotIn("LEFT", JS)
        # The insurer table has no out-of-network view, so nothing links into one.
        self.assertNotIn("data-iview", JS)

    def test_map_outlines_states_a_view_leaves_out(self):
        # Grey is for states with no HealthCare.gov data; a left-out state looks different.
        self.assertIn("A state that a view leaves out has an outlined tile, and the line under the map says why.", self.content)
        self.assertIn(".dw-map g.dw-gone rect {", self.content)
        self.assertIn("gone=v==null&&goneOf(st)", JS)

    def test_no_network_only_label_left(self):
        # The page says "sold only HMO or EPO plans", which a reader can check on a plan.
        self.assertNotIn("network-only plans", _text(_without_data(self.content)))
        self.assertNotIn("network-only plans", JS)

    def test_trend_chart_label_says_in_network(self):
        self.assertIn('aria-label="Line chart of the in-network denial rate by year, 2020 to 2024"', self.content)
        self.assertIn("Line chart of the ${m.lab} denial rate by year, ${y0} to ${y1}", JS)

    def test_each_view_has_a_visible_line(self):
        # Under the map, under the trend chart and above the insurer table.
        self.assertLess(self.content.index('id="dleg"'), self.content.index('id="dnote"'))
        self.assertLess(self.content.index('id="tline"'), self.content.index('id="tnote"'))
        self.assertLess(self.content.index('id="inote"'), self.content.index('id="itable"'))
        self.assertIn("$('dnote').innerHTML=m.note", JS)
        self.assertIn('id="inote">In-network claims only (<a href="#method-what-why">why</a>).</p>', self.content)
        self.assertIn("$('tnote').innerHTML=", JS)

    def test_map_line_names_the_states_left_out_and_why(self):
        self.assertIn("show':'shows'} no figure here, because ${esc(cantBe(", JS)
        self.assertIn("reported more out-of-network denials than claims, which can\\'t be right", JS)

    def test_state_readout_says_why(self):
        self.assertIn("No all-claims figure for ${NAMES[s]}: ${esc(cantBe(a.left_out))}.", JS)
        self.assertIn("No all-claims figure: no insurer here reported both kinds of claims.", JS)

    def test_state_readout_has_no_out_of_network_lines(self):
        # With no out-of-network map view, the readout has no out-of-network branch.
        for gone in ("dm==='out'", "function outRead(", "function netLine("):
            self.assertNotIn(gone, JS)

    def test_out_of_network_trend_table_shows_claims_and_no_appeals(self):
        # Denials alone read as a jump; claims grew faster. Appeals aren't split by network.
        self.assertIn("${out?'<th class=\"dw-r\">Claims</th>':''}", JS)
        self.assertIn("${out?'':'<th class=\"dw-r\">Appeals</th>'}", JS)

    def test_out_of_network_is_about_twice_in_network(self):
        latest = DATA["trend"][-1]
        self.assertEqual(round(latest["out_rate"] / latest["in_rate"]), 2)
        self.assertIn("about twice the in-network rate", JS)

    def test_method_note_explains_the_out_of_network_limits(self):
        meta, latest = DATA["oon_meta"], DATA["trend"][-1]
        self.assertIn(
            f"in {meta['plan_types_year']}, {meta['net_only']['issuers']} of the {latest['out_issuers']} "
            "insurers reporting them sold only HMO or EPO plans",
            self.sources,
        )
        self.assertIn("We don't show them insurer by insurer either", self.sources)
        self.assertIn("The trend chart can switch to out-of-network claims or all claims, and the map to all claims", self.sources)
        self.assertIn("The insurer table has no all-claims rates for the same reason", self.sources)

    def test_method_note_gives_the_closed_network_figure(self):
        closed = DATA["oon_meta"]["closed_network"]
        self.assertIn(
            f"For {_millions(closed['denied'])} of those out-of-network denials, about half, insurers gave the reason",
            self.sources,
        )

    def test_method_note_names_the_left_out_insurers(self):
        for t in DATA["trend"]:
            for x in t["left_out"]:
                self.assertIn(_short(x["n"]), self.sources)
        self.assertIn("shows no figure for Alaska or North Dakota", self.sources)


class TestDenialWatchCharts(TestCase):
    """The line charts stay readable on a phone and keep their labels apart."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.style = _between(_page(), "<style>", "</style>")

    def test_phone_chart_text_is_enlarged(self):
        # The 560-wide drawing fills about 310px on a phone, which made 12px text under 7px.
        phone = _between(self.style, "@media (max-width: 560px)", "\n  }")
        self.assertIn(".dw-chart text { font-size: 20px; }", phone)
        self.assertIn(".dw-chart text.dw-end { font-size: 22px; }", phone)
        # The edge labels run a few pixels past the drawing at that size.
        self.assertIn(".dw-chart { overflow: visible; }", phone)

    def test_end_label_goes_under_a_falling_line(self):
        # Above the point, "18.7% denied" sat on the 2023 to 2024 line.
        self.assertIn("below=last>0&&pts[last-1].y>lp.y", JS)
        self.assertIn('y="${Y(lp.y)+(below?26:-12)}"', JS)
        self.assertIn('<text class="dw-end"', JS)

    def test_last_year_label_is_not_crowded(self):
        # California's chart showed "2024 2025" side by side.
        self.assertIn("if(i===last||(i%step===0&&last-i>=step))", JS)


THEMES = [
    ("why-appeal", "Why should I appeal?", "method-why-appeal"),
    ("who-denies", "Who is denying the most?", "method-who-denies"),
    ("what-why", "What are they denying, and why?", "method-what-why"),
    ("coverage", "Who doesn't have coverage?", "method-coverage"),
]


class TestDenialWatchThemes(TestCase):
    """Four questions carry the page, each short, with its method one link away."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        cls.sources = _between(cls.content, 'id="sources"', "</section>")

    def _theme(self, ident):
        # A theme holds sub-sections, so it runs to the next theme's start.
        i = self.content.index(f'id="{ident}"')
        return self.content[i : self.content.index('<section class="fhi-stack', self.content.index("</h2>", i))]

    def test_themes_come_in_order_with_the_question_as_heading(self):
        at = [self.content.index(f'<section class="fhi-stack dw-theme" id="{i}"') for i, _, _ in THEMES]
        self.assertEqual(at, sorted(at))
        for ident, question, _ in THEMES:
            self.assertIn(f'<h2 id="{ident}-title">{question}</h2>', self.content)

    def test_jump_menu_reaches_each_theme(self):
        nav = _between(self.content, 'class="fhi-jump-links"', "</nav>")
        for ident, _, _ in THEMES:
            self.assertIn(f'href="#{ident}"', nav)

    def test_each_theme_links_its_method(self):
        for ident, question, method in THEMES:
            self.assertIn(f'<p class="dw-how"><a href="#{method}">How we got these numbers<span class="fhi-visually-hidden">: {question}</span></a></p>', self._theme(ident))
            self.assertIn(f'<div class="dw-method" id="{method}">', self.sources)
        self.assertIn('<p class="dw-how"><a href="#method-costs">How we got these numbers<span class="fhi-visually-hidden">: Drug costs</span></a></p>', _between(self.content, 'id="costs"', "</section>"))

    def test_method_groups_link_back_to_their_theme(self):
        for ident, question, method in THEMES:
            group = _between(self.sources, f'id="{method}"', "</div>")
            self.assertIn(f'<p class="dw-back"><a href="#{ident}">Back to {question}</a></p>', group)

    def test_sources_have_no_nested_sections(self):
        # Tests and readers take the method section as one block.
        self.assertNotIn("<section", self.sources[1:])

    def test_counter_and_four_numbers_open_the_page(self):
        # The counter, then the four tiles, before the enrollment box, the menu and the questions.
        order = ['class="dw-ticker"', 'id="stats"', 'id="enroll"', 'class="fhi-jump-links"', 'id="why-appeal"']
        at = [self.content.index(m) for m in order]
        self.assertEqual(at, sorted(at))
        self.assertNotIn("oddsrows", self.content)
    def test_insurer_heading_matches_the_spread_inside_states(self):
        # "Four or more times": the widest gaps in the table, state by state.
        by_state = {}
        for i in DATA["issuers"]:
            by_state.setdefault(i["s"], []).append(i["r"])
        ratios = [max(v) / min(v) for v in by_state.values()]
        self.assertGreaterEqual(sum(r >= 4 for r in ratios), 2, sorted(ratios)[-5:])
        self.assertIn(
            '<h3 id="insurers-title">In some states, one insurer denies four or more times as often as another</h3>',
            self.content,
        )

    def test_coverage_answer_matches_the_census_figures(self):
        year = _census_year()
        sahie = DATA["sahie"][year]
        total = sum(r["n"] for r in sahie.values())
        people = sum(r["n"] / (r["pct"] / 100) for r in sahie.values())
        low = min(sahie.items(), key=lambda kv: kv[1]["pct"])
        high = max(sahie.items(), key=lambda kv: kv[1]["pct"])
        self.assertEqual(round(people / total), 10)
        self.assertIn(
            f"About {round(total / 1e6)} million people under 65 in {year}, about 1 in 10, from {low[1]['pct']}% in {low[0]} to {high[1]['pct']}% in {high[0]}.",
            self._theme("coverage"),
        )


MK = DATA["marketplace"]
KFF_MARKET = "https://www.kff.org/affordable-care-act/individual-market-enrollment-continues-to-fall-through-mid-2026/"
KFF_SURVEY = "https://www.kff.org/public-opinion/a-follow-up-survey-of-aca-marketplace-enrollees/"
KFF_SENTENCE = "Enrollment in the individual health insurance market fell 18% between June 2025 and June 2026."
CMS_EFFECTUATED = "https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/health-insurance-marketplace/health-insurance-exchanges-monthly-effectuated-enrollment"
CMS_PREMIUM = "plan selections will only become coverage for consumers that effectuate their coverage by paying their first monthly premium"
KFF_MARKET_DATE = "October 14, 2026"
KFF_JUNE = (25.2, 20.5)  # millions, June 2025 and June 2026, from the KFF analysis
# CMS's early 2025 effectuated report counted February 2025 on March 15, 2025, sooner than the file the page uses.
EARLY_FEB_2025 = 23_393_198


def _census_year() -> str:
    return max(DATA["sahie"], key=int)


def _picks() -> dict[int, int]:
    return {p["y"]: p["n"] for p in MK["picks"]}


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


def _february() -> dict[int, int]:
    return {r["y"]: r["n"] for r in MK["feb"]}


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


class TestDenialWatchMarketplaceData(TestCase):
    """February coverage in effect, every year from one CMS file, and the plan picks beside it."""

    def test_years_run_from_2016_without_a_gap(self):
        years = [r["y"] for r in MK["feb"]]
        self.assertEqual(years, list(range(2016, years[-1] + 1)))

    def test_only_the_latest_year_is_preliminary(self):
        self.assertEqual(MK["prelim"], MK["feb"][-1]["y"])

    def test_2025_is_the_final_count_not_the_early_snapshot(self):
        # Mixing in the early report's count fakes an 18% drop.
        self.assertEqual(_february()[2025], 21_776_626)
        self.assertGreater(EARLY_FEB_2025, _february()[2025])

    def test_sources_give_the_years_shown(self):
        years = {}
        for s in DATA["sources"]:
            years.setdefault(s["url"], s["years"])
        self.assertEqual(years[CMS_EFFECTUATED], f"February {MK['feb'][0]['y']}-{MK['feb'][-1]['y']}")
        for p in MK["picks"]:
            self.assertEqual(years[p["url"]], str(p["y"]))

    def test_file_and_date_are_the_ones_the_builder_checks(self):
        b = _load_builder()
        self.assertEqual((MK["file"], MK["as_of"]), (b.EFFECTUATED_FILE, b.EFFECTUATED_AS_OF))
        self.assertIn("asof_" + MK["as_of"].replace("-", ""), MK["file"])

    def test_every_year_has_minnesotas_count(self):
        for r in MK["feb"]:
            self.assertTrue(0 < r["mn"] < r["n"], r["y"])

    def test_plan_picks_have_a_february_count_and_a_cms_page(self):
        feb = _february()
        # The page compares the last two Februaries, each with that year's plan picks.
        self.assertEqual([p["y"] for p in MK["picks"]], [r["y"] for r in MK["feb"][-2:]])
        for p in MK["picks"]:
            self.assertIn(p["y"], feb)
            self.assertTrue(feb[p["y"]] < p["n"], p["y"])
            self.assertTrue(p["url"].startswith("https://www.cms.gov/"), p["y"])

    def test_builder_reads_the_curated_plan_picks(self):
        self.assertEqual(_load_builder().load_marketplace_signups(), MK["picks"])


class TestDenialWatchMarketplaceBuilder(TestCase):
    """The builder's February rules, on made-up rows shaped like CMS's CSV (no pandas needed)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    def _rows(self, years, no_december=()):
        rows = []

        def add(y, month, state, platform, n):
            rows.append({"State": state, "Platform": platform, "Year": str(y), "Month": month, "Effectuated_Enr": str(n)})

        for y in years:
            add(y, "February", "Total", "All", 300 + y)
            add(y, "February", "Total", "HealthCare.gov", 200 + y)
            add(y, "January", "Total", "All", 1)
            add(y, "February", "MN", "SBE", 100)
            add(y, "February", "TX", "HealthCare.gov", 200 + y)
            if y not in no_december:
                add(y, "December", "Total", "All", 1)
        return rows

    def test_keeps_only_the_national_february_count(self):
        feb, prelim = self.b.february_totals(self._rows([2016, 2017], no_december=[2017]))
        self.assertEqual(feb, [dict(y=2016, n=2316, mn=100), dict(y=2017, n=2317, mn=100)])
        self.assertEqual(prelim, 2017)

    def test_a_year_with_its_december_is_final(self):
        self.assertIsNone(self.b.february_totals(self._rows([2016, 2017]))[1])

    def test_stops_on_a_gap_in_the_years(self):
        with self.assertRaises(SystemExit):
            self.b.february_totals(self._rows([2016, 2018], no_december=[2018]))

    def test_stops_when_an_earlier_year_has_no_december(self):
        with self.assertRaises(SystemExit):
            self.b.february_totals(self._rows([2016, 2017], no_december=[2016, 2017]))

    def test_stops_when_the_states_dont_add_up(self):
        rows = self._rows([2016])
        next(r for r in rows if r["State"] == "TX")["Effectuated_Enr"] = "1"
        with self.assertRaises(SystemExit):
            self.b.february_totals(rows)

    def test_stops_on_a_national_february_count_of_zero(self):
        # States that add up to 0, so only the count check can stop it.
        rows = self._rows([2016, 2017], no_december=[2017])
        next(r for r in rows if r["Year"] == "2016" and r["State"] == "Total" and r["Platform"] == "All" and r["Month"] == "February")["Effectuated_Enr"] = "0"
        next(r for r in rows if r["Year"] == "2016" and r["State"] == "TX")["Effectuated_Enr"] = "-100"
        with self.assertRaisesRegex(SystemExit, "February 2016 has a national count of 0"):
            self.b.february_totals(rows)

    def _temp(self, text, suffix):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, newline="") as f:
            f.write(text)
        self.addCleanup(pathlib.Path(f.name).unlink)
        return pathlib.Path(f.name)

    def test_stops_on_a_cms_file_it_wasnt_written_for(self):
        # Rows that would build, so only the file check can stop it.
        rows = self._rows([2025, 2026], no_december=[2026])
        lines = ['"State","Platform","Year","Month","Effectuated_Enr"'] + [",".join(r.values()) for r in rows]
        path = self._temp("\n".join(lines) + "\n", ".csv")
        with self.assertRaises(SystemExit):
            self.b.build_marketplace(path)

    PICK = dict(
        year="2026",
        plan_selections="1",
        source_url="https://www.cms.gov/x",
        file="https://www.cms.gov/files/zip/x.zip",
        file_sha256="0" * 64,
        accessed="2026-10-22",
    )

    def _picks(self, **change):
        row = {**self.PICK, **change}
        return self._temp(",".join(row) + "\n" + ",".join(row.values()) + "\n", ".csv")

    def test_a_complete_plan_pick_row_loads(self):
        self.assertEqual(self.b.load_marketplace_signups(self._picks()), [dict(y=2026, n=1, url="https://www.cms.gov/x")])

    def test_plan_picks_must_cite_a_cms_page(self):
        with self.assertRaisesRegex(SystemExit, ": source_url must"):
            self.b.load_marketplace_signups(self._picks(source_url="https://example.org/x"))

    def test_plan_picks_must_name_the_cms_zip_its_sha256_and_when(self):
        for column, bad in (
            ("file", ""),
            ("file", "https://example.org/x.zip"),
            ("file", "https://www.cms.gov/files/document/x.pdf"),
            ("file_sha256", ""),
            ("file_sha256", "4538f7f8"),
            ("accessed", ""),
            ("accessed", "October 22"),
        ):
            with self.subTest(column=column, bad=bad), self.assertRaisesRegex(SystemExit, f": {column} must"):
                self.b.load_marketplace_signups(self._picks(**{column: bad}))

    def test_plan_picks_must_be_a_count_above_zero_for_a_year(self):
        for column, bad in (
            ("plan_selections", "0"),
            ("plan_selections", "-5"),
            ("plan_selections", ""),
            ("plan_selections", "1.5"),
            ("plan_selections", "1_000"),
            ("plan_selections", "023130860"),
            ("year", ""),
            ("year", "26"),
            ("year", "2026.0"),
        ):
            with self.subTest(column=column, bad=bad), self.assertRaisesRegex(SystemExit, f": {column} must"):
                self.b.load_marketplace_signups(self._picks(**{column: bad}))

    def _picks_rows(self, *years):
        rows = [{**self.PICK, "year": str(y)} for y in years]
        lines = [",".join(self.PICK)] + [",".join(r.values()) for r in rows]
        return self._temp("\n".join(lines) + "\n", ".csv")

    def test_plan_picks_stop_on_two_rows_for_a_year(self):
        with self.assertRaisesRegex(SystemExit, ": two rows for 2026"):
            self.b.load_marketplace_signups(self._picks_rows(2025, 2026, 2026))

    def test_plan_picks_stop_on_a_file_with_no_rows(self):
        with self.assertRaisesRegex(SystemExit, ": no plan picks"):
            self.b.load_marketplace_signups(self._picks_rows())

    def _dir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return pathlib.Path(d.name)

    def _zip_dir(self, content):
        d = self._dir()
        if content is not None:
            (d / "x.zip").write_bytes(content)
        return d

    def test_plan_picks_zip_in_the_raw_directory_must_match_its_sha256(self):
        content = b"a CMS zip"
        ok = hashlib.sha256(content).hexdigest()
        loaded = [dict(y=2026, n=1, url="https://www.cms.gov/x")]
        self.assertEqual(self.b.load_marketplace_signups(self._picks(file_sha256=ok), zip_dir=self._zip_dir(content)), loaded)
        with self.assertRaisesRegex(SystemExit, "not the file_sha256 recorded"):
            self.b.load_marketplace_signups(self._picks(file_sha256=ok), zip_dir=self._zip_dir(b"another file"))
        # A zip that isn't there is reported as unchecked, not refused.
        self.assertEqual(self.b.load_marketplace_signups(self._picks(file_sha256=ok), zip_dir=self._zip_dir(None)), loaded)

    def _feb(self, *years):
        return [dict(y=y, n=1000 + y, mn=1) for y in years]

    def _pick(self, *years):
        return [dict(y=y, n=2000 + y, url="https://www.cms.gov/x") for y in years]

    def test_plan_picks_cover_the_two_februaries_the_page_compares(self):
        entry = self.b.marketplace_entry(self._feb(2024, 2025, 2026), 2026, self._pick(2025, 2026))
        self.assertEqual((entry["prelim"], [p["y"] for p in entry["picks"]]), (2026, [2025, 2026]))
        for picks in (self._pick(2026), self._pick(2025), self._pick(2024, 2026), []):
            with self.subTest(picks=[p["y"] for p in picks]), self.assertRaisesRegex(SystemExit, "plan picks have no row for"):
                self.b.marketplace_entry(self._feb(2024, 2025, 2026), 2026, picks)

    def test_plan_picks_need_a_february_to_compare_with(self):
        with self.assertRaisesRegex(SystemExit, r"plan picks for \[2027\] have no February count"):
            self.b.marketplace_entry(self._feb(2025, 2026), 2026, self._pick(2025, 2026, 2027))

    def test_one_february_is_not_enough_to_compare(self):
        with self.assertRaisesRegex(SystemExit, "compares two Februaries"):
            self.b.marketplace_entry(self._feb(2026), 2026, self._pick(2026))

    def test_report_gives_each_change_and_share(self):
        mk = dict(feb=[dict(y=2025, n=200, mn=1), dict(y=2026, n=150, mn=1)], prelim=2026, picks=self._pick(2026))
        mk["picks"][0]["n"] = 300
        self.assertEqual(
            self.b.marketplace_report(mk),
            ["marketplace February 2026 150 -25.00% not final", "plan picks 2026 300 February coverage 50.0% of them"],
        )

    def test_nothing_is_written_when_the_report_cant_be_worked_out(self):
        # A count of 0 that got past the checks still stops the build before the bundle is written.
        raw = self._dir()
        names = [f"tic{py}.xlsx" for py in self.b.CLAIMS_YEAR] + [
            "imr.csv", "partd.csv", "sahie_2023.csv", "sahie_2024.csv", "places_county.csv", "effectuated_monthly.csv"
        ]
        for n in names:
            (raw / n).write_text("")
        out = raw / "out.json"
        mk = dict(feb=[dict(y=2025, n=0, mn=1), dict(y=2026, n=150, mn=1)], prelim=2026, picks=[])
        stubs = dict(build_denials={}, build_imr={}, build_drugs={}, build_sahie={}, build_places={}, correlations={}, build_prior_auth=[])
        with contextlib.ExitStack() as stack:
            # main() points RAW at the raw directory; put it back afterwards.
            stack.enter_context(mock.patch.object(self.b, "RAW", self.b.RAW))
            for name, value in stubs.items():
                stack.enter_context(mock.patch.object(self.b, name, return_value=value))
            stack.enter_context(mock.patch.object(self.b, "build_marketplace", return_value=mk))
            with self.assertRaises(ZeroDivisionError):
                self.b.main(["--raw-dir", str(raw), "--out", str(out)])
        self.assertFalse(out.exists())


class TestDenialWatchMarketplacePage(TestCase):
    """The marketplace section's words, chart and tables agree with the data and say where they come from."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        cls.section = _between(cls.content, 'id="marketplace-trend"', "</section>")
        cls.text = _text(cls.section)
        method = _between(cls.content, 'id="method-coverage"', "</div>")
        cls.method = _text(method[method.index("<b>Marketplace coverage:</b>") : method.index("</ol>")])
        cls.method_html = method
        cls.script = _between(JS, "/* marketplace coverage:", "})();")

    def test_section_sits_inside_coverage_after_the_map(self):
        self.assertEqual(_section_parents(self.content)["marketplace-trend"], "coverage")
        self.assertLess(self.content.index('id="cmap"'), self.content.index('id="marketplace-trend"'))
        self.assertIn('<section class="fhi-stack" id="marketplace-trend" aria-labelledby="marketplace-trend-title">', self.content)

    def test_no_id_is_used_twice_in_season_or_out(self):
        # The season box already owns id="enroll", so this section names itself differently.
        self.assertNotIn('id="enroll', self.section)
        for day in (date(2026, 10, 18), date(2027, 1, 16)):
            body = _between(_page(day), '<div class="fhi-page-wide dw"', '<script id="dw-data"')
            ids = re.findall(r'\sid="([^"]+)"', body)
            self.assertEqual(len(ids), len(set(ids)), sorted(i for i in ids if ids.count(i) > 1))

    def test_heading_states_the_biggest_drop_from_the_data(self):
        changes, feb = _yearly_changes(), _february()
        last, first = MK["feb"][-1]["y"], MK["feb"][0]["y"]
        self.assertEqual(min(changes, key=changes.get), last)
        self.assertEqual(min(changes, key=lambda y: feb[y] - feb[y - 1]), last)
        self.assertIn(
            f'<h3 id="marketplace-trend-title">Marketplace coverage fell {round(-changes[last] * 100)}% in {last}, '
            f"the biggest drop in CMS's monthly count, which goes back to {first}</h3>",
            self.section,
        )

    def test_the_only_other_drop_is_small(self):
        changes, last = _yearly_changes(), MK["feb"][-1]["y"]
        others = [y for y, c in changes.items() if c < 0 and y != last]
        self.assertEqual(len(others), 1, others)
        self.assertLess(abs(changes[others[0]]), 0.01)
        self.assertIn(f"The only other drop was in {others[0]}, and it was less than 1%.", self.text)

    def test_intro_gives_the_counts(self):
        a, b = MK["feb"][-2], MK["feb"][-1]
        self.assertLess(b["n"], a["n"])
        self.assertIn(f"February is the latest month CMS has published for {b['y']}, so every year is compared on that month.", self.text)
        self.assertIn(
            f"From February {a['y']} to February {b['y']} the count fell from {_millions(a['n'])} to {_millions(b['n'])}, "
            f"about {_millions(a['n'] - b['n'])} fewer people.",
            self.text,
        )

    def test_chart_label_before_the_script_runs_names_the_years(self):
        first, last, prelim = MK["feb"][0]["y"], MK["feb"][-1]["y"], MK["prelim"]
        self.assertIn(
            f'aria-label="Line chart of people with marketplace coverage in effect each February, {first} to {last}. {prelim} is preliminary"',
            self.section,
        )

    def test_latest_year_is_marked_preliminary(self):
        prelim = MK["prelim"]
        self.assertIn(f"The hollow point, {prelim}, is preliminary.", _text(_between(self.section, 'id="mknote"', "</p>")))
        self.assertIn(f"The {prelim} count isn't final yet", self.text)
        self.assertIn(f"so {prelim} is preliminary and may change", self.method)
        self.assertIn("open:p=>p.x===MK.prelim", self.script)
        self.assertIn("fill=\"${o?'#ffffff':color}\" stroke=\"${o?color:'#ffffff'}\"", JS)
        self.assertIn("(preliminary)", self.script)

    def test_chart_starts_at_zero_and_fits_every_year(self):
        call = re.search(r"line\(\$\('mkline'\),.*?\{ymin:(\d+),ymax:([\d.e]+),ticks:\[([^\]]+)\]", self.script)
        self.assertIsNotNone(call)
        ymin, ymax = float(call.group(1)), float(call.group(2))
        self.assertEqual(ymin, 0)
        self.assertEqual(float(call.group(3).split(",")[-1]), ymax)
        self.assertTrue(all(r["n"] <= ymax for r in MK["feb"]))
        self.assertIn("color:css('--dw-blue')", self.script)

    def test_script_draws_last_and_only_with_data(self):
        # Last in the script, so a throw here can't stop another chart.
        self.assertIn("const MK=D.marketplace;\nif(MK){", JS)
        self.assertEqual(JS.index("/* marketplace coverage:"), JS.rindex("/* "))

    def test_tables_are_accessible(self):
        picks = _between(self.section, 'id="mkpicks"', "</table>")
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

        table = _between(self.section, 'id="mkpicks"', "</table>")
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
        self.assertIn(f"Coverage in effect in February came to {s0} of those people for {y0}, and {s1} for {y1}.", self.text)
        self.assertIn(f"The {s0} and {s1} divide each February's coverage by that year's plan picks.", self.method)
        self.assertIn(f"CMS's early 2025 report gave {_pct(EARLY_FEB_2025 / picks[2025])}, from its earlier February count.", self.method)

    def test_picking_a_plan_is_explained_and_cms_quoted(self):
        self.assertIn("Picking a plan isn't the same as having coverage.", self.text)
        self.assertIn("only becomes coverage once the first month's premium is paid, if there is one.", self.text)
        self.assertIn(f'"{CMS_PREMIUM}"', self.method)
        self.assertIn('href="https://www.cms.gov/files/document/2026-public-use-files-faqs.pdf"', self.method_html)

    def test_kff_sentence_is_quoted_exactly_and_linked(self):
        quote = _between(self.section, '<blockquote class="dw-quote"', "</blockquote>")
        self.assertIn(f'cite="{KFF_MARKET}"><p>{KFF_SENTENCE}</p>', quote)
        self.assertIn(f'href="{KFF_MARKET}"', self.section)
        self.assertIn(f"From KFF's analysis of {KFF_MARKET_DATE}.", self.text)
        self.assertIn(f"(KFF, {KFF_MARKET_DATE})", self.method)

    def test_kff_counts_agree_with_its_quoted_drop(self):
        june25, june26 = KFF_JUNE
        self.assertIn(f"{june25} million in June 2025 and {june26} million in June 2026", self.method)
        # The quote rounds to a whole percent, the counts to a tenth of a million: their ranges must overlap.
        drop = int(re.search(r"fell (\d+)%", KFF_SENTENCE).group(1)) / 100
        low, high = 1 - (june26 + 0.05) / (june25 - 0.05), 1 - (june26 - 0.05) / (june25 + 0.05)
        self.assertTrue(low < drop + 0.005 and high >= drop - 0.005, (low, high))
        self.assertIn("KFF, a nonprofit health policy research, polling, and news organization,", self.text)

    def test_plan_picks_count_people_including_automatic_renewals(self):
        # CMS's count is of people, and it includes people renewed without picking again.
        self.assertIn("CMS counts everyone with a plan picked, including people renewed into one automatically.", self.text)
        self.assertIn("CMS counts people, not plans, including people renewed into a plan automatically.", self.method)
        self.assertNotIn("of the plans picked", self.text)
        rows = re.findall(r'<th scope="row">([^<]+)</th>', _between(self.section, 'id="mkpicks"', "</table>"))
        self.assertEqual(rows, ["People with a plan picked in open enrollment", "People with coverage in effect in February"])

    def test_kff_is_described_the_way_it_asks_everywhere(self):
        self.assertNotIn("independent health policy research group", self.content)
        more = _text(_between(self.content, '<a href="https://www.kff.org/">KFF</a>', "</li>"))
        self.assertIn("A nonprofit health policy research, polling, and news organization.", more)

    def test_kff_differences_are_named(self):
        self.assertIn("on the marketplace or off it", self.text)
        self.assertIn("compares June with June", self.text)
        self.assertIn("so the two numbers aren't meant to match", self.text)

    def test_no_link_to_a_kff_pdf(self):
        for href in re.findall(r'href="(https://www\.kff\.org/[^"]*)"', self.content):
            self.assertFalse(href.lower().endswith(".pdf"), href)

    def test_survey_figures_say_who_was_asked(self):
        self.assertIn(f'href="{KFF_SURVEY}"', self.section)
        self.assertIn("adults who had a marketplace plan in 2025, taken in February and March 2026, 22% said they had moved to other coverage", self.text)
        self.assertIn("and 9% said they were uninsured.", self.text)
        # KFF's own caveat on those plans, so the note doesn't reassure more than KFF does.
        self.assertIn("KFF notes that some plans sold outside the marketplace may cover less and have fewer consumer protections.", self.text)
        self.assertIn("1,117 adults who had marketplace plans in 2025", self.method)
        self.assertIn("plus or minus 3.8 points", self.method)

    def test_census_figures_say_their_year(self):
        self.assertIn(f"the Census uninsured figures above are for {_census_year()}, before this drop.", self.text)

    def test_causes_stay_off_the_page(self):
        # Why enrollment fell is contested, so the page shows what changed, not why.
        for words in ("tax credit", "subsid", "fraud", "improper", "because", "caused", "blame", "enhanced"):
            self.assertNotIn(words, (self.text + self.method).lower())

    def test_no_insurer_is_named(self):
        names = {_short(i["n"]) for i in DATA["issuers"]} | {r["n"] for r in DATA["prior_auth"]}
        for name in names:
            self.assertNotIn(name, self.text + self.method)

    def test_no_em_dashes_in_the_new_copy(self):
        for part in (self.section, self.method, self.script):
            self.assertNotIn("\u2014", part)

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

    def test_method_note_compares_the_early_count_with_this_file(self):
        self.assertIn(
            f"its early 2025 report had {_millions(EARLY_FEB_2025)} for February 2025 (counted March 15, 2025), "
            f"against {_millions(_february()[2025])} in this file, so mixing reports would make the drop look bigger.",
            self.method,
        )
        for p in MK["picks"]:
            self.assertIn(f'<a href="{p["url"]}">{p["y"]}</a>', self.method_html)

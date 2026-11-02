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
        # Checked outside the embedded JSON, which carries every URL anyway. The marketplace
        # files are named on the open enrollment page, with the numbers drawn from them.
        visible = _without_data(self.content)
        for source in DATA["sources"]:
            if source["url"] not in MARKETPLACE_SOURCES:
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
            f"Rates run from about {_pct(min(rates))} to {_pct(max(rates))}",
            self.content,
        )

    def test_state_range_headline_says_which_states(self):
        # The map covers only states whose insurers report to HealthCare.gov, not every state.
        self.assertIn(f"across {len(DATA['states'])} HealthCare.gov states. Share of in-network claims denied", self.content)
        self.assertNotIn("depending on your state", self.content)

    def test_california_review_rates(self):
        by_year = {r["y"]: r["r"] for r in DATA["imr_trend"]}
        self.assertIn(f"overturned {_pct(by_year[2025])} of the denials", self.content)
        self.assertIn(f"up from {_pct(by_year[2017])} in 2017", self.content)

    def test_appeals_section_leads_with_the_healthcare_gov_figure_before_california(self):
        # So the first chart section doesn't read as if the whole page were about California.
        latest = DATA["trend"][-1]
        lead = _between(self.content, '<p id="appeals-lead">', "</p>").removeprefix('<p id="appeals-lead">')
        national = (
            f"On HealthCare.gov plans, the federal marketplace, insurers reversed {_pct(latest['overturned'] / latest['appeals'])} "
            f"of the internal claim appeals they reported in {latest['year']} ({latest['overturned']:,.0f} of {latest['appeals']:,.0f})."
        )
        self.assertTrue(lead.startswith(national), lead)
        self.assertLess(lead.index(national), lead.index("California"))
        self.assertIn('<h3 id="appeals-title">Independent review in California</h3>', self.content)

    def test_appeal_win_rate_is_not_compared_with_2024(self):
        # The insurers reporting changed between years, so it isn't like for like.
        prev = next(t for t in DATA["trend"] if t["year"] == 2024)
        self.assertIn(
            "In 2025, HealthCare.gov insurers reversed more than a quarter of the internal claim appeals people filed",
            self.content,
        )
        self.assertIn("more than a quarter of appeals win.", self.content)
        self.assertNotIn(_pct(prev["overturned"] / prev["appeals"]), self.content)

    def test_method_note_explains_the_won_counts_left_out(self):
        self.assertIn("Three insurers' 2025 appeals-won counts look like reporting errors", self.content)
        self.assertIn("With them, the 2025 rate would be 26.8%.", self.content)

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
        self.assertEqual(round(1 / latest["in_rate"]), 6)
        self.assertIn(
            "about one in six in-network claims in 2025, "
            f"{_millions(latest['in_denied'])} among reporting insurers",
            self.content,
        )

    def test_method_note_gives_both_counting_bases(self):
        latest = DATA["trend"][-1]
        ratio = latest["in_denied"] / latest["appeals_all"]
        self.assertIn(
            f"{_millions(latest['in_denied'])} denials in 2025, "
            f"{_pct(latest['in_rate'])} of in-network claims, and "
            f"{latest['appeals_all']:,.0f} internal claim appeals, "
            f"about 1 for every {round(ratio, -1):.0f} of those denials",
            self.content,
        )
        self.assertIn(
            f"In 2025 insurers denied {_millions(latest['out_denied'])} of "
            f"{_millions(latest['out_claims'])} out-of-network claims ({_pct(latest['out_rate'])}) and "
            f"{_millions(latest['all_denied'])} of {_millions(latest['all_claims'])} claims overall "
            f"({_pct(latest['all_rate'])}).",
            self.content,
        )

    def test_trend_copy_gives_the_fall_with_its_caveat(self):
        by_year = {t["year"]: t for t in DATA["trend"]}
        new, old = by_year[2025], by_year[2024]
        self.assertIn(
            f"Insurers denied {new['in_rate'] * 100:.1f}% of in-network claims in 2025, down from {old['in_rate'] * 100:.1f}% in 2024.",
            self.content,
        )
        self.assertEqual(round((new["in_claims"] / old["in_claims"] - 1) * 100), 18)
        self.assertLess(abs(new["in_denied"] / old["in_denied"] - 1), 0.02)
        self.assertIn(f"Denials held at about {_millions(new['in_denied'])} while claims rose 18%", self.content)
        self.assertIn("That may be a good sign, but this data can't prove it", self.content)
        self.assertIn('<a href="#method-2025-drop">', self.content)
        self.assertIn('id="method-2025-drop"', self.content)

    def test_trend_copy_uses_the_in_network_series(self):
        # The yearly in-network rates are in the table beside the chart, not read out in the intro.
        latest = DATA["trend"][-1]
        self.assertEqual(round(latest["out_rate"] / latest["in_rate"]), 2)
        self.assertIn(
            f"denied {_pct(latest['out_rate'])} of them in 2025, about twice the in-network rate",
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


class TestDenialWatchWhatChanged(TestCase):
    """The 2024 to 2025 section's sentences name numbers from the bundle's changes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()
        i = cls.content.index('id="what-changed"')
        cls.section = cls.content[i : cls.content.index('id="coverage"', i)]
        cls.changes = DATA["changes"]
        cls.companies = {r["k"]: r for r in cls.changes["companies"]}

    def test_rates_are_on_the_same_insurers_in_both_years(self):
        everyone = self.companies["All these insurers"]
        self.assertIn(f"Among the {everyone['insurers']} insurers with figures in both years", self.section)
        self.assertIn(f"fell from {everyone['r0'] * 100:.1f}% to {everyone['r1'] * 100:.1f}%", self.section)
        self.assertEqual(everyone["fell"] + everyone["rose"], everyone["insurers"])
        for field in ("insurers", "c0", "d0", "c1", "d1"):
            self.assertEqual(
                everyone[field],
                sum(r[field] for k, r in self.companies.items() if k != "All these insurers"),
            )

    def test_only_the_three_named_companies_fell(self):
        for name in ("Centene", "Oscar", "Florida Blue"):
            self.assertLess(self.companies[name]["r1"], self.companies[name]["r0"], name)
            self.assertEqual(self.companies[name]["rose"], 0, name)
        rest = [self.companies["UnitedHealthcare"], self.companies["All other insurers"]]
        old = sum(r["d0"] for r in rest) / sum(r["c0"] for r in rest)
        new = sum(r["d1"] for r in rest) / sum(r["c1"] for r in rest)
        self.assertIn(f"the other insurers' rate rose from {old * 100:.1f}% to {new * 100:.1f}%", self.section)
        self.assertLess(old, new)

    def test_the_cautions_on_the_falls_match_the_data(self):
        centene, oscar, florida = (self.companies[k] for k in ("Centene", "Oscar", "Florida Blue"))
        self.assertIn(
            f"Claims grew {round((centene['c1'] / centene['c0'] - 1) * 100)}% at Centene and {round((oscar['c1'] / oscar['c0'] - 1) * 100)}% at Oscar.",
            self.section,
        )
        self.assertIn(f"total denials fell from {centene['d0'] / 1e6:.1f} to {centene['d1'] / 1e6:.1f} million", self.section)
        self.assertIn(f"rose from {centene['pd0'] / 1e6:.1f} to {centene['pd1'] / 1e6:.1f} million", self.section)
        self.assertIn(f"down from about {round(florida['pd0'] / florida['d0'] * 100)}% in 2024", self.section)
        self.assertAlmostEqual(florida["pd1"] / florida["d1"], 1 / 3, delta=0.02)
        self.assertIn("We can't tell a real change from a change in how a company counts.", self.section)

    def test_states_sentence(self):
        states = self.changes["states"]
        self.assertEqual(states["states"], len(DATA["states"]))
        self.assertEqual(states["fell"] + len(states["rose"]) + len(states["flat"]), states["states"])
        self.assertEqual(states["rose"], ["WI", "WY", "MT", "DE", "MI", "LA"])
        self.assertEqual(states["flat"], ["WV"])
        self.assertIn(f"rates fell in {states['fell']} of the {states['states']} HealthCare.gov states, held in West Virginia, and rose in six", self.section)

    def test_reasons_sentences(self):
        rows = {r["k"]: r for r in self.changes["reasons"]["rows"]}
        paper = rows["Administrative (missing information, duplicate, late filing, billing)"]
        self.assertIn(f"paperwork denials fell from {paper['per0']:.2f} to {paper['per1']:.2f}", self.section)
        need = rows['"Not medically necessary" (physical health)']
        self.assertIn(f"rose from {need['per0']:.2f} to {need['per1']:.2f}", self.section)
        auth = rows["Prior authorization or referral required"]
        self.assertIn(f"from {auth['per0']:.2f} to {auth['per1']:.2f}", self.section)

    def test_florida_blue_is_more_than_the_whole_fall_in_paperwork(self):
        florida = self.changes["reasons"]["florida_blue"]
        row = next(r for r in self.changes["reasons"]["rows"] if r["k"].startswith("Administrative"))
        fall = (row["n0"] - row["n1"]) / 1e6
        self.assertGreater((florida["paperwork0"] - florida["paperwork1"]) / 1e6, fall)
        self.assertIn(f"fell from {florida['paperwork0'] / 1e6:.1f} to {florida['paperwork1'] / 1e6:.1f} million, more than the {fall:.1f} million fall", self.section)
        self.assertIn(f"barely moved, from {florida['without0']:.2f} to {florida['without1']:.2f}.", self.section)
        self.assertLess(abs(florida["without0"] - florida["without1"]), 0.05)

    def test_appeals_sentences(self):
        appeals = self.changes["appeals"]
        same, rest = appeals["same"], appeals["without_largest"]
        self.assertIn(
            f"Among the {same['insurers']} insurers reporting appeals in both years, they reversed {same['r0'] * 100:.1f}% of claim appeals in 2024 and {same['r1'] * 100:.1f}% in 2025.",
            self.section,
        )
        self.assertIn(f"it went from {rest['r0'] * 100:.1f}% to {rest['r1'] * 100:.1f}%", self.section)
        self.assertEqual(sum(n.startswith("Oscar") for n in appeals["largest"]), 3)
        self.assertIn("five large appeal counts, three of them Oscar's", self.section)
        self.assertIn(f"Insurers reversed {appeals['pre_rate'] * 100:.1f}% of those.", self.section)

    def test_review_sentences(self):
        rows = {r["k"]: r for r in DATA["imr_periods"]["rows"]}
        cancer, surgery = rows["Cancer care"], rows["Reconstructive surgery"]
        self.assertIn(f"Cancer care rose from {_pct(cancer['r0'])} in 2021 to 2023 to {_pct(cancer['r1'])}", self.section)
        self.assertIn(f"Reconstructive surgery fell from {_pct(surgery['r0'])} to {_pct(surgery['r1'])}", self.section)

    def test_method_describes_the_groups_and_the_counts(self):
        everyone = self.companies["All these insurers"]
        i = self.content.index('id="method-what-changed"')
        method = self.content[i : self.content.index('id="method-coverage"', i)]
        self.assertIn(f"{everyone['pd1'] / 1e6:.1f} million against {everyone['d1'] / 1e6:.1f} million", method)
        self.assertIn("QCA Health Plan and QualChoice in Arkansas", method)
        for name in self.changes["appeals"]["largest"][:3]:
            self.assertIn(name.split(" (")[0].replace(", Inc.", ""), method)


class TestDenialWatchCompanyGroups(TestCase):
    """Which company an insurer's name belongs to."""

    def setUp(self):
        self.builder = _load_builder()

    def test_names_sort_into_companies(self):
        for name, company in {
            "Celtic Insurance Company": "Centene",
            "QCA Health Plan, Inc.": "Centene",
            "Health Options, Inc.": "Florida Blue",
            "Florida Health Care Plan, Inc.": "Florida Blue",
            "Oscar Buckeye State Insurance Corp.": "Oscar",
            "UnitedHealthcare of Texas, Inc.": "UnitedHealthcare",
            "Blue Cross Blue Shield of Texas": self.builder.OTHER_INSURERS,
        }.items():
            self.assertEqual(self.builder.company_of(name), company, name)

    def test_an_unlisted_centene_insurer_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self.builder.company_of("Ambetter of Somewhere Inc.")

    def test_the_prior_rate_in_the_insurer_table_needs_the_table_size_in_2024(self):
        rows = [r for r in DATA["issuers"] if r["p"] is not None]
        self.assertGreater(len(rows), 100)
        self.assertTrue(all(0 < r["p"] < 1 for r in rows))
        self.assertTrue(any(r["p"] is None for r in DATA["issuers"]))

    def test_appeals_tally_needs_both_counts_in_both_years(self):
        def insurer(name, filed, won):
            return dict(issuer=name, state="TX", ap_filed=filed, ap_over=won)

        pairs = [
            (insurer("Oscar Insurance Company", 100, 50), insurer("Oscar Insurance Company", 300, 30)),
            (insurer("B", 100, None), insurer("B", 100, 50)),
            (insurer("C", 0, 0), insurer("C", 100, 50)),
        ]
        with mock.patch.object(self.builder, "APPEAL_BOOKS_LEFT_OUT", 0):
            same = self.builder.appeal_changes(pairs)["same"]
        self.assertEqual((same["insurers"], same["r0"], same["r1"]), (1, 0.5, 0.1))


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
        self.assertIn(f'<a href="{href}" id="denialwatchlink">Denial Watch</a>', about)
        self.assertIn(f'<a href="{href}">Denial Watch</a>', about)
        self.assertRegex(home, rf'(?s)id="denial-watch-home".*?href="{href}"')
        self.assertIn(f'<a href="{href}">See how often, on Denial Watch</a>', explain)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_home_card_states_the_latest_number(self):
        latest = DATA["trend"][-1]
        html = Client().get(reverse("root")).content.decode()
        self.assertIn(
            f"About 1 in {round(1 / latest['in_rate'])} denied in-network claims on "
            f"HealthCare.gov plans, by insurers in {latest['year']}",
            _text(_between(html, 'id="denial-watch-home"', "</a>")),
        )

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_home_hero_announces_it_with_the_counters_pace(self):
        latest = DATA["trend"][-1]
        per_day = round(latest["in_denied"] / latest["days"])
        # 9am UTC on October 15 is still October 14 in Hawaii, the last day before open enrollment takes the pill.
        last = datetime(2026, 10, 15, 9, 0, tzinfo=dt_timezone.utc)
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
    def test_the_home_hero_hands_off_to_the_card_after_november_2(self):
        # After open enrollment too, so no season pill stands in for it.
        after = datetime(2027, 1, 16, 11, 0, tzinfo=dt_timezone.utc)
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
        self.assertIn('id="denialwatchmenu">Denial Watch <span class="fhi-nav-new">New</span></summary>', html)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_nav_drops_new_after_december_15(self):
        after = datetime(2026, 12, 16, 11, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=after):
            html = Client().get(reverse("about")).content.decode()
        self.assertIn('id="denialwatchmenu">Denial Watch</summary>', html)
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
        self.assertIn("$('istate').value=rowsOf(dsel)?dsel:states[0];", self.js)
        self.assertIn("$('dmap')._onPin=s=>{ipin=s;$('istate').value=rowsOf(s)?s:'';iDraw();};", self.js)
        # A state with no insurers in the table says so, instead of leaving the last state's rows.
        self.assertIn("isn't in this data: it runs its own marketplace, so its insurers aren't in the HealthCare.gov figures this page uses.", self.js)
        self.assertIn("if(svg._onPin)svg._onPin(g.dataset.s);", self.js)
        self.assertNotIn("highest and lowest 10", self.js)
        self.assertNotIn("slice(0,10)", self.js)

    def test_an_empty_insurer_table_calls_only_grey_states_grey(self):
        # A state on the map with no insurer over the table's floor gets its own line, not the grey-state one.
        self.assertIn(
            "${NAMES[ipin]?(ST[ipin]?`None of ${NAMES[ipin]}'s insurers reported the 50,000 in-network claims this table needs.`:OWN(ipin)+elsewhere(ipin)):''}",
            self.js,
        )

    def test_a_grey_state_points_to_where_its_data_is(self):
        # California has no HealthCare.gov figures, so its readout links to the page's independent review results.
        self.assertIn(
            '"", "#appeals", "See how California\'s independent reviews turned out", ""],',
            self.js,
        )
        self.assertIn("if(!r)h+=(earlier(s)||line1(OWN(s)))+elsewhere(s);", self.js)
        template = (
            REPO / "fighthealthinsurance" / "templates" / "denial_watch.html"
        ).read_text()
        self.assertIn('id="appeals"', template)

    def test_california_line_gives_its_insurers_own_reports_and_keeps_the_reviews_link(self):
        # FHI's sum over the 9 insurers with usable 2025 reports on Covered California's page; not CMS data.
        line = self.js[self.js.index("  CA:[") : self.js.index("\n", self.js.index("  CA:["))]
        self.assertIn("reported denying 15.7% of in-network claims", line)
        self.assertIn("(6.2 million of 39.7 million; FHI's calculation from those reports). Health Net and Aetna are missing for 2025", line)
        self.assertIn('"https://www.hbex.ca.gov/insurance-companies/denied-claims/", "Covered California"', line)
        self.assertIn('"#appeals"', line)
        self.assertIn("for(let i=0;i<e.length;i+=4)", self.js)

    def test_pennsylvania_line_names_its_insurers_and_publisher(self):
        # FHI's sum over the 11 insurers in the state's report, for 2024 claims; not CMS data.
        self.assertIn(
            '" denied 14.2% of in-network claims (FHI\'s calculation from the Pennsylvania Insurance Department\'s data)."',
            self.js,
        )
        self.assertIn('PA:["In 2024, the 11 insurers in ", ', self.js)

    def test_method_note_says_which_grey_states_have_no_source_to_link(self):
        # The JS gives Rhode Island and Kentucky no link, so the note can't promise one for every grey state.
        self.assertIn("Rhode Island and Kentucky publish nothing current, so theirs says so with no link.", _page())
        for state in ("RI", "KY"):
            line = self.js[self.js.index(f"  {state}:[") : self.js.index("\n", self.js.index(f"  {state}:["))]
            self.assertIn('"", "", ""]', line)

    def test_every_grey_state_has_one_checked_source_line(self):
        # A state the map leaves grey gets exactly one entry, and nothing the map fills does.
        bundle = json.loads(
            (REPO / "fighthealthinsurance" / "static" / "denial_watch.json").read_text()
        )
        grey = set(bundle["places"]) - set(bundle["states"])
        block = self.js[
            self.js.index("const ELSEWHERE={") : self.js.index("const elsewhere=")
        ]
        entries = re.findall(r"^  ([A-Z]{2}):(\[.*\]),?$", block, re.M)
        self.assertEqual(sorted(s for s, _ in entries), sorted(grey))
        for state, raw in entries:
            items = json.loads(raw)
            self.assertEqual(len(items) % 4, 0, state)
            for i in range(0, len(items), 4):
                before, link, text, after = items[i : i + 4]
                self.assertTrue(
                    link == "" or link == "#appeals" or link.startswith("https://"),
                    state,
                )
                self.assertEqual(bool(link), bool(text), state)
                for part in (before, text, after):
                    self.assertNotIn("\u2014", part)
                    self.assertNotIn("\u2013", part)

    def test_map_tiles_show_each_states_insurer_range_in_network_only(self):
        # The heatmap carries what the ranked bars did; the range is of in-network insurer rates.
        self.assertEqual(self.js.count("sub:spanLab"), 1)
        self.assertIn("in:{v:s=>ST[s]?ST[s].rate:null,b:RB,c:DENY,l:x=>pct(x,0),t:x=>pct(x,1),sub:spanLab,", self.js)
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
        self.assertIn("HealthCare.gov claims denied today, at 2025's pace", page)
        self.assertIn("denied per day in 2025, on average", page)

    def test_counter_can_be_paused(self):
        self.assertIn("prefers-reduced-motion", self.js)
        self.assertIn('id="tickpause"', Client().get(reverse("denial-watch")).content.decode())


class TestDenialWatchEarlierYears(TestCase):
    """Grey states that used HealthCare.gov before: CMS's last year for each, shown only when picked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()
        cls.js = (
            REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js"
        ).read_text()

    def _row(self, state, rec_in, den_in):
        """An insurer as read_puf returns it, for the in-network rules."""
        return dict(state=state, rec_in=rec_in, den_in=den_in, ap_filed=None, ap_over=None)

    def test_earlier_figures_are_only_for_grey_states(self):
        grey = set(DATA["places"]) - set(DATA["states"])
        self.assertTrue(DATA["earlier"])
        self.assertLessEqual(set(DATA["earlier"]), grey)

    def test_each_earlier_year_is_before_the_map_year_and_matches_its_file(self):
        latest = DATA["trend"][-1]["year"]
        for state, e in DATA["earlier"].items():
            self.assertLess(e["year"], latest, state)
            self.assertEqual(self.b.CLAIMS_YEAR[e["py"]], e["year"], state)

    def test_earlier_rates_are_proportions_of_their_counts(self):
        for state, e in DATA["earlier"].items():
            self.assertTrue(0 < e["rate"] < 1, state)
            self.assertAlmostEqual(e["rate"], e["denied"] / e["claims"], places=12, msg=state)
            self.assertGreaterEqual(e["issuers"], 1, state)
            if e["span"] is not None:
                lo, hi = e["span"]
                self.assertTrue(0 <= lo <= hi <= 1, state)

    def test_builder_takes_the_last_year_a_state_is_in(self):
        per_year = {
            2023: ([self._row("XX", 1000.0, 300.0)], {}, 0.0),
            2025: ([self._row("XX", 1000.0, 100.0)], {}, 0.0),
            2026: ([self._row("YY", 1000.0, 200.0)], {}, 0.0),
        }
        e = self.b.earlier_states(per_year, {"YY"})["XX"]
        self.assertEqual((e["year"], e["py"], e["rate"]), (2023, 2025, 0.1))

    def test_builder_leaves_out_states_on_the_latest_map(self):
        per_year = {
            2025: ([self._row("YY", 1000.0, 100.0)], {}, 0.0),
            2026: ([self._row("YY", 1000.0, 200.0)], {}, 0.0),
        }
        self.assertEqual(self.b.earlier_states(per_year, {"YY"}), {})

    def test_builder_rate_leaves_out_suppressed_cells(self):
        rows = [self._row("XX", 1000.0, 100.0), self._row("XX", 1000.0, None)]
        e = self.b.earlier_states({2025: (rows, {}, 0.0)}, set())["XX"]
        self.assertEqual((e["rate"], e["issuers"]), (0.1, 1))

    def test_builder_range_covers_the_insurers_the_table_would_list(self):
        rows = [
            self._row("XX", 60_000.0, 6_000.0),
            self._row("XX", 80_000.0, 24_000.0),
            self._row("XX", 40_000.0, 36_000.0),  # under the table's 50,000 claims
        ]
        e = self.b.earlier_states({2025: (rows, {}, 0.0)}, set())["XX"]
        self.assertEqual(e["span"], [0.1, 0.3])
        self.assertEqual(e["issuers"], 3)

    def test_builder_range_needs_two_table_insurers(self):
        rows = [self._row("XX", 60_000.0, 6_000.0), self._row("XX", 40_000.0, 36_000.0)]
        self.assertIsNone(self.b.earlier_states({2025: (rows, {}, 0.0)}, set())["XX"]["span"])

    def test_grey_state_readout_reads_the_earlier_figures(self):
        self.assertIn("const EARLIER=D.earlier||{};", self.js)
        self.assertIn("if(!r)h+=(earlier(s)||line1(OWN(s)))+elsewhere(s);", self.js)
        self.assertIn("When ${esc(NAMES[s])} still used HealthCare.gov, its ", self.js)
        self.assertIn("of in-network claims in ${e.year} (FHI's calculation from CMS's data).", self.js)
        # The copy's threshold is the builder's.
        self.assertEqual(self.b.MIN_ISSUER_CLAIMS, 50_000)
        self.assertIn("Among those with at least 50,000 such claims, rates ran from", self.js)

    def test_earlier_figures_stay_off_the_map_and_national_figures(self):
        # Read in one place, the grey-state line: never a map view, range, trend or table.
        self.assertEqual(self.js.count("EARLIER"), 2)
        self.assertEqual(self.js.count("earlier(s)"), 1)
        # A state with an earlier figure says it's off the map now, not that it was never in HealthCare.gov's data.
        self.assertIn("isn't on the ${T.year} map: it now runs its own marketplace.", self.js)
        self.assertFalse(set(DATA["earlier"]) & set(DATA["states"]))

    def test_method_note_says_where_the_earlier_figures_come_from(self):
        note = _text(_between(_page(), 'id="method-who-denies"', "</div>"))
        names = sorted((self.b.STATE_NAMES[s], e["year"]) for s, e in DATA["earlier"].items())
        picks = ", ".join(n for n, _ in names[:-1]) + " or " + names[-1][0]
        years = ", ".join(f"{y} for {n}" for n, y in names).replace(" for ", " claims for ", 1)
        self.assertIn(
            f"Picking {picks} also shows its in-network denial rate from the latest CMS file that includes its insurers ({years}), "
            f"FHI's calculation from CMS's data on the same measure as the map. Those are older years, so don't "
            f"rank them against the {DATA['trend'][-1]['year']} states.",
            note,
        )


STATE_REPORT_HEADER = "state,claims_year,insurers,in_network_claims,in_network_denied,publisher,source_url,accessed,note\n"


def _every_bars(html: str) -> list[dict]:
    """Each bar of the every-state chart, in page order, with the list it sits in ("map" or "other")."""
    return [
        bar
        for where, start, end in (
            ("map", 'id="every-bars"', 'id="every-other-title"'),
            ("other", 'id="every-other-bars"', 'id="every-alt"'),
        )
        for bar in _bars_in(_between(html, start, end), where)
    ]


def _bars_in(chart: str, where: str) -> list[dict]:
    return [
        dict(where=where, group=g, code=s, name=_text(n), tag=_text(t or ""), width=float(w), value=v)
        for g, s, n, t, w, v in re.findall(
            r'<div class="dw-bar dw-src-(\w+)" data-s="(\w+)"><div class="dw-bar-name"><span>([^<]*)'
            r'(?: <span class="dw-every-tag">([^<]*)</span>)?</span></div>.*?width:([\d.]+)%.*?'
            r'<div class="dw-bar-value dw-num">([^<]*)</div>',
            chart,
            re.S,
        )
    ]


class TestDenialWatchEveryStateChart(TestCase):
    """One chart with every state that has an in-network rate: CMS's 2024 states, two states' own reports and CMS's last year for three more."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = _page()
        cls.section = _between(cls.page, 'id="every-state"', 'id="insurers"')
        cls.bars = _every_bars(cls.page)
        cls.js = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()
        cls.year = DATA["trend"][-1]["year"]
        cls.expected = {
            **{s: ("cms", v["rate"], cls.year) for s, v in DATA["states"].items()},
            **{s: ("state", v["rate"], v["year"]) for s, v in DATA["state_reports"].items()},
            **{s: ("earlier", v["rate"], v["year"]) for s, v in DATA["earlier"].items()},
        }

    def test_every_bar_value_equals_the_bundle(self):
        self.assertEqual(sorted(b["code"] for b in self.bars), sorted(self.expected))
        top = max(rate for _, rate, _ in self.expected.values())
        for b in self.bars:
            group, rate, _ = self.expected[b["code"]]
            self.assertEqual(b["group"], group, b["code"])
            self.assertEqual(b["value"], f"{rate * 100:.1f}%", b["code"])
            self.assertAlmostEqual(b["width"], rate / top * 100, delta=0.05, msg=b["code"])

    def test_map_states_run_highest_first_and_the_other_five_sit_apart(self):
        # The other five aren't ranked against the map's year: they follow in their own list, by kind then name.
        where = [b["where"] for b in self.bars]
        self.assertEqual(where, sorted(where))
        self.assertEqual({b["code"] for b in self.bars if b["where"] == "map"}, set(DATA["states"]))
        self.assertTrue(all(b["group"] != "cms" for b in self.bars if b["where"] == "other"))
        rates = [self.expected[b["code"]][1] for b in self.bars if b["where"] == "map"]
        self.assertEqual(rates, sorted(rates, reverse=True))
        others = [(b["group"], b["name"]) for b in self.bars if b["where"] == "other"]
        self.assertEqual(others, sorted(others, key=lambda o: (o[0] != "state", o[1])))

    def test_each_bar_off_the_map_names_its_source_and_year(self):
        for b in self.bars:
            group, _, year = self.expected[b["code"]]
            if group == "cms":
                self.assertEqual(b["tag"], "", b["code"])
            elif group == "state":
                self.assertEqual(b["tag"], f"{DATA['state_reports'][b['code']]['publisher']}, {year} claims")
            else:
                self.assertLess(year, self.year, b["code"])
                self.assertEqual(b["tag"], f"CMS, {year} claims")

    def test_key_names_each_group_in_words_with_publisher_and_year(self):
        key = [_text(li) for li in re.findall(r"<li><i class=\"dw-src-\w+\"></i>(.*?)</li>", self.section)]
        self.assertEqual(re.findall(r'<li><i class="dw-src-(\w+)"></i>', self.section), ["cms", "state", "earlier"])
        # Each key sits with its own list: the map's above it, the other two under the subheading.
        self.assertEqual(re.findall(r'<li><i class="dw-src-(\w+)"></i>', _between(self.section, 'id="every-key"', 'id="every-bars"')), ["cms"])
        self.assertEqual(
            re.findall(r'<li><i class="dw-src-(\w+)"></i>', _between(self.section, 'id="every-other-key"', 'id="every-other-bars"')),
            ["state", "earlier"],
        )
        self.assertEqual(key[0], f"HealthCare.gov insurers, {self.year} claims (FHI's calculation from CMS's data)")
        years = {v["year"] for v in DATA["state_reports"].values()}
        self.assertEqual(years, {self.year, self.year - 1})
        self.assertIn("California (2025 claims) and Pennsylvania (2024 claims) insurers' own reports on the same federal form", key[1])
        publishers = [v["publisher"] for v in DATA["state_reports"].values()]
        self.assertIn(
            "(FHI's calculation from " + "'s and the ".join(publishers) + "'s data)",
            key[1],
        )
        self.assertEqual(
            key[2],
            "HealthCare.gov insurers in the last year CMS's files include the state, given on the bar (FHI's calculation from CMS's data)",
        )

    def test_intro_and_caveat_name_the_states_in_the_bundle(self):
        text = _text(self.section)
        names = _load_builder().STATE_NAMES
        self.assertEqual({s: r["year"] for s, r in DATA["state_reports"].items()}, {"CA": 2025, "PA": 2024})
        older = sorted(names[s] for s in DATA["earlier"])
        older = ", ".join(older[:-1]) + " and " + older[-1]
        self.assertIn(
            f"Share of in-network claims denied, by state: the {len(DATA['states'])} HealthCare.gov states from the map, highest first. Below them, on the same scale but listed apart, are six grey states we found a figure for elsewhere: "
            f"California (2025 claims) and Pennsylvania (2024 claims) from their insurers' own reports on the same federal form, and {older} from the last year CMS's files include them.",
            text,
        )
        self.assertIn(
            "These sources and years aren't strictly comparable: California's and Pennsylvania's reports can count plans sold outside their marketplaces, "
            "which CMS's figures leave out. California's 2025 figure is missing Health Net and Aetna, whose 2025 reports we couldn't read or find. "
            f"Pennsylvania, {older} are from earlier years. So don't rank those six against the {self.year} states.",
            text,
        )

    def test_chart_sits_after_the_map_with_its_caveat_and_table(self):
        order = [
            self.page.index(f'id="{i}"')
            for i in ("map-denials", "every-state", "every-measure", "every-bars", "every-other-title", "every-caveat", "every-other-key", "every-other-bars", "every-alt", "insurers")
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn('<h3 id="every-state-title">Every state\'s denial rate</h3>', self.section)
        self.assertIn("<p>Share of in-network claims denied, by state: the 29 HealthCare.gov states from the map, highest first.", self.section)
        self.assertIn("Each bar is the % of in-network claims insurers denied in that state.", _text(self.section))
        five = {6: "Six"}[len(DATA["state_reports"]) + len(DATA["earlier"])]
        self.assertIn(f'<h4 class="dw-every-sub" id="every-other-title">{five} grey states, from other sources or years</h4>', self.section)
        self.assertIn("<summary>Show these figures as a table</summary>", self.section)

    def test_table_gives_the_same_values_as_the_bars(self):
        table = _between(self.section, 'id="every-table"', "</table>")
        # Every rate is FHI's calculation, so the visible column says so, not just the caption.
        self.assertIn('<th scope="col">FHI\'s calculation from</th>', table)
        self.assertNotIn(">Source<", table)
        map_rows, other_rows = table.split('<tbody id="every-other-rows">')
        five = {6: "Six"}[len(DATA["state_reports"]) + len(DATA["earlier"])]
        self.assertIn(f'<th scope="colgroup" colspan="3" class="dw-every-sub">{five} grey states, from other sources or years</th>', other_rows)
        self.assertEqual(set(re.findall(r'<tr data-s="(\w+)">', map_rows)), set(DATA["states"]))
        rows = re.findall(
            r'<tr data-s="(\w+)"><th scope="row">([^<]*)</th><td class="dw-r dw-num">([^<]*)</td><td>([^<]*)</td></tr>',
            table,
        )
        self.assertEqual(
            [(s, n, v) for s, n, v, _ in rows],
            [(b["code"], b["name"], b["value"]) for b in self.bars],
        )
        for s, _, _, source in rows:
            group, _, year = self.expected[s]
            publisher = DATA["state_reports"][s]["publisher"] if group == "state" else "CMS"
            self.assertEqual(source, f"{publisher}, {year} claims", s)

    def test_chart_names_are_state_names(self):
        names = _load_builder().STATE_NAMES
        self.assertEqual({b["code"]: b["name"] for b in self.bars}, {s: names[s] for s in self.expected})

    def test_grey_state_figures_stay_off_the_map_national_figures_and_ranges(self):
        grey = set(DATA["places"]) - set(DATA["states"])
        self.assertLessEqual(set(DATA["state_reports"]), grey)
        self.assertFalse(set(DATA["state_reports"]) & set(DATA["earlier"]))
        # The script never reads them: no map view, range, trend or table.
        self.assertNotIn("state_reports", self.js)
        # The chart's 2024 bars are the map's states, and nothing else carries CMS's year.
        self.assertEqual({b["code"] for b in self.bars if b["group"] == "cms"}, set(DATA["states"]))
        # The map's headline range is still the HealthCare.gov states' alone.
        rates = [s["rate"] for s in DATA["states"].values()]
        self.assertIn(f"Rates run from about {_pct(min(rates))} to {_pct(max(rates))} across", self.page)

    def test_method_note_says_what_the_chart_combines(self):
        note = _between(self.page, 'id="method-every-state"', "</li>")
        ca, pa = DATA["state_reports"]["CA"], DATA["state_reports"]["PA"]
        self.assertIn(
            f"both FHI's calculations: from the {ca['year']} claims that {ca['insurers']} insurers in California (the ones with usable reports) "
            f"and the {pa['year']} claims that {pa['insurers']} in Pennsylvania reported on the same federal form",
            _text(note),
        )
        names = _load_builder().STATE_NAMES
        older = sorted(names[s] for s in DATA["earlier"])
        self.assertIn(f"from the last CMS files that include {', '.join(older[:-1])} and {older[-1]}, described above.", _text(note))
        self.assertIn(f'<a href="{ca["url"]}">{ca["publisher"]}</a>', note)
        self.assertIn(f'the <a href="{pa["url"]}">{pa["publisher"]}</a>', note)

    def test_california_and_pennsylvania_readout_lines_match_the_bundle(self):
        # Hand-written in the script, so tie each number and link to the bundle.
        def entry(state):
            i = self.js.index(f"  {state}:[")
            return json.loads(self.js[i + 5 : self.js.index("\n", i)].rstrip(","))

        ca, pa = DATA["state_reports"]["CA"], DATA["state_reports"]["PA"]
        e = entry("CA")
        self.assertIn(f"The {ca['insurers']} with usable reports for {ca['year']} claims reported denying {ca['rate'] * 100:.1f}% of in-network claims", e[0])
        self.assertEqual(e[1:3], [ca["url"], ca["publisher"]])
        self.assertTrue(e[3].startswith(f" ({_millions(ca['denied'])} of {_millions(ca['claims'])}; FHI's calculation from those reports)."), e[3])
        e = entry("PA")
        self.assertEqual(e[0], f"In {pa['year']}, the {pa['insurers']} insurers in ")
        self.assertEqual(e[1], pa["url"])
        self.assertEqual(
            e[3],
            f" denied {pa['rate'] * 100:.1f}% of in-network claims (FHI's calculation from the {pa['publisher']}'s data).",
        )


class TestDenialWatchStateReportsBuilder(TestCase):
    """state_reports.csv: one checked row per grey state, and the build stops on anything it can't show."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    def _load(self, *rows, shown=(), earlier=()):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(STATE_REPORT_HEADER + "".join(r + "\n" for r in rows))
        try:
            return self.b.load_state_reports(set(shown), dict.fromkeys(earlier), f.name)
        finally:
            pathlib.Path(f.name).unlink()

    def _row(self, state="CA", year="2024", claims="1000", denied="150", url="https://example.org/r.pdf"):
        return f"{state},{year},3,{claims},{denied},A State Office,{url},2026-10-25,checked"

    def test_builder_reads_the_curated_file(self):
        self.assertEqual(self.b.load_state_reports(set(DATA["states"]), DATA["earlier"]), DATA["state_reports"])

    def test_every_row_cites_an_https_source_and_says_what_was_summed(self):
        with open(REPO / "scripts" / "denial_watch" / "state_reports.csv", newline="") as f:
            for r in csv.DictReader(f):
                self.assertTrue(r["source_url"].startswith("https://"), r["state"])
                self.assertTrue(r["note"].strip(), r["state"])
                self.assertNotIn("\u2014", r["note"])
                self.assertNotIn("\u2013", r["note"])

    def test_rate_is_denials_over_claims(self):
        e = self._load(self._row())["CA"]
        self.assertEqual((e["rate"], e["claims"], e["denied"], e["insurers"], e["year"]), (0.15, 1000, 150, 3, 2024))

    def test_a_state_on_the_map_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(), shown={"CA"})

    def test_a_state_with_an_earlier_cms_figure_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(), earlier={"CA"})

    def test_another_claims_year_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(year="2023"))

    def test_more_denials_than_claims_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(claims="100", denied="101"))

    def test_a_source_that_isnt_https_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(url="http://example.org/r.pdf"))

    def test_two_rows_for_one_state_stop_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(), self._row())


STATE_REPORT_INSURERS_CSV = REPO / "scripts" / "denial_watch" / "state_report_insurers.csv"
INSURER_HEADER = (
    "state,claims_year,insurer,in_network_claims,in_network_denied,internal_appeals_filed,"
    "internal_appeals_overturned,left_out,source_url,page,accessed,note\n"
)

# Each Georgia, Illinois and Virginia insurer in the last CMS file that includes the state: the file, its Ind QHP
# sheet row, and the in-network claims, denials, internal appeals filed and overturned read there (None where CMS
# printed *, ** or N/A). Copied by hand from the files, so the table can't drift from them.
EARLIER_PUF_CELLS = [
    ("tic2026.xlsx", 10091, "OR", "PacificSource Health Plans", 3771, 658, 136, 12),
    ("tic2026.xlsx", 39424, "OR", "Moda Health Plan, Inc.", 1102738, 157741, 173, 67),
    ("tic2026.xlsx", 56707, "OR", "Providence Health Plan", 410010, 16582, 301, 217),
    ("tic2026.xlsx", 63474, "OR", "BridgeSpan Health Company", 12346, 1294, None, None),
    ("tic2026.xlsx", 71287, "OR", "Kaiser Foundation Healthplan of the NW", 9923, 725, 39, 17),
    # DOUBTFUL_WON: the same counts as the 2025 file, so no win count.
    ("tic2026.xlsx", 77969, "OR", "Regence BlueCross BlueShield of Oregon", 1013376, 100498, 616, None),
    ("tic2024.xlsx", 365, "GA", "Cigna HealthCare of Georgia, Inc.", 819128, 144773, None, None),
    ("tic2024.xlsx", 2585, "GA", "UnitedHealthcare of Georgia, Inc.", 297827, 94241, 427, 202),
    ("tic2024.xlsx", 2607, "GA", "AMGP Georgia Managed Care Company, Inc. dba Anthem Blue Cross and Blue Shield", None, None, None, None),
    ("tic2024.xlsx", 2746, "GA", "Anthem Blue Cross and Blue Shield", 3813700, 904058, 569, 95),
    ("tic2024.xlsx", 3201, "GA", "Oscar Health Plan of Georgia", 1956528, 244361, 1071, 439),
    ("tic2024.xlsx", 3331, "GA", "CareSource Georgia Co.", 1661639, 361615, 29, 14),
    ("tic2024.xlsx", 3761, "GA", "Ambetter of Peach State Inc.", 17619920, 2410742, 1465, 785),
    ("tic2024.xlsx", 4261, "GA", "Aetna Health Inc. (a GA corp.)", 189293, 37611, 51, 45),
    ("tic2024.xlsx", 4271, "GA", "Alliant Health Plans", 455071, 36987, 2772, 310),
    ("tic2024.xlsx", 4598, "GA", "Kaiser Foundation Health Plan of Georgia", 7974, 822, 60, 26),
    ("tic2025.xlsx", 123, "IL", "Oscar Health Plan, Inc.", 49013, 10211, 722, 238),
    ("tic2025.xlsx", 570, "IL", "Health Alliance Medical Plans, Inc.", 1507897, 187861, 221, 88),
    ("tic2025.xlsx", 823, "IL", "Celtic Insurance Company", 1681947, 230759, 314, 140),
    ("tic2025.xlsx", 1197, "IL", "Molina Healthcare of Illinois, Inc.", 91489, 31949, 34, 13),
    ("tic2025.xlsx", 1845, "IL", "Blue Cross Blue Shield of Illinois", 10556396, 2169193, 2232, 694),
    ("tic2025.xlsx", 2406, "IL", "UnitedHealthcare of Illinois, Inc.", 486710, 181665, 691, 271),
    ("tic2025.xlsx", 2724, "IL", "Cigna HealthCare of Illinois, Inc.", 347240, 90407, 53, 24),
    ("tic2025.xlsx", 2899, "IL", "MercyCare HMO, Inc.", 67263, 7524, None, None),
    ("tic2025.xlsx", 3545, "IL", "Aetna Life Insurance Company", None, None, None, None),
    ("tic2025.xlsx", 3967, "IL", "Quartz Health Benefit Plans Corporation", 71597, 4921, 18, None),
    ("tic2025.xlsx", 4492, "IL", "Aetna Health Inc. (a PA corp.)", 167244, 33764, 53, 26),
    ("tic2023.xlsx", 6035, "VA", "Optima Health Plan", 872033, 250857, 165, 96),
    ("tic2023.xlsx", 6044, "VA", "Piedmont Community HealthCare HMO, Inc.", 104805, 18799, 79, 32),
    ("tic2023.xlsx", 6049, "VA", "Aetna Life Insurance Company", None, None, None, None),
    ("tic2023.xlsx", 6057, "VA", "Cigna Health and Life Insurance Company", 2456029, 323020, 147, 56),
    ("tic2023.xlsx", 6078, "VA", "Aetna Health Inc. (a PA corp.)", None, None, None, None),
    ("tic2023.xlsx", 6096, "VA", "CareFirst BlueChoice, Inc.", 57518, 1736, 39, 25),
    ("tic2023.xlsx", 6101, "VA", "Optimum Choice, Inc.", 47651, 20263, 674, 386),
    ("tic2023.xlsx", 6115, "VA", "Oscar Insurance Company", 6613, 610, None, None),
    ("tic2023.xlsx", 6136, "VA", "Group Hospitalization and Medical Services Inc.", 17760, 681, None, 0),
    ("tic2023.xlsx", 6140, "VA", "Innovation Health Plan, Inc.", None, None, None, None),
    ("tic2023.xlsx", 6149, "VA", "HealthKeepers, Inc.", 5627954, 887777, 646, 144),
    ("tic2023.xlsx", 6163, "VA", "Kaiser Foundation Health Plan of the Mid-Atlantic States, Inc.", 33749, 4953, 18, None),
]


def _expected_row(name, claims, denied, filed, over):
    """An insurer's row by the table's rules: 50,000 in-network claims to be listed, an appeal win rate only on 30 appeals."""
    if claims is None or denied is None or claims < 50_000:
        return None
    a = filed if filed is not None and filed >= 30 else None
    return dict(n=name, r=round(denied / claims, 4), c=claims, d=denied, a=a, o=over if a is not None else None)


def _report_insurers():
    with open(STATE_REPORT_INSURERS_CSV, newline="") as f:
        return list(csv.DictReader(f))


def _count(cell):
    return int(cell) if cell else None


class TestDenialWatchGreyStateInsurers(TestCase):
    """The insurer table's grey states: each insurer pinned to its source, under the table's rules, with its year."""

    def test_the_five_grey_states_with_a_figure_have_insurers_and_no_others(self):
        grey = DATA["grey_issuers"]
        self.assertEqual(set(grey), set(DATA["state_reports"]) | set(DATA["earlier"]))
        self.assertFalse(set(grey) & set(DATA["states"]))
        for state in ("NY", "NJ", "MA"):
            self.assertNotIn(state, grey)
        for state, g in grey.items():
            self.assertTrue(g["rows"], state)

    def test_earlier_states_rows_equal_their_puf_cells(self):
        for state in DATA["earlier"]:
            pinned = [c for c in EARLIER_PUF_CELLS if c[2] == state]
            self.assertTrue(pinned, state)
            self.assertEqual({f"tic{DATA['earlier'][state]['py']}.xlsx"}, {c[0] for c in pinned}, state)
            expected = sorted(filter(None, (_expected_row(*c[3:]) for c in pinned)), key=lambda r: r["n"])
            self.assertEqual(DATA["grey_issuers"][state]["rows"], expected, state)

    def test_earlier_states_carry_their_year_and_source(self):
        latest = DATA["trend"][-1]["year"]
        for state, e in DATA["earlier"].items():
            g = DATA["grey_issuers"][state]
            self.assertEqual((g["year"], g["kind"], g["publisher"]), (e["year"], "earlier", "CMS"), state)
            self.assertLess(g["year"], latest, state)
            self.assertTrue(g["appeals"], state)

    def test_earlier_states_rows_match_the_readouts_range(self):
        # The readout's lowest to highest is over the insurers this table lists.
        for state, e in DATA["earlier"].items():
            rates = [r["r"] for r in DATA["grey_issuers"][state]["rows"]]
            self.assertEqual(e["span"], [min(rates), max(rates)] if len(rates) > 1 else None, state)

    def test_state_report_rows_equal_the_source_csv(self):
        for state, rep in DATA["state_reports"].items():
            rows = [r for r in _report_insurers() if r["state"] == state]
            counted = [r for r in rows if not r["left_out"]]
            expected = sorted(
                filter(None, (_expected_row(r["insurer"], _count(r["in_network_claims"]), _count(r["in_network_denied"]),
                                            _count(r["internal_appeals_filed"]), _count(r["internal_appeals_overturned"]))
                              for r in counted)),
                key=lambda r: r["n"],
            )
            g = DATA["grey_issuers"][state]
            self.assertEqual(g["rows"], expected, state)
            self.assertEqual(
                g["left_out"],
                sorted((dict(n=r["insurer"], why=r["left_out"]) for r in rows if r["left_out"]), key=lambda x: x["n"]),
                state,
            )
            self.assertEqual((g["year"], g["kind"], g["publisher"], g["url"]), (rep["year"], "state", rep["publisher"], rep["url"]), state)
            self.assertEqual(g["appeals"], any(r["internal_appeals_filed"] for r in counted), state)

    def test_state_report_rows_add_up_to_the_every_state_figure(self):
        for state, rep in DATA["state_reports"].items():
            counted = [r for r in _report_insurers() if r["state"] == state and not r["left_out"]]
            self.assertEqual(
                (len(counted), sum(int(r["in_network_claims"]) for r in counted), sum(int(r["in_network_denied"]) for r in counted)),
                (rep["insurers"], rep["claims"], rep["denied"]),
                state,
            )

    def test_every_source_row_cites_its_file_and_date(self):
        for r in _report_insurers():
            who = (r["state"], r["insurer"])
            self.assertTrue(r["source_url"].startswith("https://"), who)
            self.assertNotIn(" ", r["source_url"], who)
            date.fromisoformat(r["accessed"])
            self.assertTrue(r["note"].strip(), who)
            if not r["left_out"]:
                self.assertTrue(r["page"].strip(), who)
            for part in (r["insurer"], r["left_out"], r["note"]):
                self.assertNotIn("\u2014", part)
                self.assertNotIn("\u2013", part)

    def test_california_leaves_out_the_three_insurers_its_state_figure_does(self):
        # The every-state figure's note names the same three, so the table and the chart can't disagree.
        g = DATA["grey_issuers"]["CA"]
        names = [x["n"] for x in g["left_out"]]
        self.assertEqual(names, ["Aetna CVS Health", "Health Net", "Western Health Advantage"])
        with open(REPO / "scripts" / "denial_watch" / "state_reports.csv", newline="") as f:
            note = next(r["note"] for r in csv.DictReader(f) if r["state"] == "CA")
        left = note[note.index("Left out:") :]
        for name in names:
            self.assertIn(name, left)
        # Covered California posts no appeal counts, so the table has no appeals column for California.
        self.assertFalse(g["appeals"])
        self.assertTrue(all(r["a"] is None and r["o"] is None for r in g["rows"]))

    def test_grey_rows_meet_the_table_thresholds(self):
        for state, g in DATA["grey_issuers"].items():
            for row in g["rows"]:
                self.assertGreaterEqual(row["c"], 50_000, (state, row["n"]))
                self.assertTrue(0 < row["r"] < 1, (state, row["n"]))
                self.assertEqual(row["r"], round(row["d"] / row["c"], 4), (state, row["n"]))
                if row["a"] is None:
                    self.assertIsNone(row["o"], (state, row["n"]))
                else:
                    self.assertGreaterEqual(row["a"], 30, (state, row["n"]))
                    if row["o"] is not None:
                        self.assertLessEqual(row["o"], row["a"], (state, row["n"]))

    def test_grey_rows_stay_out_of_the_national_table_and_the_map(self):
        self.assertFalse({i["s"] for i in DATA["issuers"]} & set(DATA["grey_issuers"]))
        # The map's ranges come from the latest year's table alone.
        self.assertIn("const BYST=IS.reduce(", JS)
        self.assertIn("const span=s=>{const g=BYST[s];", JS)
        self.assertEqual(JS.count("D.grey_issuers"), 1)
        dm = _between(JS, "const DM={", "/* FHI's state help page")
        self.assertNotIn("GI", dm)


class TestDenialWatchGreyStateInsurersBuilder(TestCase):
    """load_state_report_insurers and grey_issuers in scripts/denial_watch/build_bundle.py."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_builder()

    REPORT = dict(year=2024, rate=0.15, claims=200_000, denied=30_000, insurers=2, publisher="A State Office", url="https://example.org/r.pdf")

    def _row(self, insurer="Rebecca Lee Crumpler Health", claims="100000", denied="15000", filed="40", over="10",
             left_out="", page="8", year="2024", state="CA"):
        return f"{state},{year},{insurer},{claims},{denied},{filed},{over},{left_out},https://example.org/r.pdf,{page},2026-10-25,checked"

    def _load(self, *rows, reports=None):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(INSURER_HEADER + "".join(r + "\n" for r in rows))
        try:
            return self.b.load_state_report_insurers({"CA": dict(self.REPORT)} if reports is None else reports, f.name)
        finally:
            pathlib.Path(f.name).unlink()

    def _two(self, **second):
        return [self._row(), self._row(**dict(dict(insurer="Mary Eliza Mahoney Plan"), **second))]

    def test_builder_reads_the_curated_file(self):
        reports = self.b.load_state_reports(set(DATA["states"]), DATA["earlier"])
        self.assertEqual(set(self.b.load_state_report_insurers(reports)), set(DATA["state_reports"]))

    def test_counted_rows_and_left_out_rows(self):
        out = self._load(*self._two(), self._row(insurer="Susan La Flesche Picotte Care", claims="", denied="", filed="", over="", left_out="no report posted", page=""))
        self.assertEqual([i["issuer"] for i in out["CA"]["rows"]], ["Rebecca Lee Crumpler Health", "Mary Eliza Mahoney Plan"])
        self.assertEqual(out["CA"]["rows"][0], dict(state="CA", issuer="Rebecca Lee Crumpler Health", rec_in=100000.0, den_in=15000.0, ap_filed=40.0, ap_over=10.0))
        self.assertEqual(out["CA"]["left_out"], [dict(n="Susan La Flesche Picotte Care", why="no report posted")])

    def test_rows_that_dont_add_up_to_the_state_figure_stop_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(denied="15001"))
        with self.assertRaises(SystemExit):
            self._load(self._row(claims="200000", denied="30000"))

    def test_a_state_report_with_no_insurer_rows_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(), reports={"CA": dict(self.REPORT), "PA": dict(self.REPORT)})

    def test_a_state_without_a_state_report_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(), self._row(state="NY"))

    def test_another_claims_year_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(year="2023"))

    def test_more_denials_than_claims_stops_the_build(self):
        # The rows add up to the report, so only the denials-over-claims check can stop it.
        report = dict(self.REPORT, claims=150_000, denied=65_001)
        with self.assertRaises(SystemExit):
            self._load(self._row(), self._row(insurer="Mary Eliza Mahoney Plan", claims="50000", denied="50001"), reports={"CA": report})

    def test_more_appeals_overturned_than_filed_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(filed="40", over="41"))

    def test_a_counted_row_without_its_page_stops_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(*self._two(page=""))

    def test_two_rows_for_one_insurer_stop_the_build(self):
        with self.assertRaises(SystemExit):
            self._load(self._row(), self._row())

    def test_grey_issuers_use_the_tables_rules(self):
        def ins(name, claims, denied, filed, over, state="XX"):
            return dict(state=state, issuer=name, rec_in=claims, den_in=denied, ap_filed=filed, ap_over=over)

        per_year = {2025: ([ins("Big", 60_000.0, 6_000.0, 30.0, 12.0), ins("Few appeals", 80_000.0, 8_000.0, 29.0, 9.0),
                            ins("Won withheld", 90_000.0, 9_000.0, 50.0, None), ins("Small", 40_000.0, 4_000.0, 300.0, 100.0),
                            ins("Elsewhere", 70_000.0, 7_000.0, 40.0, 20.0, state="YY")], {}, 0.0)}
        earlier = {"XX": dict(year=2023, py=2025)}
        reports = {"ZZ": dict(self.REPORT)}
        report_rows = {"ZZ": dict(rows=[ins("No appeals", 100_000.0, 15_000.0, None, None, "ZZ")],
                                  left_out=[dict(n="Zed", why="b"), dict(n="Ada", why="a")])}
        out = self.b.grey_issuers(per_year, earlier, reports, report_rows)
        self.assertEqual(
            out["XX"],
            dict(year=2023, kind="earlier", publisher="CMS", url=None, appeals=True, left_out=[], rows=[
                dict(n="Big", r=0.1, c=60_000, d=6_000, a=30, o=12),
                dict(n="Few appeals", r=0.1, c=80_000, d=8_000, a=None, o=None),
                dict(n="Won withheld", r=0.1, c=90_000, d=9_000, a=50, o=None),
            ]),
        )
        self.assertFalse(out["ZZ"]["appeals"])
        self.assertEqual([x["n"] for x in out["ZZ"]["left_out"]], ["Ada", "Zed"])
        self.assertEqual((out["ZZ"]["kind"], out["ZZ"]["year"], out["ZZ"]["publisher"]), ("state", 2024, "A State Office"))


class TestDenialWatchGreyStateInsurersPage(TestCase):
    """The table says each grey state's year and source, and the copy no longer calls it HealthCare.gov only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = _page()
        cls.section = _between(cls.page, 'id="insurers"', "</section>")
        cls.names = _load_builder().STATE_NAMES

    def test_intro_names_the_grey_states_in_the_table(self):
        reports = " and ".join(sorted(self.names[s] for s in DATA["state_reports"]))
        older = sorted(self.names[s] for s in DATA["earlier"])
        year = DATA["trend"][-1]["year"]
        self.assertEqual(len(DATA["grey_issuers"]), 6)
        self.assertEqual(reports, "California and Pennsylvania")
        self.assertIn(
            f"In-network denial rates for each insurer with at least 50,000 in-network claims, as each company reported them. "
            f"The {len({i['s'] for i in DATA['issuers']})} HealthCare.gov states show {year} claims. Six states with their own marketplace "
            f"show figures from other sources or years: California (2025) and Pennsylvania (2024) from insurers' own reports, and "
            f"{', '.join(older[:-1])} and {older[-1]} from the last year CMS's files include them.",
            _text(self.section),
        )
        self.assertNotIn("each HealthCare.gov insurer", self.section)

    def test_source_and_left_out_lines_sit_with_the_table(self):
        order = [self.section.index(f'id="{i}"') for i in ("istate", "isource", "inote", "itable", "ileft")]
        self.assertEqual(order, sorted(order))
        self.assertIn('<p id="isource" hidden></p>', self.section)
        self.assertIn('<p class="dw-note" id="ileft" hidden></p>', self.section)

    def test_each_view_says_its_claims_year_and_source(self):
        # Year first and in bold; an earlier state says it now runs its own marketplace.
        self.assertIn("if(!g)return `<b>${T.year} claims</b>, from insurers' own reports to CMS for HealthCare.gov. FHI's calculation.`;", JS)
        self.assertIn(
            "if(g.kind==='earlier')return `<b>${g.year} claims</b>, from insurers' own reports to CMS, the last year its files include ${n}. "
            "FHI's calculation. ${n} now runs its own marketplace, so some of these insurers may not sell there now.`;",
            JS,
        )
        self.assertIn(
            "return `<b>${g.year} claims</b>, from insurers' own reports on the same federal form, posted by ${the(g.publisher)}"
            "<a href=\"${safeUrl(g.url)}\">${esc(g.publisher)}</a>. FHI's calculation.${g.appeals?'':' These reports have no appeal counts.'}`;}",
            JS,
        )
        self.assertEqual({DATA["state_reports"][s]["publisher"] for s in DATA["state_reports"]}, {"Covered California", "Pennsylvania Insurance Department"})
        self.assertIn("const the=p=>/Department|Office|Bureau|Division/.test(p)?'the ':'';", JS)

    def test_the_year_is_in_the_picker_and_the_count_too(self):
        self.assertIn("'<optgroup label=\"States with their own marketplace\">'+greys.map(s=>`<option value=\"${esc(s)}\">${esc(NAMES[s])}, ${GI[s].year} claims</option>`)", JS)
        self.assertIn("`<optgroup label=\"HealthCare.gov states, ${T.year} claims\">`", JS)
        self.assertIn("NAMES[s]+(g?', '+g.year+' claims':'')", JS)

    def test_left_out_insurers_are_listed_with_why(self):
        self.assertIn("return 'Not in the table: '+esc(andList([...by].map(([why,n])=>andList(n)+' ('+why+')')))+'.';}", JS)
        self.assertIn("$('ileft').innerHTML=left;$('ileft').hidden=!left;", JS)

    def test_a_report_without_appeal_counts_gets_no_appeals_column(self):
        self.assertIn("ap=!g||g.appeals", JS)
        self.assertIn("(ap?'<th class=\"dw-r\">Appeals won</th>':'')", JS)

    def test_a_grey_states_readout_links_to_its_insurers(self):
        self.assertIn("const rowsOf=s=>BYST[s]||(GI[s]&&GI[s].rows);", JS)
        self.assertIn("if(rowsOf(s))h+=line1(`<a href=\"#insurers\">See ${esc(NAMES[s])}'s insurers</a>`);", JS)

    def test_one_bar_scale_for_every_state(self):
        self.assertIn("const IMAX=Math.max(...IS.map(i=>i.r),...Object.values(GI).flatMap(g=>g.rows.map(i=>i.r)));", JS)
        self.assertIn("i.r/IMAX*90", JS)

    def test_method_note_says_where_each_states_insurers_come_from(self):
        note = _text(_between(self.page, 'id="method-insurers"', "</li>"))
        ca = DATA["grey_issuers"]["CA"]
        older = sorted((self.names[s], e["year"]) for s, e in DATA["earlier"].items())
        self.assertIn("Insurer table: Each insurer that handled at least 50,000 in-network claims.", note)
        self.assertIn("the three California insurers we leave out are named under the table, with why.", note)
        self.assertEqual(len(ca["left_out"]), 3)
        self.assertIn("Covered California posts each insurer's plan rows, not its totals, so California's figures add up those rows, and it posts no appeal counts.", note)
        self.assertIn("California's and Pennsylvania's reports can count plans sold outside the marketplace, which CMS's figures leave out", note)
        self.assertIn(
            f"For {', '.join(n for n, _ in older[:-1])} and {older[-1][0]}, it's each insurer in the last CMS file that includes the state "
            f"({', '.join(str(y) for _, y in older[:-1])} and {older[-1][1]} claims), by the same rules",
            note,
        )
        self.assertNotIn("Each reporting HealthCare.gov insurer", note)

    def test_two_california_insurers_moved_10_points_from_2024(self):
        # Covered California's 2024 reports (Kaiser 24.8%, Valley 16.4%) against the 2025 table.
        before = {"Kaiser Permanente": 0.248, "Valley Health Plan": 0.164}
        rows = {r["n"]: r["r"] for r in DATA["grey_issuers"]["CA"]["rows"]}
        self.assertTrue(all(abs(rows[n] - r) > 0.10 for n, r in before.items()))
        self.assertEqual(len(rows), 9)
        self.assertIn("Kaiser Permanente's and Valley Health Plan's rates moved more than 10 points", _text(_between(self.page, 'id="method-insurers"', "</li>")))

    def test_capital_health_plans_in_network_counts_are_left_out_and_named(self):
        self.assertNotIn(("FL", "Capital Health Plan"), {(i["s"], i["n"]) for i in DATA["issuers"]})
        self.assertIn("Capital Health Plan in Florida isn't listed: it reported denying 448,953 of 466,619 in-network claims in 2025 (96%)", _text(_between(self.page, 'id="method-insurers"', "</li>")))

    def test_the_change_view_names_the_year_it_compares_with(self):
        self.assertIn('data-m="chg">Change from 2024<', self.page)
        # The uninsured map's own change is Census's 2023 to 2024, a different comparison.
        self.assertIn('data-m="uchg">Change from 2023<', self.page)
        self.assertNotIn("since 2023", JS)

    def test_capital_health_plans_reasons_are_left_out_too(self):
        # Its rows would add 96,042 reasons and 71,065 plan-level denials.
        self.assertEqual(DATA["reason_meta"], {"entries": 86565373.0, "plan_denied_in": 63085209.0})
        self.assertEqual(_load_builder().DOUBTFUL_DENIED_IN[2027], {("FL", 66966)})

    def test_every_state_note_says_why_california_isnt_a_clean_comparison(self):
        note = _text(_between(self.page, 'id="method-every-state"', "</li>"))
        self.assertIn("Health Net (22.5% of 4.2 million claims in 2024) has no readable 2025 report", note)
        self.assertIn("Pennsylvania's 2025 claims weren't posted when we checked.", note)

    def test_the_2025_drop_note_gives_the_same_insurer_figures(self):
        note = _text(_between(self.page, 'id="method-2025-drop"', "</li>"))
        for piece in ("from 18.7% to 15.6%", "Among the 119 insurers with figures in both years, the rate fell from 18.8% to 16.4%", "from 17.7% to 17.8%", "40 insurers"):
            self.assertIn(piece, note)

    def test_map_intro_says_six_grey_states_have_insurers(self):
        intro = _text(_between(self.page, 'id="map-denials-title"', "</p>"))
        self.assertIn("A grey state shows what it publishes instead, and six of them also have insurers in the table below, from other sources or years.", intro)
        self.assertEqual(len(DATA["grey_issuers"]), 6)


class TestDenialWatchEnrollment(TestCase):
    """The open-enrollment box shows only in season; tests pin the date."""


class TestDenialWatchEnrollmentCard(TestCase):
    """Open enrollment has its own page: in season Denial Watch keeps a small card for it, and out of season one line."""

    # Medicare's window, HealthCare.gov's alone, and the card's last day.
    SEASON_DAYS = (date(2026, 10, 18), date(2026, 12, 8), date(2027, 1, 15))

    def _card(self, day):
        return _between(_page(day), 'id="enroll"', "</section>")

    def test_card_in_season_is_a_line_one_tile_and_the_link(self):
        html = _page(date(2026, 10, 18))
        card = _between(html, 'id="enroll"', "</section>")
        self.assertIn('<section class="fhi-card dw-enroll-card" id="enroll" aria-labelledby="enroll-title">', html)
        self.assertIn('<h2 class="fhi-card-title" id="enroll-title">Open enrollment for 2027</h2>', card)
        self.assertIn(
            "Compare plans and costs for 2027, even if you'll be renewed automatically.",
            _text(_between(card, '<p id="enroll-line">', "</p>")),
        )
        self.assertEqual(card.count('<div class="dw-stat">'), 1)
        self.assertIn(
            f'<p class="dw-cta"><a href="{reverse("open-enrollment")}">Open enrollment: dates, numbers and what to do</a></p>',
            card,
        )

    def test_card_tile_is_the_latest_plan_picks_in_the_bundle(self):
        picks = _picks()
        year = max(picks)
        tile = re.search(
            r'<div class="dw-stat"><p class="dw-stat-head"><b class="dw-num">([^<]+)</b> <strong>([^<]+)</strong></p><span>([^<]+)</span></div>',
            self._card(date(2026, 10, 18)),
        )
        self.assertEqual(
            tile.groups(),
            (_millions(picks[year]), "picked a marketplace plan", f"for {year} in open enrollment, including people renewed automatically"),
        )

    def test_card_sits_near_the_top(self):
        html = _page(date(2026, 10, 18))
        self.assertLess(html.index('id="stats"'), html.index('id="enroll"'))
        self.assertLess(html.index('id="enroll"'), html.index('class="fhi-jump-links"'))

    def test_card_prints_no_healthcare_gov_dates(self):
        # CMS set the 2027 marketplace dates, but the court ruling behind them is under appeal, so the pages link out.
        for day in self.SEASON_DAYS:
            card = self._card(day)
            for text in ("November 1", "December 15", "January 15"):
                with self.subTest(day=day, text=text):
                    self.assertNotIn(text, card)

    def test_card_stays_through_the_last_day_in_hawaii(self):
        # 9am UTC on January 16 is still January 15, the card's last day, in Hawaii; 11am is not.
        for hour, shown in ((9, True), (11, False)):
            when = datetime(2027, 1, 16, hour, 0, tzinfo=dt_timezone.utc)
            with self.subTest(hour=hour), mock.patch("django.utils.timezone.now", return_value=when):
                html = Client().get(reverse("denial-watch")).content.decode()
                self.assertEqual('class="fhi-card dw-enroll-card"' in html, shown)

    def test_out_of_season_one_plain_link(self):
        html = _page(date(2027, 1, 16))
        self.assertIn(
            f'<p class="dw-cta" id="enroll"><a href="{reverse("open-enrollment")}">Signing up for marketplace coverage: numbers and what to do</a></p>',
            html,
        )
        for gone in ('class="fhi-card dw-enroll-card"', "Open enrollment for", "Picking a 2027 HealthCare.gov plan?", "<b>Picking a"):
            self.assertNotIn(gone, html)

    def test_season_words_come_from_the_constants(self):
        # Next fall's update is two constants in views.py, not a hunt through the template.
        with (
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_MEDICARE_ENROLLMENT", (date(2027, 10, 15), date(2027, 12, 7))),
            mock.patch("fighthealthinsurance.views.DENIAL_WATCH_ENROLLMENT_BOX_ENDS", date(2028, 1, 15)),
        ):
            html = _page(date(2027, 10, 20))
        card = _between(html, 'id="enroll"', "</section>")
        self.assertIn('<h2 class="fhi-card-title" id="enroll-title">Open enrollment for 2028</h2>', card)
        self.assertIn("Compare plans and costs for 2028,", _text(card))
        self.assertIn("Picking a 2028 marketplace plan?", _between(html, 'id="insurers"', "</section>"))
        self.assertIn("<b>Picking a 2028 plan:</b>", _between(html, 'id="method-who-denies"', "</div>"))
        # The tile keeps the data's own year.
        self.assertIn(f"for {max(_picks())} in open enrollment", card)

    def test_insurer_section_points_to_healthcare_gov_in_season(self):
        # The table has grey states too, so the notice points to state-run marketplaces as well.
        html = _page(date(2026, 10, 18))
        notice = _between(_between(html, 'id="insurers"', "</section>"), "Picking a 2027 marketplace plan?", "</p>")
        self.assertIn(
            'then compare plans on <a href="https://www.healthcare.gov/">HealthCare.gov</a> or '
            '<a href="https://www.healthcare.gov/marketplace-in-your-state/">your state\'s marketplace</a>.',
            notice,
        )

    def test_insurer_notice_names_oregon_and_the_insurers_leaving(self):
        # From November 1, 2026, Oregon enrolls through ExploreHealthOR.gov; the table is 2025 data and no insurer is named.
        html = _page(date(2026, 10, 18))
        notice = _between(_between(html, 'id="insurers"', "</section>"), "Picking a 2027 marketplace plan?", "</p>")
        self.assertIn("Oregon residents compare 2027 plans at <a href=\"https://explorehealthor.gov/\">ExploreHealthOR.gov</a> instead.", notice)
        # Most of the table is 2025 claims; the grey states' own years are in the table.
        self.assertIn("its insurers' rates (2025 claims in most states)", notice)
        self.assertIn("Some of these insurers don't sell plans for 2027, so check which ones do.", notice)
        self.assertNotIn("Cigna", notice)

    def test_insurer_notice_cites_its_sources_in_season(self):
        # Both facts in the notice, with the date they were checked, in the insurer table's method group.
        group = _between(_page(date(2026, 10, 18)), 'id="method-who-denies"', "</div>")
        note = _between(group, "<b>Picking a 2027 plan:</b>", "</li>")
        self.assertIn('href="https://www.healthcare.gov/marketplace-in-your-state/"', note)
        self.assertIn('href="https://www.kff.org/affordable-care-act/tracking-insurer-participation-changes-in-the-aca-marketplaces-in-2027/"', note)
        self.assertIn("checked October 19, 2026.", _text(note))

    def test_the_questions_chart_and_tiles_moved_off_the_page(self):
        for day in (date(2026, 10, 18), date(2027, 1, 16)):
            html = _page(day)
            for gone in ('id="enroll-questions"', 'id="mkline"', 'id="mktable"', 'id="mkpicks"', 'id="enroll-numbers"', "dw-quote", "dw-meter", 'id="method-enroll"', "Enrollment questions"):
                with self.subTest(day=day, gone=gone):
                    self.assertNotIn(gone, html)
        # The script names the moved ids only to hand old links to the new page, never to draw them.
        self.assertNotIn("$('mkline')", JS)
        self.assertNotIn("D.marketplace", JS)

    def test_jump_menu_drops_the_questions(self):
        nav = _between(_page(date(2026, 10, 18)), 'class="fhi-jump-links"', "</nav>")
        self.assertNotIn("enroll", nav)

    def test_coverage_points_to_the_open_enrollment_page(self):
        coverage = _between(_page(), 'id="coverage"', "</section>")
        self.assertIn('id="cmap"', coverage)
        self.assertIn(
            f'<p id="coverage-marketplace">See marketplace coverage year by year on <a href="{reverse("open-enrollment")}">the open enrollment page</a>.</p>',
            coverage,
        )

    def test_sources_point_to_the_open_enrollment_pages_method(self):
        link = f'<a href="{reverse("open-enrollment")}#method-enroll">the open enrollment page\'s sources</a>'
        latest = max(MK["picks"], key=lambda p: p["y"])
        for day, names_the_tile in ((date(2026, 10, 18), True), (date(2027, 1, 16), False)):
            fold = _between(_between(_page(day), 'id="sources"', "</section>"), "<details", "</details>")
            pointer = _between(fold, '<p class="dw-sources" id="method-enroll-pointer">', "</p>")
            with self.subTest(day=day):
                self.assertIn(link, pointer)
                self.assertIn("<b>Open enrollment and marketplace coverage:</b>", pointer)
                # In season the card's tile is on the page, so its source is named here too.
                self.assertEqual(f'href="{latest["url"]}"' in pointer, names_the_tile)
                if names_the_tile:
                    self.assertIn(
                        f"the plan picks at the top of this page are CMS's count, from its {latest['y']} Marketplace Open Enrollment Period public use files.",
                        _text(pointer),
                    )

    def test_card_and_pointer_have_no_em_dash_or_insurer(self):
        html = _page(date(2026, 10, 18))
        parts = _between(html, 'id="enroll"', "</section>") + _between(html, 'id="method-enroll-pointer"', "</p>")
        self.assertNotIn("—", parts)
        for name in {_short(i["n"]) for i in DATA["issuers"]}:
            self.assertNotIn(name, _text(parts))


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

    def test_marketplace_decisions_are_appealed_to_the_marketplace(self):
        bullet = _between(self.content, 'id="marketplace-appeals"', "</li>")
        self.assertIn(
            "appeal to the marketplace itself, generally within 90 days of your eligibility notice, "
            "on HealthCare.gov's appeals page or your state marketplace's site.",
            _text(bullet),
        )
        self.assertIn('href="https://www.healthcare.gov/marketplace-appeals/"', bullet)
        self.assertIn('href="https://www.healthcare.gov/marketplace-in-your-state/"', bullet)

    def test_appeals_bullet_says_insurer_denials_are_a_different_appeal(self):
        bullet = _text(_between(self.content, 'id="marketplace-appeals"', "</li>"))
        self.assertIn("A denial from your insurer is a different appeal, the kind the rest of this page and Fight Health Insurance help with.", bullet)
        self.assertNotIn(chr(0x2014), bullet)

    def test_appeals_bullet_sits_in_the_card_with_its_check_date(self):
        card = _between(self.content, 'id="who-to-call"', "</section>")
        self.assertIn('id="marketplace-appeals"', card)
        self.assertIn("Numbers and links checked October 22, 2026.", _text(card))

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

    def test_every_kind_of_plan_shares_one_scale(self):
        # The highest rate over every row, not the view's own, so a bar's length compares across clicks.
        self.assertIn("const PAMAX=Math.max(0,...PA.filter(r=>r.std!=null).map(r=>r.std))||1;", JS)
        self.assertIn("const mx=PAMAX;", JS)
        self.assertNotIn("const mx=rows[0].std", JS)
        self.assertIn(
            "All four kinds of plan share one scale, up to the highest rate in any of them, so the same rate gets the same length of bar whichever button you pick.",
            _text(_between(self.section, 'id="pascale"', "</p>")),
        )

    def test_heading_above_the_bars_names_the_view_picked(self):
        # The page carries the first view's heading for readers without the script, and the script swaps it on each click.
        self.assertIn('<h4 id="pahead">Medicare Advantage plans: share of standard requests denied</h4>', self.section)
        self.assertLess(self.section.index('id="pahead"'), self.section.index('id="pabars"'))
        self.assertIn("const PAH={ma:'Medicare Advantage plans',mcd:'Medicaid plans',mkt:'HealthCare.gov plans',ffs:'State Medicaid programs'};", JS)
        self.assertIn("$('pahead').textContent=PAH[k]+': share of standard requests denied';", JS)
        self.assertEqual(re.findall(r'data-m="(\w+)"', _between(self.section, 'id="pamode"', "</div>")), ["ma", "mcd", "mkt", "ffs"])

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
        # A pale pink, the drug spending colour, with the dark label ink.
        self.assertIn("background-color: var(--dw-cost-tint)", rule)
        self.assertIn("color: var(--dw-tile-ink)", rule)
        # The palette is in the styles Denial Watch shares with the open enrollment page.
        tokens = (REPO / "fighthealthinsurance" / "templates" / "partials" / "denial_watch_style.html").read_text()
        ink, ground = (parse_colour(re.search(rf"--{name}: (#[0-9a-f]{{6}})", tokens).group(1)) for name in ("dw-tile-ink", "dw-cost-tint"))
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
            "https://www.cms.gov/Medicare/Appeals-and-Grievances/MMCAG/Downloads/Parts-C-and-D-Enrollee-Grievances-Organization-Coverage-Determinations-and-Appeals-Guidance.pdf",
            "https://www.ecfr.gov/current/title-29/subtitle-B/chapter-XXV/subchapter-G/part-2560/section-2560.503-1",
            "https://www.ecfr.gov/current/title-42/chapter-IV/subchapter-C/part-438/subpart-F/section-438.406",
        ):
            self.assertIn(f'href="{href}"', sources)


JS = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch.js").read_text()
# The fold opener, number formats and line chart, shared with the open enrollment page.
CHARTS_JS = (REPO / "fighthealthinsurance" / "static" / "js" / "denial_watch_charts.js").read_text()


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

    def test_national_rates_round_as_the_page_says(self):
        latest = DATA["trend"][-1]
        self.assertEqual(
            (_pct(latest["in_rate"]), _pct(latest["out_rate"]), _pct(latest["all_rate"])),
            ("16%", "31%", "17%"),
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
        self.assertEqual(gone, {"MI"})
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
        self.assertIn("There's no out-of-network change from 2024, which mostly reflects which insurers reported, and no out-of-network appeals figure.", self.sources)

    def test_map_has_no_out_of_network_state_figures(self):
        # A state's rate would mostly show plan type, so the map points to the trend chart instead.
        views = _between(JS, "const DM={", "\n};")
        self.assertNotIn("out:{", views)
        self.assertIn("we don't map out-of-network claims by state (see What are they denying? below)", self.sources)
        self.assertIn("we don't map them by state: in 2025, 99 of the 135 insurers reporting them sold only HMO or EPO plans", self.sources)

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
        self.assertIn('aria-label="Line chart of the in-network denial rate by year, 2020 to 2025"', self.content)
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
            f"For {_millions(closed['denied'])} of those out-of-network denials, more than half, insurers gave the reason",
            self.sources,
        )

    def test_method_note_names_the_left_out_insurers(self):
        for t in DATA["trend"]:
            for x in t["left_out"]:
                self.assertIn(_short(x["n"]), self.sources)
        self.assertIn("shows no figure for Michigan", self.sources)


class TestDenialWatchCharts(TestCase):
    """The line charts stay readable on a phone and keep their labels apart."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.style = _between(_page(), "<style>", "</style>")

    def test_phone_chart_text_is_enlarged(self):
        # The 560-wide drawing fills about 310px on a phone, which made 12px text under 7px. The coverage donut has a phone block of its own.
        phone = "\n".join(re.findall(r"@media \(max-width: 560px\) \{(.*?)\n  \}", self.style, re.S))
        self.assertIn(".dw-chart text { font-size: 20px; }", phone)
        self.assertIn(".dw-chart text.dw-end { font-size: 22px; }", phone)
        # The edge labels run a few pixels past the drawing at that size.
        self.assertIn(".dw-chart { overflow: visible; }", phone)

    def test_end_label_goes_under_a_falling_line(self):
        # Above the point, "18.7% denied" sat on the 2023 to 2024 line.
        self.assertIn("below=last>0&&pts[last-1].y>lp.y", CHARTS_JS)
        self.assertIn('y="${Y(lp.y)+(below?26:-12)}"', CHARTS_JS)
        self.assertIn('<text class="dw-end"', CHARTS_JS)

    def test_last_year_label_is_not_crowded(self):
        # California's chart showed "2024 2025" side by side.
        self.assertIn("if(i===last||(i%step===0&&last-i>=step))", CHARTS_JS)


THEMES = [
    ("why-appeal", "Why should I appeal?", "method-why-appeal"),
    ("who-denies", "Who is denying the most?", "method-who-denies"),
    ("what-why", "What are they denying, and why?", "method-what-why"),
    ("what-changed", "What changed from 2024 to 2025?", "method-what-changed"),
    ("coverage", "Who doesn't have coverage?", "method-coverage"),
]


class TestDenialWatchThemes(TestCase):
    """Five questions carry the page, each short, with its method one link away."""

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
    def test_answers_run_the_width_of_their_section(self):
        # Melanie: a 46rem cap wrapped one-line answers early beside empty space on a wide screen.
        rule = re.search(r"\.dw-answer \{([^}]*)\}", STYLE_PARTIAL).group(1)
        self.assertNotIn("max-width", rule)

    def test_chart_titles_are_short_with_the_finding_leading_the_text(self):
        # Each chart's heading names the chart; what it shows is the first sentence under it.
        titles = {
            "appeals": ("Independent review in California", "On HealthCare.gov plans, the federal marketplace, insurers reversed"),
            "map-denials": ("Denial rates by state", "Rates run from about"),
            "every-state": ("Every state's denial rate", "Share of in-network claims denied, by state:"),
            "insurers": ("Denial rates by insurer", "In some states, one insurer denies four or more times as often as another."),
            "prior-auth": ("Prior authorization denials", "Insurers now post how often they deny prior authorization, and the spread is wide."),
            "trend": ("Denials over time", "A lower denial rate in 2025, though we'd read it with care."),
            "why": ("Reasons given for denials", '"Not medically necessary" is a small share of the reasons given.'),
        }
        for ident, (title, lead) in titles.items():
            section = _between(self.content, f'<section class="fhi-stack" id="{ident}" aria-labelledby="{ident}-title">', "</p>")
            self.assertRegex(section, rf'<h3 id="{ident}-title">{re.escape(title)}</h3>\s*<p[^>]*>{re.escape(lead)}', ident)

    def test_insurer_heading_matches_the_spread_inside_states(self):
        # "Four or more times": the widest gaps in the table, state by state.
        by_state = {}
        for i in DATA["issuers"]:
            by_state.setdefault(i["s"], []).append(i["r"])
        ratios = [max(v) / min(v) for v in by_state.values()]
        self.assertGreaterEqual(sum(r >= 4 for r in ratios), 2, sorted(ratios)[-5:])
        self.assertIn(
            '<h3 id="insurers-title">Denial rates by insurer</h3>\n                <p>In some states, one insurer denies four or more times as often as another. ',
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
CMS_EFFECTUATED = "https://data.cms.gov/summary-statistics-on-beneficiary-enrollment/health-insurance-marketplace/health-insurance-exchanges-monthly-effectuated-enrollment"
# CMS's early 2025 effectuated report counted February 2025 on March 15, 2025, sooner than the file the page uses.
EARLY_FEB_2025 = 23_393_198
# Every open enrollment's plan picks: CMS's files from 2017, HHS ASPE's reports of CMS data before that.
CMS_OEP_PUFS = "https://www.cms.gov/data-research/statistics-trends-and-reports/marketplace-products"
ASPE_REPORTS = {
    2014: "https://www.aspe.hhs.gov/sites/default/files/migrated_legacy_files/44226/ib_2014Apr_enrollment.pdf",
    2015: "https://www.aspe.hhs.gov/sites/default/files/migrated_legacy_files/137886/ib_2015mar_enrollment.pdf",
    2016: "https://aspe.hhs.gov/sites/default/files/private/pdf/187866/Finalenrollment2016.pdf",
}
# The files behind the marketplace numbers and the people under 65 beside them, which the open enrollment page names.
POPULATION_SOURCES = {
    "https://www2.census.gov/programs-surveys/popest/datasets/2010-2020/intercensal/national/asrh/nc-est2020int-agesex-res.csv",
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/national/asrh/nc-est2025-agesex-res.csv",
}
MARKETPLACE_SOURCES = {CMS_EFFECTUATED, CMS_OEP_PUFS} | {p["url"] for p in MK["picks"]} | set(ASPE_REPORTS.values()) | POPULATION_SOURCES


def _census_year() -> str:
    return max(DATA["sahie"], key=int)


def _picks() -> dict[int, int]:
    return {p["y"]: p["n"] for p in MK["picks"]}


def _february() -> dict[int, int]:
    return {r["y"]: r["n"] for r in MK["feb"]}


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

    def test_builder_reads_the_curated_history(self):
        self.assertEqual(_load_builder().load_open_enrollment_history(MK["picks"]), MK["history"])

    def test_sources_give_the_history_years(self):
        years = {s["url"]: s["years"] for s in DATA["sources"]}
        self.assertEqual(years[CMS_OEP_PUFS], f"2017-{MK['history'][-1]['y']}")
        for y, url in ASPE_REPORTS.items():
            self.assertEqual(years[url], str(y))


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

    def test_build_checks_plan_pick_zips_in_the_raw_directory(self):
        rows = self._rows([2025, 2026], no_december=[2026])
        lines = ['"State","Platform","Year","Month","Effectuated_Enr"'] + [",".join(r.values()) for r in rows]
        path = self._temp("\n".join(lines) + "\n", ".csv")
        picks = self._picks_rows(2025, 2026)
        # Made-up rows pass the file check only with their own sha256.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with mock.patch.object(self.b, "EFFECTUATED_SHA256", digest):
            with mock.patch.object(self.b, "RAW", self._zip_dir(b"not the zip")), self.assertRaisesRegex(SystemExit, "not the file_sha256 recorded"):
                self.b.build_marketplace(path, picks, self._history_rows(2014))
            with mock.patch.object(self.b, "RAW", self._zip_dir(None)):
                mk = self.b.build_marketplace(path, picks, self._history_rows(2014))
        self.assertEqual((mk["prelim"], [r["y"] for r in mk["feb"]], [p["y"] for p in mk["picks"]]), (2026, [2025, 2026], [2025, 2026]))
        self.assertEqual(mk["history"], [dict(y=2014, n=3, fed=2, sbm=1, pub="CMS")])

    HISTORY = dict(
        year="2014",
        plan_selections="3",
        healthcare_gov="2",
        state_based="1",
        publisher="CMS",
        source_url="https://www.cms.gov/files/zip/x.zip",
        source_file="x.csv",
    )

    def _history_rows(self, *years, **change):
        rows = [{**self.HISTORY, "year": str(y), **change} for y in years]
        lines = [",".join(self.HISTORY)] + [",".join(f'"{v}"' for v in r.values()) for r in rows]
        return self._temp("\n".join(lines) + "\n", ".csv")

    def test_complete_history_rows_load_in_year_order(self):
        history = self.b.load_open_enrollment_history([], self._history_rows(2015, 2014, publisher="HHS ASPE, from CMS data"))
        self.assertEqual(
            history,
            [dict(y=2014, n=3, fed=2, sbm=1, pub="HHS ASPE, from CMS data"), dict(y=2015, n=3, fed=2, sbm=1, pub="HHS ASPE, from CMS data")],
        )

    def test_history_total_must_be_healthcare_gov_plus_state_run(self):
        with self.assertRaisesRegex(SystemExit, "plan_selections for 2014 isn't healthcare_gov plus state_based"):
            self.b.load_open_enrollment_history([], self._history_rows(2014, plan_selections="4"))

    def test_history_years_run_from_2014_without_a_gap_or_a_repeat(self):
        for years in ((2014, 2016), (2015, 2016), (2014, 2015, 2015), ()):
            with self.subTest(years=years), self.assertRaisesRegex(SystemExit, "years must run from 2014 without a gap or a repeat"):
                self.b.load_open_enrollment_history([], self._history_rows(*years))

    def test_history_publisher_is_cms_or_aspe_from_cms_data(self):
        for bad in ("", "KFF", "HHS ASPE", "HHS ASPE (CMS data)", "cms"):
            with self.subTest(bad=bad), self.assertRaisesRegex(SystemExit, ": publisher must"):
                self.b.load_open_enrollment_history([], self._history_rows(2014, publisher=bad))

    def test_history_cites_cms_or_aspe_over_https(self):
        for good in ("https://aspe.hhs.gov/x.pdf", "https://www.aspe.hhs.gov/x.pdf", "https://www.cms.gov/x.zip"):
            self.assertEqual(len(self.b.load_open_enrollment_history([], self._history_rows(2014, source_url=good))), 1)
        for bad in ("", "http://www.cms.gov/x.zip", "https://cms.gov.example.org/x.zip", "https://www.kff.org/x", "https://data.cms.gov/x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(SystemExit, ": source_url must"):
                self.b.load_open_enrollment_history([], self._history_rows(2014, source_url=bad))
        with self.assertRaisesRegex(SystemExit, ": source_file must"):
            self.b.load_open_enrollment_history([], self._history_rows(2014, source_file=" "))

    def test_history_counts_must_be_above_zero(self):
        for column, bad in (("plan_selections", "0"), ("healthcare_gov", ""), ("state_based", "-1"), ("state_based", "1.0"), ("year", "14")):
            with self.subTest(column=column, bad=bad), self.assertRaisesRegex(SystemExit, f": {column} must"):
                self.b.load_open_enrollment_history([], self._history_rows(2014, **{column: bad}))

    def test_history_must_match_the_plan_picks_for_a_year_in_both(self):
        path = self._history_rows(2014, 2015)
        self.assertEqual(len(self.b.load_open_enrollment_history([dict(y=2015, n=3, url="https://www.cms.gov/x"), dict(y=2026, n=1, url="")], path)), 2)
        with self.assertRaisesRegex(SystemExit, "2015 has 3 plans picked, and marketplace_signups.csv has 4"):
            self.b.load_open_enrollment_history([dict(y=2015, n=4, url="https://www.cms.gov/x")], path)

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

    def test_report_lists_each_open_enrollment(self):
        mk = dict(feb=[], prelim=None, picks=[], history=[dict(y=2014, n=3_000_000, fed=2, sbm=1, pub="HHS ASPE, from CMS data")])
        self.assertEqual(self.b.marketplace_report(mk), ["open enrollment 2014 3,000,000 picked (HHS ASPE, from CMS data)"])

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


class TestDenialWatchWholePage(TestCase):
    """Checks across the whole page that outlived the enrollment section's move."""

    def test_no_id_is_used_twice_in_season_or_out(self):
        for day in (date(2026, 10, 18), date(2027, 1, 16)):
            body = _between(_page(day), '<div class="fhi-page-wide dw"', '<script id="dw-data"')
            ids = re.findall(r'\sid="([^"]+)"', body)
            self.assertEqual(len(ids), len(set(ids)), sorted(i for i in ids if ids.count(i) > 1))

    def test_kff_is_described_the_way_it_asks_everywhere(self):
        content = _page()
        self.assertNotIn("independent health policy research group", content)
        more = _text(_between(content, '<a href="https://www.kff.org/">KFF</a>', "</li>"))
        self.assertIn("A nonprofit health policy research, polling, and news organization.", more)

    def test_no_link_to_a_kff_pdf(self):
        for href in re.findall(r'href="(https://www\.kff\.org/[^"]*)"', _page()):
            self.assertFalse(href.lower().endswith(".pdf"), href)


class TestDenialWatchSourcesFold(TestCase):
    """Sources and method fold into one closed <details>, and a link to anything inside opens it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page(date(2026, 10, 18))
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
        self.assertIn('<section class="fhi-stack" id="sources" aria-labelledby="sources-title">', self.content)

    def test_every_method_group_is_inside_the_fold(self):
        groups = re.findall(r'<div class="dw-method" id="([\w-]+)">', self.sources)
        self.assertEqual(
            groups,
            ["method-data", "method-why-appeal", "method-who-denies", "method-what-why", "method-what-changed", "method-coverage", "method-costs", "method-other"],
        )
        for group in groups + ["method-enroll-pointer"]:
            self.assertIn(f'id="{group}"', self.fold)

    def test_every_link_into_the_sources_lands_inside_the_fold(self):
        page = _between(self.content, '<div class="fhi-page-wide dw"', '<script id="dw-data"')
        targets = set(re.findall(r'href="#(method-[\w-]+)"', page + JS))
        self.assertTrue({"method-why-appeal", "method-who-denies", "method-what-why", "method-coverage", "method-costs", "method-data"} <= targets, targets)
        for target in targets:
            self.assertIn(f'id="{target}"', self.fold)

    def test_script_opens_a_closed_details_for_a_link_inside_it(self):
        opener = _between(CHARTS_JS, "/* Sources and the enrollment questions start closed", "const fmt=")
        # Every closed <details> around the target opens, the target's own included.
        self.assertIn("for(let d=el;d;d=d.parentElement)if(d.tagName==='DETAILS'&&!d.open){d.open=true;shut=true;}", opener)
        # On load, after the charts are drawn; on every hash change; and on a click, before the browser jumps.
        self.assertIn("setTimeout(landOn,0);window.addEventListener('hashchange',landOn);", opener)
        self.assertIn(
            "document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href^=\"#\"]');if(a)openTo(a.getAttribute('href'));});",
            opener,
        )
        self.assertIn("const landOn=()=>{const el=openTo(location.hash);if(el)el.scrollIntoView();};", opener)

    def test_script_opens_before_reading_the_data(self):
        # A problem with the data, or a chart that throws, can't leave a link stuck on a closed fold.
        self.assertLess(JS.index("foldLinks();"), JS.index("const D=JSON.parse"))
        self.assertNotIn("const openTo=", JS)


TEMPLATE_DIR = REPO / "fighthealthinsurance" / "templates"
STYLE_PARTIAL = (TEMPLATE_DIR / "partials" / "denial_watch_style.html").read_text()
OE_JS = (REPO / "fighthealthinsurance" / "static" / "js" / "open_enrollment.js").read_text()
HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
# Machado, Oliveira and Fernandes (2009), full deuteranopia and protanopia, on linear RGB.
DEUTAN = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))
PROTAN = ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998))


def _linear(rgb):
    return [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255 for v in rgb)]


def _lab(linear):
    """CIELAB (D65) from linear RGB, for lightness and colour difference."""
    r, g, b = (min(max(c, 0.0), 1.0) for c in linear)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return (116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z)))


def _lightness(token):
    from tests.sync.test_contrast import dw_colour

    return _lab(_linear(dw_colour(token)))[0]


def _seen_with(matrix, token):
    """A token's colour in CIELAB as someone with that colour blindness sees it."""
    from tests.sync.test_contrast import dw_colour

    lin = _linear(dw_colour(token))
    return _lab([sum(m * c for m, c in zip(row, lin)) for row in matrix])


def _delta(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _style_blocks(name):
    return re.findall(r"<style>(.*?)</style>", (TEMPLATE_DIR / name).read_text(), re.S)


def _view(table, key):
    """One view's entry in the script's DM or CM table of map views."""
    body = _between(JS, f"const {table}={{", "\n};")
    return re.search(rf"^  {key}:\{{(.*?)\}},?$", body, re.M | re.S).group(1)


class TestDenialWatchColourMeaning(TestCase):
    """Blue is denials, green good for patients, purple coverage and health, pink drug spending, red only a rise, grey only no data; colour is never the only cue."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.content = _page()

    def test_chart_colours_are_written_once_as_tokens(self):
        # Defined in the shared partial and nowhere else, so both pages and every chart move together.
        declared = re.findall(r"(--dw-[\w-]+):", STYLE_PARTIAL)
        self.assertEqual(len(declared), len(set(declared)), declared)
        for name in ("denial_watch.html", "open_enrollment.html"):
            self.assertNotRegex((TEMPLATE_DIR / name).read_text(), r"--dw-[\w-]+:")

    def test_scripts_write_no_colour_of_their_own(self):
        for script in (JS, CHARTS_JS, OE_JS):
            self.assertEqual(HEX.findall(script), [])

    def test_page_styles_write_no_chart_colour_outside_the_tokens(self):
        # The ticker's white words on the dark panel are the one literal left, and the contrast gate measures it.
        tokens = _between(STYLE_PARTIAL, ".dw {", "}")
        for name in ("denial_watch.html", "open_enrollment.html"):
            for block in _style_blocks(name):
                self.assertEqual(set(HEX.findall(block)) - {"#fff"}, set(), name)
        self.assertEqual(set(HEX.findall(STYLE_PARTIAL.replace(tokens, ""))), set())

    def test_denials_are_blue(self):
        self.assertIn(".dw-bar-fill { height: 100%; border-radius: 2px; background-color: var(--dw-deny); }", self.content)
        # The denial trend, the denial maps, prior authorization and the insurers' bars.
        self.assertIn("color:tok('--dw-deny'),ink:tok('--dw-deny-deep'),label:y=>pct(y)+' denied'", JS)
        for view in ("in", "all"):
            self.assertIn("c:DENY,", _view("DM", view))
        self.assertIn('$(\'pabars\').innerHTML=rows.map(r=>`<div class="dw-bar">', JS)
        self.assertIn("vertical-align: middle; background-color: var(--dw-deny); }", _between(self.content, ".dw-mini {", "\n"))

    def test_red_is_only_for_a_rise(self):
        # Two red steps on the change scale, a rising bar, and the key's swatch; nothing else reads the red tokens.
        self.assertEqual(re.findall(r"BAD\[\d\]", JS), ["BAD[2]", "BAD[4]"])
        self.assertIn("cm==='uchg'?(v<0?'dw-bar-good':'dw-bar-bad')", JS)
        uses = [u for u in re.findall(r"[^\n]*var\(--dw-bad[^\n]*", self.content) if not u.strip().startswith("--dw-")]
        self.assertEqual(
            [u.strip() for u in uses],
            [".dw-sw-bad { background-color: var(--dw-bad); }", ".dw-bar-bad .dw-bar-fill { background-color: var(--dw-bad); }"],
        )

    def test_coverage_and_health_are_purple(self):
        # The uninsured and the CDC measures share one map, so one purple, and their bars with it.
        for view in ("unins", "fair_poor_health", "diabetes", "depression"):
            self.assertIn("c:HEALTH,", _view("CM", view))
        self.assertIn("const cls=v=>cm==='uchg'?(v<0?'dw-bar-good':'dw-bar-bad'):'dw-bar-health';", JS)
        self.assertIn(".dw-bar-health .dw-bar-fill { background-color: var(--dw-health-4); }", self.content)
        self.assertNotIn("COUNT", JS)

    def test_drug_spending_is_pink(self):
        self.assertIn('$(\'dgbars\').innerHTML=rows.map(d=>`<div class="dw-bar dw-bar-cost', JS)
        self.assertIn(".dw-bar-cost .dw-bar-fill { background-color: var(--dw-cost); }", self.content)
        self.assertIn('<p class="dw-key" id="dgkey"><span><i class="dw-sw dw-sw-cost"></i>', self.content)
        self.assertNotIn("dw-bar-count", JS + self.content)

    def test_each_colour_reads_as_the_hue_the_key_names(self):
        from tests.sync.test_contrast import dw_colour

        lab = {name: _lab(_linear(dw_colour(name))) for name in ("--dw-deny", "--dw-health-4", "--dw-cost")}
        self.assertLess(lab["--dw-deny"][2], -20)  # blue
        self.assertGreater(lab["--dw-health-4"][1], 20)  # purple: red and blue together
        self.assertLess(lab["--dw-health-4"][2], -20)
        self.assertGreater(lab["--dw-cost"][1], 45)  # pink: strongly magenta-red...
        self.assertTrue(-25 < lab["--dw-cost"][2] < 10)  # ...neither orange-red nor purple-blue

    def test_pressed_toggles_take_their_sections_colour(self):
        # Never a dark grey: blue for denials, purple for coverage and health, pink for drug spending; aria-pressed says it too.
        self.assertIn('.dw-toggles .fhi-button[aria-pressed="true"] { background-color: var(--dw-deny); border-color: var(--dw-deny); color: var(--dw-paper); }', self.content)
        self.assertIn('.dw-toggles-health .fhi-button[aria-pressed="true"] { background-color: var(--dw-health-4); border-color: var(--dw-health-4); }', self.content)
        self.assertIn('.dw-toggles-cost .fhi-button[aria-pressed="true"] { background-color: var(--dw-cost); border-color: var(--dw-cost); color: var(--dw-paper); }', self.content)
        groups = {ident: cls for cls, ident in re.findall(r'<div class="fhi-cluster ([\w -]+)" id="(\w+)" role="group"', self.content)}
        self.assertEqual(
            groups,
            {"dmode": "dw-toggles", "pamode": "dw-toggles", "tmode": "dw-toggles", "cmode": "dw-toggles dw-toggles-health", "dgmode": "dw-toggles dw-toggles-cost"},
        )
        self.assertNotIn("fhi-ink-2); color: #fff; }", _between(self.content, ".dw-toggles {", "</style>"))

    def test_good_things_are_green(self):
        self.assertIn("color:tok('--dw-good'),ink:tok('--dw-good-ink'),label:y=>pct(y,0)+' overturned'", JS)
        self.assertIn("$('imrbars').innerHTML=D.imr_cat.map(r=>`<div class=\"dw-bar dw-bar-good\">", JS)
        self.assertIn("c:GOOD,", _view("DM", "won"))
        self.assertIn(".dw-bar-good .dw-bar-fill { background-color: var(--dw-good); }", self.content)

    def test_informative_measures_take_their_own_colours(self):
        # Health measures purple, people and money blue: neither good nor bad, and each apart from the others.
        for view in ("fair_poor_health", "diabetes", "depression"):
            self.assertIn("c:HEALTH,", _view("CM", view))
        self.assertNotIn("dw-bar-low", JS + self.content)
        self.assertIn("color:tok('--dw-count')", OE_JS)
        # The open enrollment page's coverage blue is the denial blue: that page shows no denials.
        self.assertIn("--dw-count: var(--dw-deny-4);", STYLE_PARTIAL)
        hues = {name: _seen_with(((1, 0, 0), (0, 1, 0), (0, 0, 1)), name) for name in ("--dw-deny", "--dw-bad", "--dw-good", "--dw-health-4", "--dw-cost")}
        for a in hues:
            for b in hues:
                if a < b:
                    self.assertGreater(_delta(hues[a], hues[b]), 20, (a, b))

    def test_a_change_runs_green_through_pale_to_red(self):
        self.assertIn("const DIV=[GOOD[4],GOOD[2],tok('--dw-mid'),BAD[2],BAD[4]];", JS)
        self.assertIn("c:DIV,mk:true,", _view("DM", "chg"))
        self.assertIn("c:DIV,mk:true,", _view("CM", "uchg"))
        # Each side darkens away from the middle, so the scale reads in grayscale.
        for side in ("good", "bad"):
            self.assertGreater(_lightness("--dw-mid"), _lightness(f"--dw-{side}-2") + 15)
            self.assertGreater(_lightness(f"--dw-{side}-2"), _lightness(f"--dw-{side}-4") + 15)

    def test_grey_means_no_data_and_nothing_else(self):
        uses = re.findall(r"tok\('--dw-na'\)", JS)
        self.assertEqual(len(uses), 2)
        self.assertIn("gone?tok('--dw-gone'):v==null?tok('--dw-na'):scale(v)", JS)
        self.assertIn("parts.push(`<div><i style=\"background:${tok('--dw-na')}\"></i><span>${na}</span></div>`);", JS)
        self.assertEqual(re.findall(r"[^\n]*var\(--dw-na\)[^\n]*", self.content), ["  .dw-sw-na { background-color: var(--dw-na); }"])
        # The change scale's middle is a warm white, apart from the grey in lightness too.
        self.assertGreater(_lightness("--dw-mid") - _lightness("--dw-na"), 4)

    def test_ramps_step_down_in_lightness(self):
        for ramp in ("deny", "bad", "good", "health"):
            steps = [_lightness(f"--dw-{ramp}-{i}") for i in range(6)]
            for lighter, darker in zip(steps, steps[1:]):
                self.assertGreater(lighter - darker, 8, (ramp, steps))

    def test_red_and_green_stay_apart_with_red_green_colour_blindness(self):
        # Apart in lightness as well as hue, and still apart as deuteranopes and protanopes see them.
        self.assertGreater(_lightness("--dw-good") - _lightness("--dw-bad"), 10)
        for matrix in (DEUTAN, PROTAN):
            self.assertGreater(_delta(_seen_with(matrix, "--dw-good"), _seen_with(matrix, "--dw-bad")), 20)
            self.assertGreater(_delta(_seen_with(matrix, "--dw-good-2"), _seen_with(matrix, "--dw-bad-2")), 15)

    def test_blue_and_green_stay_apart_with_red_green_colour_blindness(self):
        # Denials and appeals won sit side by side in the appeals section and the tiles.
        for matrix in (DEUTAN, PROTAN):
            self.assertGreater(_delta(_seen_with(matrix, "--dw-good"), _seen_with(matrix, "--dw-deny")), 20)

    def test_change_maps_mark_direction_with_a_triangle(self):
        self.assertIn('<path class="dw-mark" data-x="${x}" data-y="${y}"/>', JS)
        self.assertIn("mk.setAttribute('d',dir==='up'?", JS)
        self.assertIn("const dirBy=b=>v=>v>=b[2]?'up':v<b[1]?'down':'';", JS)
        self.assertIn("${d==='up'?'▲ ':d==='down'?'▼ ':''}", JS)
        for table, view in (("DM", "chg"), ("CM", "uchg")):
            self.assertIn("mk:true,", _view(table, view))
            key = re.search(r"key:'([^']*)'", _view(table, view)).group(1)
            self.assertIn("▲ Red: rose", key)
            self.assertIn("▼ Green: fell", key)

    def test_every_map_view_says_its_colours_in_words(self):
        self.assertIn("const parts=key?[`<p class=\"dw-legend-key\">${key}</p>`]:[];", JS)
        for table, views in (("DM", ("in", "all", "chg", "won")), ("CM", ("unins", "uchg", "fair_poor_health", "diabetes", "depression"))):
            for view in views:
                entry = _view(table, view)
                self.assertRegex(entry, r"key:'[^']*(darker|Red|Green)[^']*'", view)
                self.assertRegex(entry, r"name:'[^']+'", view)
        self.assertIn("legend($('dleg'),m.b,m.c,m.l,'No data',m.gk,m.key,dir);", JS)
        self.assertIn("legend($('cleg'),m.b,m.c,m.l,'No data','',m.key,dir);", JS)

    def test_every_map_has_its_figures_as_a_table(self):
        for leg, alt, table in (("dleg", "dalt", "dtable"), ("cleg", "calt", "ctable")):
            fold = _between(self.content, f'id="{alt}"', "</details>")
            self.assertIn(f'<table class="dw-table" id="{table}"></table>', fold)
            self.assertIn("<summary>Every state's figure, as a table</summary>", fold)
            self.assertLess(self.content.index(f'id="{leg}"'), self.content.index(f'id="{alt}"'))
        self.assertIn("mapTable($('dtable'),m.name,m.v,m.tt||m.t,m.naOf);", JS)
        self.assertIn("mapTable($('ctable'),m.name,m.v,m.tt||m.t);", JS)

    def test_each_chart_has_a_key_in_words(self):
        keys = {
            "tkey": ["Share of claims denied"],
            "imrkey": ["Denials overturned, decided in the patient's favour"],
            "reasonskey": ["Share of all reasons given for denials", "Darker, in bold: the reasons about medical judgment"],
            "pakey": ["Share of standard requests denied"],
            "dgkey": ["Medicare Part D spending, before rebates"],
        }
        for ident, words in keys.items():
            self.assertIn(f'<p class="dw-key" id="{ident}">', self.content)
            key = _between(self.content, f'<p class="dw-key" id="{ident}">', "</p>")
            self.assertEqual(re.findall(r'<i class="dw-sw[^"]*"></i>([^<]+)</span>', key), words, ident)

    def test_highlighted_reasons_are_bold_as_well_as_darker(self):
        self.assertIn(".dw-bar-deep .dw-bar-name > span:first-child { font-weight: 700; }", self.content)
        self.assertIn("'dw-bar-deep':''", JS)

    def test_tiles_say_denied_or_won_beside_their_colour(self):
        tiles = re.findall(r"\[[^\[\]]*?'([^']+)',[^\[\]]*?,'(deny|good)'\]", _between(JS, "$('stats').innerHTML=[", "].map("))
        self.assertEqual([k for _, k in tiles], ["deny", "deny", "good", "good"])
        for label, kind in tiles:
            self.assertIn("deni" if kind == "deny" else "won", label)
        self.assertIn(".dw-stat-deny { border-top-color: var(--dw-deny); }", self.content)
        self.assertIn(".dw-stat-good { border-top-color: var(--dw-good); }", self.content)

    def test_every_state_chart_is_blue_with_a_texture_per_source(self):
        # Every bar is a denial rate, so blue; solid, striped or hollow says which kind of record, as the keys say in words.
        self.assertIn("i.dw-src-cms { background-color: var(--dw-deny); }", self.content)
        self.assertIn(".dw-src-state .dw-bar-fill, i.dw-src-state { background: repeating-linear-gradient(135deg, var(--dw-deny) 0 3px, var(--dw-deny-1) 3px 6px); }", self.content)
        self.assertIn(".dw-src-earlier .dw-bar-fill, i.dw-src-earlier { background: var(--dw-paper); box-shadow: inset 0 0 0 2px var(--dw-deny); }", self.content)
        self.assertIn('<details class="dw-years" id="every-alt">', self.content)

    def test_page_key_names_each_colour_in_words(self):
        key = _text(_between(self.content, '<p class="dw-key" id="colour-key">', "</p>"))
        self.assertEqual(
            key,
            "Colours in the charts: blue, denials; green, good for patients, such as appeals won; purple, coverage and health, such as the uninsured; "
            "pink, drug spending; red, only where something got worse; grey, no data.",
        )
        self.assertLess(self.content.index('class="fhi-jump-links"'), self.content.index('id="colour-key"'))


class TestDenialWatchPy2027File(TestCase):
    """The PY2027 file renamed and split columns; these are its headers as CMS published them."""

    def test_reason_columns_map_to_the_2027_in_network_headers(self):
        builder = _load_builder()
        columns = builder.REASON_COLUMNS_2027
        self.assertEqual(set(columns), set(builder.REASONS))
        for header in (
            "plan_number_claims_denied_due_to_enrollee_benefit_limit_reached_in_network",
            "plan_number_claims_denied_due_to_investigational_experimental_cosmetic_procedure_in_network",
            "plan_number_claims_denied_referral_required_in_network",
        ):
            self.assertIn(header, columns.values())

    def test_issuer_cells_read_appeals_of_claims_not_requests_before_care(self):
        row = {
            "state": "TX", "issuer_name": "Example", "issuer_id": 1,
            "issuer_internal_appeals_pre_service_benefit_requests_filed": 900,
            "issuer_number_internal_appeals_pre_service_benefit_requests_overturned": 800,
            "issuer_internal_appeals_claims_filed": 10,
            "issuer_number_internal_appeals_claims_overturned": 3,
        }
        cells = _load_builder().issuer_cells(row)
        self.assertEqual((cells["ap_filed"], cells["ap_over"]), (10, 3))

    def test_a_state_report_can_lag_the_map_by_one_year_only(self):
        builder = _load_builder()
        latest = builder.CLAIMS_YEAR[builder.LATEST_PY]
        fields = "state,claims_year,insurers,in_network_claims,in_network_denied,publisher,source_url,accessed,note\n"
        for year, ok in ((latest, True), (latest - 1, True), (latest - 2, False)):
            with self.subTest(year=year), tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "reports.csv"
                path.write_text(fields + f"CA,{year},1,10,1,Example,https://example.org/,2026-11-01,\n")
                if ok:
                    self.assertEqual(builder.load_state_reports(set(), {}, path)["CA"]["year"], year)
                else:
                    with self.assertRaises(SystemExit):
                        builder.load_state_reports(set(), {}, path)

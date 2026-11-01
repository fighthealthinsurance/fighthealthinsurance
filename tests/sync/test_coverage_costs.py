"""What health coverage costs next year, at one address with no year in it.

The page is one of the number pages, in Denial Watch's style like the open
enrollment page. Every figure sits beside its label (official, final,
proposed, projected or survey), the source that published it and when. These
tests hold the page to that, pin the figures to what the sources said when we
checked them, and pin the links in from the coverage guide and the open
enrollment page.
"""

import re

from bs4 import BeautifulSoup
from django.test import Client, TestCase, override_settings
from django.urls import reverse

MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE = rf"{MONTH} (?:\d{{1,2}}, )?\d{{4}}"
SOURCES = ("KFF", "Families USA", "IRS", "CMS", "OPM", "Medicare Trustees", "Marsh")
# A label: its kind, who published the figure and when, as "Final, KFF, August 3, 2026".
LABEL = re.compile(
    rf"^\(?(Official|Final|Proposed|Projected|Survey), ({'|'.join(SOURCES)}), {DATE}(?: and {DATE})?\)?$"
)
# A dollar amount or a percentage.
FIGURE = re.compile(r"\$\d|\d%")
# Every part of the page before Sources and method, which may name figures only beside their labels.
FIGURE_SECTIONS = ("costs-top", "marketplace", "job-based", "federal", "medicare", "act", "pending", "more-data")


def _page() -> str:
    return Client().get(reverse("coverage-costs")).content.decode()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _label_notes(node):
    """A tile's closing note, or the note spans in a cell, item or paragraph; a note paragraph is text, not a label."""
    if "dw-stat" in (node.get("class") or []):
        return node.find_all("p", class_="dw-note", recursive=False)
    return node.select("span.dw-note")


def _labels(node) -> list[str]:
    return [_text(note) for note in _label_notes(node)]


def _without_labels(node) -> str:
    """The text a figure sits in, with its label notes taken out."""
    copy = BeautifulSoup(str(node), "html.parser").find(node.name)
    for note in _label_notes(copy):
        note.decompose()
    return _text(copy)


def _units(section):
    """Each piece of the section a figure can sit in: a tile, or a cell, item or paragraph outside the tiles."""
    tiles = section.select(".dw-stat")
    others = [
        node
        for node in section.find_all(["th", "td", "li", "p", "caption", "h2", "h3"])
        if node.find_parent(class_="dw-stat") is None and "dw-stat" not in (node.get("class") or [])
    ]
    return tiles + others


def _rows(table) -> list[tuple[str, ...]]:
    return [tuple(_text(cell) for cell in row.find_all(["th", "td"])) for row in table.find("tbody").find_all("tr")]


class CoverageCostsPageTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        response = Client().get(reverse("coverage-costs"))
        cls.status = response.status_code
        cls.html = response.content.decode()
        cls.soup = _soup(cls.html)

    def test_renders_at_its_address(self):
        self.assertEqual(reverse("coverage-costs"), "/coverage-costs")
        self.assertEqual(self.status, 200)

    def test_the_address_has_no_year_in_it(self):
        # So links and search ranking carry over when the content moves to the next year.
        self.assertIsNone(re.search(r"20\d\d", reverse("coverage-costs")))

    def test_opens_with_the_title_block_like_open_enrollment(self):
        self.assertRegex(self.html, r'<header class="fhi-page-title">\s*<h1>What health coverage costs in 2027</h1>\s*</header>')
        self.assertIn("<title>What Health Coverage Costs in 2027: Premiums by Kind of Coverage</title>", self.html)
        self.assertIn('<link rel="canonical" href="https://www.fighthealthinsurance.com/coverage-costs">', self.html)

    def test_sits_on_the_wide_column_in_denial_watchs_style(self):
        self.assertIn('<div class="fhi-page-wide dw">', self.html)
        styles = re.findall(r"<style>(.*?)</style>", self.html, re.S)
        self.assertTrue(any(".dw-stat {" in block for block in styles))
        # Only the shared partial's styles: the page adds none of its own.
        self.assertEqual(sum(".dw-stat {" in block or ".dw {" in block for block in styles), 1)

    def test_a_tiles_kicker_and_note_are_spaced_by_the_tile_not_paragraph_margins(self):
        self.assertIn(".dw-stat > p { margin: 0; }", self.html)

    def test_opens_sources_and_method_with_the_shared_script(self):
        self.assertIn("js/denial_watch_charts.js", self.html)
        self.assertIn("window.DWCharts.foldLinks()", self.html)
        self.assertLess(self.html.index("js/denial_watch_charts.js"), self.html.index("window.DWCharts.foldLinks()"))

    def test_sections_come_in_order_by_kind_of_coverage(self):
        main = self.soup.find("main")
        self.assertEqual(
            [_text(h) for h in main.find_all("h2")],
            [
                "Marketplace plans",
                "Job-based plans",
                "Federal employees and retirees",
                "Medicare",
                "What to do now",
                "Still to come",
                "Where to find more data like this",
                "More on costs, coverage and denials",
                "Sources and method",
            ],
        )
        sources = self.soup.find("section", id="sources")
        self.assertEqual(
            [_text(h) for h in sources.find_all("h3")],
            ["Marketplace plans", "Job-based plans", "Federal employees and retirees", "Medicare", "Still to come"],
        )

    def test_each_section_is_named_by_its_heading(self):
        for section in self.soup.find("div", class_="fhi-page-wide").find_all("section"):
            with self.subTest(section=section.get("id")):
                self.assertEqual(section.get("aria-labelledby"), section.find("h2").get("id"))

    def test_is_in_the_sitemap(self):
        sitemap = Client().get("/sitemap.xml").content.decode()
        self.assertIn(f"{reverse('coverage-costs')}</loc>", sitemap)


class CoverageCostsFiguresTest(TestCase):
    """Every figure has its label, source and date, and says what its source said."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.soup = _soup(_page())

    def _section(self, name: str):
        found = self.soup.find(id=name)
        self.assertIsNotNone(found, name)
        return found

    def test_every_figure_has_a_label_and_a_date(self):
        for name in FIGURE_SECTIONS:
            for node in _units(self._section(name)):
                if not FIGURE.search(_without_labels(node)):
                    continue
                with self.subTest(section=name, text=_text(node)[:60]):
                    self.assertNotIn(node.name, ("th", "caption", "h2", "h3"), "a figure in a heading or caption has no label")
                    labels = _labels(node)
                    self.assertEqual(len(labels), 1, labels)
                    self.assertRegex(labels[0], LABEL)

    def test_the_tiles_give_one_headline_number_for_each_kind_of_coverage(self):
        tiles = [
            (
                _text(tile.find(class_="dw-kicker")),
                _text(tile.find("b", class_="dw-num")),
                _text(tile.find("strong")),
                _text(tile.find("span")),
                _text(tile.find("p", class_="dw-note")),
            )
            for tile in self._section("costs-numbers").select(".dw-stat")
        ]
        self.assertEqual(
            tiles,
            [
                ("Marketplace", "13%", "rise in benchmark plan prices", "Average for 2027, before premium help.", "Final, KFF, October 30, 2026"),
                ("Job-based", "8.2%", "rise in employer health costs", "Expected per employee for 2027.", "Projected, Marsh, September 2, 2026"),
                ("Federal employees", "10.9%", "more paid by FEHB enrollees", "On average, for 2027.", "Official, OPM, September 2026"),
                ("Medicare", "$209.50", "a month for Part B", "The standard premium expected for 2027.", "Projected, Medicare Trustees, June 2026"),
            ],
        )

    def test_every_table_cell_has_its_own_label(self):
        tables = self.soup.find_all("table")
        self.assertEqual([t.get("id") for t in tables], ["marketplace-table", "federal-table", "medicare-table"])
        for table in tables:
            self.assertEqual(table.get("class"), ["dw-table"])
            self.assertIn("dw-table-wrap", table.parent.get("class"))
            for cell in table.find("tbody").find_all("td"):
                with self.subTest(table=table.get("id"), cell=_text(cell)):
                    self.assertEqual(cell.get("class"), ["dw-num"])
                    self.assertTrue(FIGURE.search(_without_labels(cell)))
                    self.assertRegex(" ".join(_labels(cell)), LABEL)

    def test_each_table_shows_2026_next_to_2027(self):
        for table in self.soup.find_all("table"):
            with self.subTest(table=table.get("id")):
                head = [_text(th) for th in table.find("thead").find_all("th")]
                self.assertEqual(head[1:], ["2026", "2027"])
                self.assertIsNotNone(table.find("caption"))

    def test_marketplace_figures(self):
        self.assertEqual(
            _rows(self._section("marketplace-table")),
            [
                ("Average change in the benchmark plan's price", "26% Final, KFF, October 30, 2026", "13% Final, KFF, October 30, 2026"),
                ("Median price change", "20% Final, KFF, August 3, 2026", "15% Proposed, KFF, August 3, 2026"),
                (
                    "Average price change, weighted by enrollment",
                    "25.5% Final, Families USA, October 26, 2026",
                    "15.6% Proposed, Families USA, October 26, 2026",
                ),
                (
                    "Most you pay for the benchmark plan with premium help, as a share of income",
                    "9.96% Official, IRS, July 2025",
                    "10.22% Official, IRS, July 2026",
                ),
                (
                    "Most you pay out of pocket in a year, for covered in-network care",
                    "$10,600 for one person, $21,200 for a family Official, CMS, January 29, 2026",
                    "$12,000 for one person, $24,000 for a family Official, CMS, January 29, 2026",
                ),
            ],
        )

    def test_federal_figures(self):
        label = "Official, OPM, September 2026"
        self.assertEqual(
            _rows(self._section("federal-table")),
            [
                ("FEHB premiums", f"10.2% {label}", f"9.3% {label}"),
                ("What FEHB enrollees pay", f"12.3% {label}", f"10.9% {label}"),
                ("PSHB premiums", f"9% {label}", f"6.7% {label}"),
                ("What PSHB enrollees pay", f"11.3% {label}", f"8.2% {label}"),
            ],
        )

    def test_medicare_figures(self):
        self.assertEqual(
            _rows(self._section("medicare-table")),
            [
                ("Part B premium, per month", "$202.90 Official, CMS, November 14, 2025", "$209.50 Projected, Medicare Trustees, June 2026"),
                ("Part B deductible, per year", "$283 Official, CMS, November 14, 2025", "$292 Projected, Medicare Trustees, June 2026"),
                ("Medicare Advantage premium, average per month", "$14.37 Official, CMS, September 28, 2026", "$12.00 Projected, CMS, September 28, 2026"),
                ("Drug plan on its own (Part D), average per month", "$35.09 Official, CMS, September 28, 2026", "$36 Projected, CMS, September 28, 2026"),
                ("Most you pay for covered Part D drugs in a year", "$2,100 Official, CMS, April 6, 2026", "$2,400 Official, CMS, April 6, 2026"),
                ("Part D deductible, at most", "$615 Official, CMS, April 6, 2026", "$700 Official, CMS, April 6, 2026"),
            ],
        )

    def test_job_based_figures(self):
        items = {li["id"]: _text(li) for li in self._section("job-based").find_all("li")}
        self.assertEqual(
            items,
            {
                "job-premiums": "Average yearly premium, 2026: $9,804 for one person and $28,303 for a family. Each rose 5% from 2025. (Survey, KFF, October 22, 2026)",
                "job-worker-share": "What workers paid, 2026: $1,672 for one person and $7,471 for a family. The family share rose 9% from 2025. (Survey, KFF, October 22, 2026)",
                "job-2027": "Expected for 2027: employers expect their health benefit cost per employee to rise 8.2% on average, even after changes such as higher deductibles. (Projected, Marsh, September 2, 2026)",
            },
        )

    def test_benchmark_prices_differ_by_platform(self):
        self.assertEqual(
            _text(self._section("by-platform")),
            "It differs by state. Benchmark prices rise 11% in states that use HealthCare.gov and 16% in states that run their own platforms. (Final, KFF, October 30, 2026)",
        )

    def test_the_50_dollar_premium_claim_is_cms_and_warns_about_deductibles(self):
        self.assertEqual(
            _text(self._section("low-premiums")),
            "Check more than the premium. CMS says more than 75% of people buying through HealthCare.gov can get premiums at or below $50 in 2027. "
            "A low premium can come with a high deductible. (Official, CMS, October 30, 2026)",
        )

    def test_most_marketplace_enrollees_get_help_so_list_prices_arent_what_they_pay(self):
        self.assertEqual(
            _text(self._section("most-get-help")),
            "Most people get premium help. About 87% of people who picked a plan for 2026 got premium tax credits. (Official, CMS, March 2026)",
        )
        self.assertIn("When list prices rise, your help usually does too.", _text(self._section("help-rises")))

    def test_the_400_percent_cutoff_is_the_federal_credit_and_states_may_help(self):
        self.assertEqual(
            _text(self._section("no-help-above")),
            "No federal premium tax credit above 400% of the poverty level. "
            "Some states have their own help; your state's marketplace can tell you. "
            "(Official, IRS, July 2026)",
        )

    def test_says_the_big_jump_came_from_2025_to_2026(self):
        self.assertEqual(
            _text(self._section("big-jump")),
            "For many people, the big jump came in 2026. The extra premium help ended after 2025. "
            "The average premium after help rose from $113 a month for 2025 to $178 for 2026. "
            "So 2026 to 2027 understates what long-time enrollees felt. (Official, CMS, May 2025 and March 2026)",
        )

    def test_the_labels_are_explained_in_a_key_before_the_numbers(self):
        key = self._section("cc-labels")
        self.assertEqual(key.get("class"), ["dw-key"])
        self.assertEqual([_text(span.find("b")) for span in key.find_all("span")], ["Official:", "Final:", "Proposed:", "Projected:", "Survey:"])
        page = str(self.soup)
        self.assertLess(page.index('id="cc-labels"'), page.index('id="marketplace"'))

    def test_every_source_in_a_label_is_named_in_sources_and_method(self):
        used = set()
        for name in FIGURE_SECTIONS:
            for node in _units(self._section(name)):
                for label in _labels(node):
                    used.add(LABEL.match(label).group(2))
        self.assertEqual(used, set(SOURCES))
        sources = _text(self._section("sources"))
        for source in SOURCES:
            self.assertIn(source, sources)


class CoverageCostsSourcesTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.html = _page()
        start = cls.html.index('<section class="fhi-stack" id="sources"')
        cls.sources = cls.html[start : cls.html.index("</section>", start)]
        cls.soup = _soup(cls.html)
        cls.page = cls.soup.find("div", class_="fhi-page-wide")

    def test_sources_are_linked(self):
        for href in (
            "https://www.kff.org/affordable-care-act/how-much-and-why-aca-marketplace-premiums-are-going-up-in-2027/",
            "https://www.kff.org/affordable-care-act/9-things-to-watch-for-the-2027-aca-open-enrollment-period/",
            "https://www.cms.gov/newsroom/fact-sheets/plan-year-2027-marketplace-plans-prices-fact-sheet-0",
            "https://www.familiesusa.org/resources/aca-marketplace-premium-rate-tracker-insurers-likely-to-propose-premium-increases-for-2027/",
            "https://www.irs.gov/pub/irs-drop/rp-25-25.pdf",
            "https://www.irs.gov/pub/irs-drop/rp-26-26.pdf",
            "https://www.cms.gov/files/document/2027-papi-parameters-guidance-2026-01-29.pdf",
            "https://www.cms.gov/data-research/statistics-trends-reports/marketplace-products/2025-marketplace-open-enrollment-period-public-use-files",
            "https://www.cms.gov/data-research/statistics-trends-reports/marketplace-products/2026-marketplace-open-enrollment-period-public-use-files",
            "https://www.kff.org/health-costs/2026-employer-health-benefits-survey/",
            "https://www.mercer.com/en-us/about/newsroom/employers-expect-health-benefit-costs-to-jump-in-2027/",
            "https://www.opm.gov/healthcare-insurance/open-season/agency-benefits-officers/housing-for-os-materials/2026-federal-benefits-open-season-highlights-for-plan-year-2027.pdf",
            "https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles",
            "https://www.cms.gov/oact/tr/2026",
            "https://www.cms.gov/newsroom/press-releases/medicare-advantage-medicare-prescription-drug-programs-expected-remain-stable-2027",
            "https://www.cms.gov/files/document/2027-announcement.pdf",
            "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-B/part-154/subpart-C/section-154.301",
        ):
            self.assertIn(f'href="{href}"', self.sources)

    def test_sources_and_method_is_the_number_pages_fold(self):
        fold = self.soup.find("details", id="sources-details")
        self.assertEqual(fold.get("class"), ["fhi-accordion-item"])
        self.assertIsNone(fold.get("open"))
        self.assertEqual(fold.find("summary").find("h2").get("id"), "sources-title")
        # Each section's "How we got these numbers" lands on its method inside the fold, and the method links back.
        for how in self.page.select("p.dw-how a"):
            target = how["href"].lstrip("#")
            with self.subTest(target=target):
                method = fold.find(id=target)
                self.assertIsNotNone(method)
                back = method.find("p", class_="dw-back").find("a")["href"].lstrip("#")
                self.assertEqual(self.soup.find(id=back).find("a", href=how["href"]), how)
        self.assertEqual(len(self.page.select("p.dw-how a")), 4)

    def test_says_when_the_numbers_were_checked(self):
        top = _text(self.soup.find(id="costs-top"))
        self.assertTrue(top.endswith("Numbers and links checked November 1, 2026."), top[-80:])

    def test_says_the_one_figure_that_is_our_math(self):
        self.assertIn("except the 87%, which is our math from CMS's counts", self.sources)
        self.assertIn("The 87% is the 20,035,756 people who got a premium tax credit out of the 23,130,860 who picked a plan.", self.sources)

    def test_cites_kff_without_copying_its_charts_and_leaves_out_acasignups(self):
        self.assertEqual(self.page.find_all(["img", "svg", "iframe", "canvas", "picture"]), [])
        self.assertNotIn("acasignups", self.html.lower())

    def test_no_em_or_en_dash(self):
        copy = [str(self.page), _text(self.soup.find("title")), self.soup.find("meta", attrs={"name": "description"})["content"]]
        for text in copy:
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn(chr(0x2013), text)


class CoverageCostsActPendingAndMoreDataTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.soup = _soup(_page())

    def test_names_what_is_still_pending_and_when(self):
        cards = {card["id"]: _text(card) for card in self.soup.find(id="pending").select(".fhi-card")}
        self.assertEqual(
            cards,
            {
                "pending-final-rates": "The final median and weighted price changes When KFF and Families USA publish them.",
                "pending-part-b": "The official Part B premium Mid November. Last year's came out November 14.",
                "pending-paid": "What enrollees pay after help Around March 2027, from CMS's open enrollment files.",
            },
        )
        self.assertIn("We try to update this page as each one comes out.", _text(self.soup.find(id="pending")))

    def test_what_to_do_now_links_each_place_to_see_your_own_price(self):
        steps = {li["id"]: [a["href"] for a in li.find_all("a")] for li in self.soup.find(id="act").find_all("li")}
        self.assertEqual(
            steps,
            {
                "act-healthcare-gov": ["https://www.healthcare.gov/see-plans/"],
                "act-state": ["https://www.healthcare.gov/marketplace-in-your-state/"],
                "act-medicare": ["https://www.medicare.gov/plan-compare/"],
                "act-federal": [
                    "https://www.opm.gov/healthcare-insurance/healthcare/plan-information/compare-plans/",
                    "https://www.opm.gov/healthcare-insurance/healthcare/plan-information/premiums/",
                ],
                "act-job": ["https://www.healthcare.gov/health-care-law-protections/summary-of-benefits-and-coverage/"],
                "act-share": [reverse("share_plan_documents")],
                "act-help": ["https://www.healthcare.gov/find-local-help/", reverse("state_help_index")],
                "act-checklist": [f"{reverse('coverage-changes')}#fall-checklist"],
            },
        )

    def test_each_step_is_one_short_sentence_after_its_lead(self):
        for li in self.soup.find(id="act").find_all("li"):
            rest = _text(li)[len(_text(li.find("b"))) :].strip()
            with self.subTest(step=li["id"]):
                self.assertLessEqual(len(re.findall(r"[.?](?:\s|$)", rest)), 1, rest)
                self.assertLessEqual(len(rest.split()), 25, rest)

    def test_points_to_more_data_and_asks_for_more(self):
        section = self.soup.find(id="more-data")
        self.assertEqual(
            [a["href"] for a in section.select("ul.fhi-list h3 a")],
            [
                "https://www.kff.org/affordable-care-act/state-indicator/average-marketplace-premiums-by-metal-tier/",
                "https://www.cms.gov/data-research/statistics-trends-and-reports/marketplace-products",
                "https://www.healthcare.gov/health-and-dental-plan-datasets-for-researchers-and-issuers/",
            ],
        )
        self.assertIn(f'href="{reverse("contact")}"', str(section.find(id="join-forces-costs")))
        self.assertIn("Checked November 1, 2026.", _text(section))


class CoverageCostsIsLinkedTest(TestCase):
    """Preparing for 2027 and the open enrollment page both link here."""

    def setUp(self):
        self.href = f'href="{reverse("coverage-costs")}"'

    def test_the_coverage_guide_links_it_from_more_help(self):
        # Not from inside a question's answer: Tab from an open question
        # goes to the next question (the keyboard test checks it).
        html = Client().get(reverse("coverage-changes")).content.decode()
        more = html[html.index('<section id="resources"') : html.index("</section>", html.index('<section id="resources"'))]
        self.assertIn(f"<li><a {self.href}>What health coverage costs in 2027</a></li>", more)

    @override_settings(DENIAL_WATCH_PAGE_ENABLED=True)
    def test_the_open_enrollment_page_links_it_in_its_steps_section(self):
        html = Client().get(reverse("open-enrollment")).content.decode()
        start = html.index('id="enroll-act"')
        steps = html[start : html.index("</section>", start)]
        self.assertIn(
            f'<p id="coverage-costs-link">For premiums and price changes by kind of coverage, see <a {self.href}>what health coverage costs</a>.</p>',
            steps,
        )

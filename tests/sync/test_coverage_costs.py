"""What health coverage costs next year, at one address with no year in it.

Every figure on the page sits beside its label (official, final, proposed,
projected or survey), the source that published it and when. These tests
hold the page to that, pin the figures to what the sources said when we
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
# The sections that give numbers, and the ones around them that must not slip one in unlabelled.
FIGURE_SECTIONS = ("cc-intro", "marketplace", "job-based", "federal", "medicare", "pending", "your-price")


def _page() -> str:
    return Client().get(reverse("coverage-costs")).content.decode()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _labels(node) -> list[str]:
    return [_text(note) for note in node.find_all("span", class_="fhi-note")]


def _without_labels(node) -> str:
    """The text a figure sits in, with its label notes taken out."""
    copy = BeautifulSoup(str(node), "html.parser")
    for note in copy.find_all("span", class_="fhi-note"):
        note.decompose()
    return _text(copy)


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

    def test_opens_with_the_title_and_lede(self):
        self.assertEqual([_text(h) for h in self.soup.find_all("h1")], ["What health coverage costs in 2027"])
        self.assertEqual(
            _text(self.soup.find("p", class_="fhi-page-lede")),
            "What we know so far about 2027 prices, by the kind of coverage you have.",
        )
        self.assertIn("<title>What Health Coverage Costs in 2027: Premiums by Kind of Coverage</title>", self.html)
        self.assertIn('<link rel="canonical" href="https://www.fighthealthinsurance.com/coverage-costs">', self.html)

    def test_sits_on_the_reading_column(self):
        self.assertIn('<div class="fhi-page">', self.html)

    def test_sections_come_in_order_by_kind_of_coverage(self):
        main = self.soup.find("main")
        self.assertEqual(
            [_text(h) for h in main.find_all("h2")],
            [
                "Marketplace plans",
                "Job-based plans",
                "Federal employees and retirees",
                "Medicare",
                "Still to come",
                "Where to see your own price",
                "Sources and method",
            ],
        )
        sources = self.soup.find("section", id="sources")
        self.assertEqual(
            [_text(h) for h in sources.find_all("h3")],
            ["Marketplace plans", "Job-based plans", "Federal employees and retirees", "Medicare", "Still to come"],
        )

    def test_each_section_is_named_by_its_heading(self):
        for section in self.soup.find("div", class_="fhi-page").find_all("section"):
            with self.subTest(section=section.get("id")):
                heading = section.find("h2")
                self.assertEqual(section.get("aria-labelledby"), heading.get("id"))

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
            section = self._section(name)
            for node in section.find_all(["th", "td", "li", "p", "caption", "h2", "h3"]):
                if not FIGURE.search(_without_labels(node)):
                    continue
                with self.subTest(section=name, text=_text(node)[:60]):
                    self.assertIn(node.name, ("td", "li", "p"), "a figure in a heading or caption has no label")
                    labels = _labels(node)
                    self.assertEqual(len(labels), 1, labels)
                    self.assertRegex(labels[0], LABEL)

    def test_every_table_cell_has_its_own_label(self):
        tables = self.soup.find_all("table")
        self.assertEqual([t.get("id") for t in tables], ["marketplace-table", "federal-table", "medicare-table"])
        for table in tables:
            for cell in table.find("tbody").find_all("td"):
                with self.subTest(table=table.get("id"), cell=_text(cell)):
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
                ("Price change for the typical insurer (the median)", "20% Final, KFF, August 3, 2026", "15% Proposed, KFF, August 3, 2026"),
                (
                    "Average price change, counting each plan by how many people are in it",
                    "25.5% Final, Families USA, October 26, 2026",
                    "15.6% Proposed, Families USA, October 26, 2026",
                ),
                (
                    "Most you pay for the benchmark plan with premium help, as a share of income",
                    "9.96% Official, IRS, July 2025",
                    "10.22% Official, IRS, July 2026",
                ),
                (
                    "Most you pay out of pocket in a year for covered, in-network care",
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
                ("Part B standard premium, per month", "$202.90 Official, CMS, November 14, 2025", "$209.50 Projected, Medicare Trustees, June 2026"),
                ("Part B deductible, per year", "$283 Official, CMS, November 14, 2025", "$292 Projected, Medicare Trustees, June 2026"),
                ("Medicare Advantage, average premium per month", "$14.37 Official, CMS, September 28, 2026", "$12.00 Projected, CMS, September 28, 2026"),
                ("Drug plan on its own (Part D), average premium per month", "$35.09 Official, CMS, September 28, 2026", "$36 Projected, CMS, September 28, 2026"),
                ("Most you pay for covered Part D drugs in a year", "$2,100 Official, CMS, April 6, 2026", "$2,400 Official, CMS, April 6, 2026"),
                ("Part D deductible, at most", "$615 Official, CMS, April 6, 2026", "$700 Official, CMS, April 6, 2026"),
            ],
        )

    def test_job_based_figures(self):
        items = {li["id"]: _text(li) for li in self._section("job-based").find_all("li")}
        self.assertEqual(
            items,
            {
                "job-premiums": "Average yearly premium in 2026: $9,804 for one person and $28,303 for a family, each 5% more than in 2025. (Survey, KFF, October 22, 2026)",
                "job-worker-share": "Workers' share of that in 2026: $1,672 a year for one person and $7,471 for a family, with the family share 9% more than in 2025. (Survey, KFF, October 22, 2026)",
                "job-2027": "For 2027: employers expect their health benefit cost per employee to rise 8.2% on average, even after changes they plan, such as higher deductibles. (Projected, Marsh, September 2, 2026)",
            },
        )

    def test_most_marketplace_enrollees_get_help_so_list_prices_arent_what_they_pay(self):
        self.assertEqual(
            _text(self._section("most-get-help")),
            "Most people with a marketplace plan get premium help. About 87% of people who picked a plan for 2026 got premium tax credits. (Official, CMS, March 2026)",
        )
        self.assertIn("a list-price increase usually isn't what you pay", _text(self._section("help-rises")))

    def test_says_the_big_jump_came_from_2025_to_2026(self):
        self.assertEqual(
            _text(self._section("big-jump")),
            "For many people, the big jump came from 2025 to 2026, when the extra premium help ended. For plans picked in open enrollment, "
            "the average premium after help went from $113 a month for 2025 to $178 for 2026, so the change from 2026 to 2027 understates "
            "what long-time enrollees have felt. (Official, CMS, May 2025 and March 2026)",
        )

    def test_the_labels_are_explained_before_the_numbers(self):
        labels = [_text(li.find("b")) for li in self._section("cc-labels").find_all("li")]
        self.assertEqual(labels, ["Official:", "Final:", "Proposed:", "Projected:", "Survey:"])
        page = str(self.soup)
        self.assertLess(page.index('id="cc-labels"'), page.index('id="marketplace"'))

    def test_every_source_in_a_label_is_named_in_sources_and_method(self):
        used = set()
        for name in FIGURE_SECTIONS:
            for label in _labels(self._section(name)):
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
        cls.page = _soup(cls.html).find("div", class_="fhi-page")

    def test_sources_are_linked(self):
        for href in (
            "https://www.kff.org/affordable-care-act/how-much-and-why-aca-marketplace-premiums-are-going-up-in-2027/",
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

    def test_outside_links_open_safely(self):
        links = self.page.find_all("a", href=re.compile(r"^https://"))
        self.assertTrue(links)
        for link in links:
            with self.subTest(href=link["href"]):
                self.assertEqual((link.get("target"), link.get("rel")), ("_blank", ["noopener", "noreferrer"]))

    def test_says_when_the_numbers_were_checked(self):
        self.assertIn('<p class="fhi-note" id="cc-checked">Numbers and links checked October 28, 2026.</p>', self.sources)

    def test_says_the_one_figure_that_is_our_math(self):
        self.assertIn("except the 87%, which is our math from CMS's counts", self.sources)
        self.assertIn("The 87% is the 20,035,756 people who got a premium tax credit out of the 23,130,860 who picked a plan.", self.sources)

    def test_cites_kff_without_copying_its_charts_and_leaves_out_acasignups(self):
        self.assertEqual(self.page.find_all(["img", "svg", "iframe", "canvas", "picture"]), [])
        self.assertNotIn("acasignups", self.html.lower())

    def test_no_em_or_en_dash(self):
        soup = _soup(self.html)
        copy = [str(self.page), _text(soup.find("title")), soup.find("meta", attrs={"name": "description"})["content"]]
        for text in copy:
            self.assertNotIn(chr(0x2014), text)
            self.assertNotIn(chr(0x2013), text)


class CoverageCostsPendingAndYourPriceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.soup = _soup(_page())

    def test_names_what_is_still_pending_and_when(self):
        items = {li["id"]: _text(li) for li in self.soup.find(id="pending").find_all("li")}
        self.assertEqual(
            items,
            {
                "pending-final-rates": "Final 2027 marketplace rates, by November 1, when open enrollment starts. Federal rules say they have to be public by then.",
                "pending-cms-files": "CMS's 2027 marketplace plan and price files, in early to mid November, as last year's were.",
                "pending-part-b": "The official 2027 Part B premium and deductible, in mid November. Last year's came out November 14.",
                "pending-paid": "What people pay for 2027 marketplace plans after premium help, from CMS's open enrollment files, around March 2027.",
            },
        )
        self.assertIn("We try to update this page as each one comes out.", _text(self.soup.find(id="pending")))

    def test_links_each_place_to_see_your_own_price(self):
        section = self.soup.find(id="your-price")
        links = {li["id"]: [a["href"] for a in li.find_all("a")] for li in section.find_all("li")}
        self.assertEqual(
            links,
            {
                "price-healthcare-gov": ["https://www.healthcare.gov/see-plans/"],
                "price-state": ["https://www.healthcare.gov/marketplace-in-your-state/"],
                "price-medicare": ["https://www.medicare.gov/plan-compare/"],
                "price-federal": [
                    "https://www.opm.gov/healthcare-insurance/healthcare/plan-information/compare-plans/",
                    "https://www.opm.gov/healthcare-insurance/healthcare/plan-information/premiums/",
                ],
                "price-job": ["https://www.healthcare.gov/health-care-law-protections/summary-of-benefits-and-coverage/"],
                "price-help": ["https://www.healthcare.gov/find-local-help/", reverse("state_help_index")],
            },
        )
        self.assertIn("ask HR for each plan's Summary of Benefits and Coverage", _text(section))

    def test_the_intro_points_to_your_own_price_and_the_coverage_guide(self):
        intro = self.soup.find(id="cc-intro")
        hrefs = [a["href"] for a in intro.find_all("a")]
        self.assertEqual(hrefs, ["#your-price", reverse("coverage-changes")])


class CoverageCostsIsLinkedTest(TestCase):
    """Preparing for 2027 and the open enrollment page both link here."""

    def setUp(self):
        self.href = f'href="{reverse("coverage-costs")}"'

    def test_the_coverage_guide_links_it_from_marketplace_premiums_and_more_help(self):
        html = Client().get(reverse("coverage-changes")).content.decode()
        watch = html[html.index('<section id="areas-to-watch"') : html.index("</section>", html.index('<section id="areas-to-watch"'))]
        self.assertIn(
            f"Final prices come out around November 1. <a {self.href}>What health coverage costs in 2027</a> has the numbers for each kind of coverage.",
            watch,
        )
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

"""Every step of the intake flow is bounded by the same width.

The report, 2026-09-21: the email box was a smidge wider than the name boxes,
the questions page was narrower than the pages before it, and Details was
wider. Measured on the real pages: four steps sat on Bootstrap's .container
at 1320px, Questions on .generic-main-content at 80% of the window, Details
on nothing at all because a :has(.scroll-x) rule in main.css outranked
.container-narrow, and the email box was 20px wider than the pair above it
because the pair's cells carried a side margin the email's did not.

The fix puts every step on .container-narrow. This walks the real flow in a
real browser and reads the numbers back, so a page that drifts onto another
wrapper fails here rather than in someone's screenshot.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)

DESKTOP = (1440, 900)
PHONE = (390, 844)
NARROW_PX = 760
# Fields that are short on purpose, so "fills its cell" is not asked of them.
SHORT_BY_DESIGN = ("form-input-state",)  # the two-letter state code

MEASURE_JS = """
const round = (n) => Math.round(n * 10) / 10;
const rect = (el) => el ? el.getBoundingClientRect() : null;
const bound = document.querySelector('.container-narrow');
const form = document.querySelector('#main-content form');
const email = document.querySelector('input#email');
const first = document.querySelector('input#store_fname');
const last = document.querySelector('input#store_lname');
const table = document.querySelector('.fhi-form-table');
// One entry per label/field row of the form table: where the label sits
// relative to its field, and how much of the row the field's cell has.
const rows = Array.from(document.querySelectorAll('.fhi-form-table tr')).map(tr => {
    const th = tr.querySelector('th');
    const td = tr.querySelector('td');
    if (!th || !td) { return null; }
    // Boxes and radios are not fill-width controls, so they are not asked to fill.
    const field = td.querySelector(
        'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea'
    );
    return {
        label: th.textContent.trim().slice(0, 30),
        stacked: rect(th).bottom <= rect(td).top + 1,
        tdWidth: round(rect(td).width),
        fieldClass: field ? field.className : null,
        fieldWidth: field ? round(rect(field).width) : null,
    };
}).filter(Boolean);
return {
    innerWidth: window.innerWidth,
    scrollsSideways: document.documentElement.scrollWidth > window.innerWidth + 1,
    bound: bound ? round(rect(bound).width) : null,
    form: form ? round(rect(form).width) : null,
    email: email ? round(rect(email).width) : null,
    pair: (first && last) ? round(rect(last).right - rect(first).left) : null,
    table: table ? round(rect(table).width) : null,
    rows: rows,
};
"""


class SeleniumTestFlowWidths(FHISeleniumBase, StaticLiveServerTestCase):
    """One walk through the intake flow, measured at two widths per step."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def _measure(self, step, results):
        """Measure a step at both widths and check it against the first step
        straight away, so a page that has drifted names itself rather than
        the walk running to the end and reporting all six at once."""
        for label, size in (("desktop", DESKTOP), ("phone", PHONE)):
            self.set_window_size(*size)
            self.wait_for_page_ready()
            # Read a layout property so the resize has settled before measuring.
            self.execute_script("return document.body.offsetWidth;")
            data = self.execute_script(MEASURE_JS)
            results[(step, label)] = data
        # Leave the browser wide so the next click is not off-screen.
        self.set_window_size(*DESKTOP)

        desktop = results[(step, "desktop")]
        assert desktop["bound"] is not None, f"{step} is not on .container-narrow"
        assert desktop["bound"] <= NARROW_PX, f"{step}: wrapper is {desktop['bound']}px"
        if self._reference_width is None:
            self._reference_width = desktop["bound"]
        assert desktop["bound"] == self._reference_width, (
            f"{step} is {desktop['bound']}px wide; the first step was "
            f"{self._reference_width}px"
        )
        assert desktop["form"] is None or desktop["form"] <= desktop["bound"] + 1, (
            f"{step}: the form ({desktop['form']}px) is wider than its wrapper "
            f"({desktop['bound']}px)"
        )
        assert not results[(step, "phone")][
            "scrollsSideways"
        ], f"{step} scrolls sideways at 390px"

    def _walk_the_flow(self):
        results = {}
        # The first step's wrapper width; every later step has to match it.
        self._reference_width = None
        self.set_window_size(*DESKTOP)
        self.open(f"{self.live_server_url}/scan")
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self._measure("upload", results)

        self.type("input#store_fname", "First NameTest")
        self.type("input#store_lname", "LastName")
        self.type("input#email", "farts@fart.com")
        self.type(
            "textarea#denial_text",
            """Dear First NameTest LastName;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp""",
        )
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        self.assert_title_eventually("Optional: Health History")
        self._measure("health history", results)
        self.click("button#next")

        self.assert_title_eventually("Optional: Add Plan Documents")
        self._measure("plan documents", results)
        self.click("button#next")

        self.assert_title_eventually("Analyzing Your Denial")
        self._measure("analyzing", results)
        self.click("button#entity-continue", timeout=90)

        self.assert_title_eventually("Categorize Your Denial")
        self._measure("details", results)
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "prep")
        self.type("input#id_diagnosis", "high risk homosexual behaviour")
        self.click("button#submit_cat")

        self.assert_title_eventually("Additional Resources & Questions")
        self._measure("questions", results)
        return results

    def test_every_step_holds_one_width_and_the_fields_fill_it(self):
        results = self._walk_the_flow()
        steps = sorted({step for step, _ in results})
        assert len(steps) == 6, steps

        # The email box is exactly as wide as the pair of name boxes above it.
        upload = results[("upload", "desktop")]
        assert upload["email"] is not None and upload["pair"] is not None, upload
        assert abs(upload["email"] - upload["pair"]) <= 1, (
            f"the email box is {upload['email']}px and the name pair spans "
            f"{upload['pair']}px"
        )

        # Details and Questions: on a phone the label sits above its field
        # and the field's cell has the whole row; on a desktop the two stay
        # side by side.
        for step in ("details", "questions"):
            phone = results[(step, "phone")]
            assert phone["table"] and phone["rows"], f"{step}: no form table measured"
            beside = [row["label"] for row in phone["rows"] if not row["stacked"]]
            assert beside == [], f"{step} at 390px: label still beside field: {beside}"
            narrow_cells = [
                (row["label"], row["tdWidth"])
                for row in phone["rows"]
                if row["tdWidth"] < 0.9 * phone["table"]
            ]
            assert narrow_cells == [], (
                f"{step} at 390px: these cells do not have the row "
                f"(table {phone['table']}px): {narrow_cells}"
            )
            narrow_fields = [
                (row["label"], row["fieldWidth"], row["tdWidth"])
                for row in phone["rows"]
                if row["fieldWidth"] is not None
                and row["fieldWidth"] < 0.9 * row["tdWidth"]
                and not any(cls in (row["fieldClass"] or "") for cls in SHORT_BY_DESIGN)
            ]
            assert (
                narrow_fields == []
            ), f"{step} at 390px: these fields do not fill their cell: {narrow_fields}"
            desktop = results[(step, "desktop")]
            stacked = [row["label"] for row in desktop["rows"] if row["stacked"]]
            assert stacked == [], f"{step} at 1440px: label above field: {stacked}"

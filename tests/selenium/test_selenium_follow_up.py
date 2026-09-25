"""Use SeleniumBase to test Submitting an appeal"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import *
from seleniumbase import BaseCase

BaseCase.main(__name__, __file__)

PHONE = (390, 844)
DESKTOP = (1440, 900)

# Where each label of a form table sits against its field, whether the
# page as a whole scrolls sideways, and whether the form's own .scroll-x box
# does: a field wider than the phone scrolls the box rather than the page,
# so the page alone would not show it. Measured against the document's own
# client width rather than window.innerWidth, which includes a scrollbar
# gutter that would hide an overflow of a few pixels.
PHONE_JS = """
const rows = Array.from(document.querySelectorAll('table.fhi-form-table tr')).map(tr => {
    const th = tr.querySelector('th');
    const td = tr.querySelector('td');
    if (!th || !td) { return null; }
    return {
        label: th.textContent.trim().slice(0, 40),
        stacked: th.getBoundingClientRect().bottom <= td.getBoundingClientRect().top + 1,
    };
}).filter(Boolean);
const box = document.querySelector('table.fhi-form-table').closest('.scroll-x');
return {
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    boxOverflow: box ? box.scrollWidth - box.clientWidth : null,
    rows: rows,
};
"""


class SeleniumFollowUp(BaseCase, StaticLiveServerTestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def setUp(self):
        # Prevent Chrome from routing localhost through an HTTP proxy,
        # matching the pattern in FHISeleniumBase.
        import seleniumbase.config as sb_config

        existing = getattr(sb_config, "chromium_arg", None)
        if not existing:
            sb_config.chromium_arg = "--no-proxy-server"
        elif "--no-proxy-server" not in existing:
            sb_config.chromium_arg = existing + ";--no-proxy-server"
        super().setUp()

    def _measure_on_a_phone(self):
        """The open page at 390px, with the window put back to desktop size
        before anything is asserted, so a failure cannot leave the next
        step on a phone."""
        self.set_window_size(*PHONE)
        self.wait_for_ready_state_complete()
        # Read a layout property so the resize has settled before measuring.
        self.execute_script("return document.body.offsetWidth;")
        data = self.execute_script(PHONE_JS)
        self.set_window_size(*DESKTOP)
        return data

    def _assert_stacks_on_a_phone(self, page, data):
        assert data["rows"], f"{page}: no .fhi-form-table rows to measure"
        assert data["scrollWidth"] <= data["clientWidth"], (
            f"{page} scrolls sideways at 390px: {data['scrollWidth']}px of "
            f"content in {data['clientWidth']}px"
        )
        beside = [row["label"] for row in data["rows"] if not row["stacked"]]
        assert beside == [], f"{page} at 390px: label still beside field: {beside}"
        assert data["boxOverflow"] is not None, f"{page}: the form has no .scroll-x"
        assert data["boxOverflow"] <= 1, (
            f"{page} at 390px: a field runs {data['boxOverflow']}px past the "
            "form's box, which scrolls sideways to reach it"
        )

    def test_follow_up_forms_stack_on_a_phone(self):
        """Both follow-up forms are Django as_table forms, a label cell
        beside a field cell. On a phone each label sits above its field, and
        neither the page nor the form's own box scrolls sideways."""
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        fax = FaxesToSend.objects.create(
            hashed_email=hashed_email,
            paid=True,
            email=email,
            name="Timbit",
            appeal_text="Please cover it.",
            denial_id=denial,
            destination=None,
        )
        self.set_window_size(*DESKTOP)

        followup = f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.follow_up_semi_sekret}"
        self.open(f"{self.live_server_url}/{followup}")
        self.assert_title("Follow Up On Your Health Insurance Appeal")
        self._assert_stacks_on_a_phone("followup", self._measure_on_a_phone())

        self.open(
            f"{self.live_server_url}/v0/faxfollowup/{fax.uuid}/{fax.hashed_email}"
        )
        self.assert_title("Fax ReSend")
        self._assert_stacks_on_a_phone("faxfollowup", self._measure_on_a_phone())

    def test_follow_up_page_loads(self):
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        mylink = f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.follow_up_semi_sekret}"
        self.open(f"{self.live_server_url}/{mylink}")
        self.assert_title("Follow Up On Your Health Insurance Appeal")
        self.type("textarea#id_user_comments", "Words Words Words")
        self.click("button#submit")
        self.assert_title("Thank you!")
        # Make sure we don't add a new follow up without opting into a 2nd follow up
        follow_up_count = FollowUpSched.objects.filter(email=email).count()
        assert follow_up_count == 0

    def test_follow_up_again(self):
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        mylink = f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.follow_up_semi_sekret}"
        self.open(f"{self.live_server_url}/{mylink}")
        self.assert_title("Follow Up On Your Health Insurance Appeal")
        self.type("textarea#id_user_comments", "Words Words Words")
        self.click("input#id_follow_up_again")
        self.click("button#submit")
        self.assert_title("Thank you!")
        # Make sure we add follow ups for the next round (1-day, 7-day, 30-day, 90-day)
        follow_up_count = FollowUpSched.objects.filter(email=email).count()
        assert follow_up_count == 4

    def test_follow_up_page_loads_fails(self):
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        mylink = (
            f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.hashed_email}"
        )
        self.open(f"{self.live_server_url}/{mylink}")
        # A stale/mangled follow-up link is a 404 (these live in emails for
        # months), not a 500 -- see FollowUpView + fetch_denial.
        self.assert_title("Page Not Found - Fight Health Insurance")

    def test_follow_up_trailing_slash_link(self):
        """Email clients sometimes add a trailing slash; the page must load.
        Regression test for a URL-pattern typo that crashed the view."""
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        mylink = (
            f"v0/followup/{denial.uuid}/{denial.hashed_email}/"
            f"{denial.follow_up_semi_sekret}/"
        )
        self.open(f"{self.live_server_url}/{mylink}")
        self.assert_title("Follow Up On Your Health Insurance Appeal")
        self.click("button#submit")
        self.assert_title("Thank you!")

    def test_follow_up_persists_comments_and_appeal_result(self):
        """Submitting comments + appeal_result must be saved on FollowUp."""
        email = "timbit@test.com"
        hashed_email = Denial.get_hashed_email(email)
        denial = Denial.objects.create(
            denial_text="I am evil so no health care for you.",
            hashed_email=hashed_email,
            use_external=False,
            raw_email=email,
            health_history="",
        )
        mylink = f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.follow_up_semi_sekret}"
        self.open(f"{self.live_server_url}/{mylink}")
        self.assert_title("Follow Up On Your Health Insurance Appeal")
        self.type("textarea#id_user_comments", "Insurer reversed the denial.")
        self.select_option_by_value("select#id_appeal_result", "Yes")
        self.click("button#submit")
        self.assert_title("Thank you!")
        followup = FollowUp.objects.get(denial_id=denial)
        assert followup.user_comments == "Insurer reversed the denial."
        assert followup.appeal_result == "Yes"
        denial.refresh_from_db()
        assert denial.appeal_result == "Yes"

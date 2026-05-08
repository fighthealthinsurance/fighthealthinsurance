"""Use SeleniumBase to test Submitting an appeal"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import *
from seleniumbase import BaseCase

BaseCase.main(__name__, __file__)


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
        self.assert_title("Something Went Wrong - Fight Health Insurance")

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

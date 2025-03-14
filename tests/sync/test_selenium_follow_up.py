"""Use SeleniumBase to test Submitting an appeal"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import Denial, FollowUpSched
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
        # Make sure we add a 2nd follow up
        follow_up_count = FollowUpSched.objects.filter(email=email).count()
        assert follow_up_count == 1

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
        mylink = f"v0/followup/{denial.uuid}/{denial.hashed_email}/{denial.hashed_email}"
        self.open(f"{self.live_server_url}/{mylink}")
        self.assert_title("Server Error (500)")

"""Use SeleniumBase to test Submitting an appeal"""

import hashlib
import os
import time
import sys

import pytest
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
        self.assert_title("Server Error (500)")

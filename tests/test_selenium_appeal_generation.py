"""Use SeleniumBase to test Submitting an appeal"""
import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase
BaseCase.main(__name__, __file__)


class SeleniumTestAppealGeneration(BaseCase, StaticLiveServerTestCase):

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()


    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()


    @pytest.mark.expected_failure
    def test_submit_an_appeal_with_missing_info(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        # Should fail
        self.assert_element("div#pii_error")

    def test_submit_an_appeal_with_missing_info(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.click("button#submit")
        self.assert_element("div#pii_error")

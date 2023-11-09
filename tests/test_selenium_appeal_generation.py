"""Use SeleniumBase to test Submitting an appeal"""
import hashlib
import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase
import time
BaseCase.main(__name__, __file__)


class SeleniumTestAppealGeneration(BaseCase, StaticLiveServerTestCase):

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()


    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()


    def test_submit_an_appeal_with_missing_info_and_fail(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        # pii error should not be present (we have not clicked submit)
        with pytest.raises(Exception) as ex:
            self.assert_element("div#pii_error")

    def test_submit_an_appeal_with_missing_info(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.click("button#submit")
        # Now we should not have changed pages and pii error should show up
        self.assert_element("div#pii_error")
        self.assert_title("Upload your Health Insurance Denial")

    def test_submit_an_appeal_with_enough(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.type("input#store_lname", "LastName")
        self.type("input#email", "farts@fart.com")
        self.type("textarea#denial_text",
                  """Dear First NameTest LastName;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp""")
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")
        self.assert_title("Categorize your denial (so we can generate the right kind of appeal)")
        self.click("input#submit_cat")
        self.assert_title("Updating denial with your feedback & checking for resources")

    def test_submit_an_appeal_with_enough_then_delete(self):
        email = "farts@farts.com"
        self.open(f"{self.live_server_url}/")
        self.assert_title("Fight Your Health Insurance Denial")
        self.click('a[id="scanlink"]')
        self.assert_title("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.type("input#store_lname", "LastName")
        self.type("input#email", email)
        self.type("textarea#denial_text",
                  """Dear First NameTest LastName;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp""")
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")
        self.assert_title("Categorize your denial (so we can generate the right kind of appeal)")
        self.click("input#submit_cat")
        self.assert_title("Updating denial with your feedback & checking for resources")
        # Assert we have some data
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email).count()
        assert(denials_for_user_count > 0)
        self.click('a[id="removedata"]')
        self.assert_title("Delete your Data")
        self.type("input#email", email)
        self.click("button#submit")
        self.assert_title("Deleted")
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email).count()
        assert(denials_for_user_count == 0)

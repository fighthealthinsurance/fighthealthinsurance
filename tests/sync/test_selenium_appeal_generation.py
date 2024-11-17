"""Use SeleniumBase to test Submitting an appeal"""

import hashlib
import os
import time
import sys

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import *
from seleniumbase import BaseCase
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

    def assert_title_eventually(self, desired_title):
        try:
            WebDriverWait(self.driver, 15).until(EC.title_is(desired_title))
        except Exception:
            # On failure assert_title gives us a better error message than the timeout.
            self.assert_title(desired_title)

    def test_submit_an_appeal_with_missing_info_and_fail(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        # pii error should not be present (we have not clicked submit)
        with pytest.raises(Exception) as ex:
            self.assert_element("div#pii_error")

    def test_submit_an_appeal_with_missing_info(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.click("button#submit")
        # Now we should not have changed pages and pii error should show up
        self.assert_element("div#pii_error")
        self.assert_title_eventually("Upload your Health Insurance Denial")

    def test_server_side_ocr_workflow(self):
        self.open(f"{self.live_server_url}/server_side_ocr")
        self.assert_title_eventually(
            "Upload your Health Insurance Denial - Server Side Processing"
        )
        file_input = self.find_element("input#uploader")
        pathname = None
        path_to_image = None
        try:
            pathname = os.path.dirname(os.path.realpath(__file__))
            path_to_image = os.path.join(pathname, "sample_ocr_image.png")
        except:
            pathname = os.path.dirname(sys.argv[0])
            path_to_image = os.path.join(
                pathname, "../../../../tests/sample_ocr_image.png"
            )
        file_input.send_keys(path_to_image)
        self.click("button#submit")
        time.sleep(5)  # wait for OCR process to complete
        self.assert_text(
            """UnidentifiedImageError""",
            "textarea#denial_text",
        )

    def test_submit_an_appeal_with_enough_and_fax(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
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
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.click("button#submit")
        self.assert_title_eventually("Categorize Your Denial")
        self.type("input#id_procedure", "prep")
        self.type("input#id_diagnosis", "high risk homosexual behaviour")
        self.click("input#submit_cat")
        self.assert_title_eventually("Updating Denial")
        self.type("input#id_medical_reason", "FakeReason")
        self.click("input#submit")
        self.assert_title_eventually("Fight Your Health Insurance Denial: Choose an Appeal")
        # It takes time for the appeals to populate
        time.sleep(11)
        self.click("button#submit1")
        self.type("input#id_name", "Testy McTestFace")
        self.type("input#id_fax_phone", "425555555")
        self.type("input#id_insurance_company", "EvilCo")
        self.click("button#fax_appeal")
        # Make sure we get to stripe checkout
        time.sleep(1)
        if (
            "STRIPE_TEST_SECRET_KEY" in os.environ
            and "NOSTRIPE" not in os.environ
            and "MEEPS" in os.environ
        ):
            self.assertIn(
                "stripe",
                self.driver.current_url,
                f"Should be redirected to stripe f{self.driver.get_current_url}",
            )

    def test_submit_an_appeal_with_enough(self):
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
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
        self.assert_title_eventually("Categorize Your Denial")
        self.click("input#submit_cat")
        self.assert_title_eventually("Updating Denial")

    def test_submit_an_appeal_with_enough_then_delete(self):
        email = "farts@farts.com"
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.type("input#store_fname", "First NameTest")
        self.type("input#store_lname", "LastName")
        self.type("input#email", email)
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
        self.assert_title_eventually("Categorize Your Denial")
        self.click("input#submit_cat")
        self.assert_title_eventually("Updating Denial")
        # Assert we have some data
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count > 0
        self.click('a[id="removedata"]')
        self.assert_title_eventually("Delete Your Data")
        self.type("input#id_email", email)
        self.click("button#submit")
        self.assert_title_eventually("Deleted Your Data")
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count == 0

"""Use SeleniumBase to test Submitting an appeal"""

import hashlib
import os
import time
import sys

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import *
from seleniumbase import BaseCase

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestAppealGeneration(FHISeleniumBase, StaticLiveServerTestCase):
    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    test_username = "testuser"
    test_password = "testpassword123"

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
        self.assert_text_eventually_contains(
            """UnidentifiedImageError""",
            "denial_text",
        )

    def test_submit_an_appeal_with_enough_and_fax(self):
        assert DenialTypes.objects.filter(name="Medically Necessary").count() > 0
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
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")
        # This is because channels is needs a different base to work and it's hanging so we manually
        # select the denial type for now.
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "prep")
        self.type("input#id_diagnosis", "high risk homosexual behaviour")
        self.click("button#submit_cat")
        self.assert_title_eventually("Some additional questions")
        self.type("input#id_medical_reason", "FakeReason")
        self.click("input#submit")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )
        return  # The rest of this code depends on channels, which is being difficult
        # See https://channels.readthedocs.io/en/latest/tutorial/part_4.html
        self.click_button_eventually("submit1")
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
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")
        self.click("button#submit_cat")
        self.assert_title_eventually("Some additional questions")

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
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")
        self.click("button#submit_cat")
        self.assert_title_eventually("Some additional questions")
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

    def test_back_navigation_preserves_form_data_correctly(self):
        """Test that localStorage persistence returns parsed values, not raw JSON"""
        test_fname = "BackTestFirst"
        test_lname = "BackTestLast"
        test_email = "backtest@test.com"
        test_denial = """Dear BackTestFirst BackTestLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""

        # Fill out the scrub form
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.type("input#store_fname", test_fname)
        self.type("input#store_lname", test_lname)
        self.type("input#email", test_email)
        self.type("textarea#denial_text", test_denial)
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # Move to next page
        self.assert_title_eventually("Optional: Health History")

        # Navigate back using browser back button
        self.driver.back()
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Verify the values are correctly restored (not as JSON strings)
        fname_value = self.get_value("input#store_fname")
        lname_value = self.get_value("input#store_lname")
        email_value = self.get_value("input#email")

        # These should be the actual values, not JSON like {"value":"...","expiry":...}
        assert fname_value == test_fname, f"Expected '{test_fname}', got '{fname_value}'"
        assert lname_value == test_lname, f"Expected '{test_lname}', got '{lname_value}'"
        assert email_value == test_email, f"Expected '{test_email}', got '{email_value}'"

        # Verify no JSON-like strings are present
        assert "{" not in fname_value, f"fname contains JSON: {fname_value}"
        assert "expiry" not in fname_value, f"fname contains expiry: {fname_value}"
        assert "{" not in lname_value, f"lname contains JSON: {lname_value}"
        assert "{" not in email_value, f"email contains JSON: {email_value}"

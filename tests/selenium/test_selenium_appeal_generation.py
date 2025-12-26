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
        import tempfile
        from PIL import Image

        self.open(f"{self.live_server_url}/server_side_ocr")
        self.assert_title_eventually(
            "Upload your Health Insurance Denial - Server Side Processing"
        )
        file_input = self.find_element("input#uploader")

        # Create a simple test image dynamically
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
            # Create a simple image with some text-like content
            img = Image.new("RGB", (200, 100), color="white")
            img.save(tmp.name)
            path_to_image = tmp.name

            file_input.send_keys(path_to_image)
            self.click("button#submit")
            # The OCR should process the image (even if it doesn't find text)
            # Give it time to process
            time.sleep(2)
            # After successful OCR, the view redirects to scrub.html (main denial form)
            self.assert_title_eventually("Upload your Health Insurance Denial")

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
        self.assert_title_eventually("Additional Resources & Questions")
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
        self.assert_title_eventually("Additional Resources & Questions")

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
        self.assert_title_eventually("Additional Resources & Questions")
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
        assert (
            fname_value == test_fname
        ), f"Expected '{test_fname}', got '{fname_value}'"
        assert (
            lname_value == test_lname
        ), f"Expected '{test_lname}', got '{lname_value}'"
        assert (
            email_value == test_email
        ), f"Expected '{test_email}', got '{email_value}'"

        # Verify no JSON-like strings are present
        assert "{" not in fname_value, f"fname contains JSON: {fname_value}"
        assert "expiry" not in fname_value, f"fname contains expiry: {fname_value}"
        assert "{" not in lname_value, f"lname contains JSON: {lname_value}"
        assert "{" not in email_value, f"email contains JSON: {email_value}"

    def test_back_button_link_on_health_history(self):
        """
        Test that the back button link on health history page works correctly.
        """
        test_fname = "BackBtnFirst"
        test_lname = "BackBtnLast"
        test_email = "backbtn@test.com"
        test_denial = """Dear BackBtnFirst BackBtnLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""

        # Fill out scan form and submit
        self.open(f"{self.live_server_url}/")
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

        # Health History page - should have back button to scan
        self.assert_title_eventually("Optional: Health History")
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be back at Scan page
        self.assert_title_eventually("Upload your Health Insurance Denial")
        self.assert_element("textarea#denial_text")
        # Verify form values preserved via localStorage
        assert self.get_value("input#store_fname") == test_fname
        assert self.get_value("input#store_lname") == test_lname
        assert self.get_value("input#email") == test_email

    def test_back_button_link_on_plan_documents_and_forward_again(self):
        """
        Test that the back button link on plan documents page works correctly,
        AND that we can then go forward again without losing form data.
        This tests the denial_id/email/semi_sekret URL params work correctly.
        """
        test_fname = "BackBtnPlanFirst"
        test_lname = "BackBtnPlanLast"
        test_email = "backbtnplan@test.com"
        test_denial = """Dear BackBtnPlanFirst BackBtnPlanLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""
        test_health_history = "Test health history for back button test."

        # Fill out scan form and submit
        self.open(f"{self.live_server_url}/")
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

        # Health History page
        self.assert_title_eventually("Optional: Health History")
        self.type("textarea#health_history", test_health_history)
        self.click("button#next")

        # Plan Documents page - should have back button to health history
        self.assert_title_eventually("Optional: Add Plan Documents")
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be back at Health History page
        self.assert_title_eventually("Optional: Health History")
        self.assert_element("textarea#health_history")
        # Wait for JavaScript to restore value from localStorage
        time.sleep(0.5)

        # Verify health history preserved via localStorage
        health_history_value = self.get_value("textarea#health_history")
        assert (
            health_history_value == test_health_history
        ), f"Expected '{test_health_history}', got '{health_history_value}'"

        # NOW TEST GOING FORWARD AGAIN - this is the key test for the fix
        # The form should have hidden fields populated from URL params
        self.click("button#next")

        # Should successfully navigate to Plan Documents page without error
        self.assert_title_eventually("Optional: Add Plan Documents")
        # Verify we're on the right page and form works
        self.assert_element("button#next")

        # Go forward to Entity Extract
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

        # Go back again to plan documents (back from entity_extract now goes to plan docs)
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be at Plan Documents (back button from entity_extract goes to dvc)
        self.assert_title_eventually("Optional: Add Plan Documents")

        # And forward again should still work
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

    def test_session_scoped_localstorage_does_not_mix_appeals(self):
        """
        Test that localStorage is scoped to session, so starting a new appeal
        does not restore data from a previous appeal.
        """
        # First appeal
        first_email = "first_appeal@test.com"
        first_health = "First appeal health history"

        self.open(f"{self.live_server_url}/")
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        self.type("input#store_fname", "FirstAppeal")
        self.type("input#store_lname", "User")
        self.type("input#email", first_email)
        self.type(
            "textarea#denial_text",
            """Dear FirstAppeal User;
Your claim for Truvada has been denied.
Sincerely, InsuranceCo""",
        )
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        self.assert_title_eventually("Optional: Health History")
        self.type("textarea#health_history", first_health)

        # Now start a SECOND appeal (new session)
        self.open(f"{self.live_server_url}/")
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        second_email = "second_appeal@test.com"
        self.type("input#store_fname", "SecondAppeal")
        self.type("input#store_lname", "Person")
        self.type("input#email", second_email)
        self.type(
            "textarea#denial_text",
            """Dear SecondAppeal Person;
Your claim for different treatment has been denied.
Sincerely, OtherInsuranceCo""",
        )
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # On health history page for second appeal
        self.assert_title_eventually("Optional: Health History")
        time.sleep(0.5)  # Wait for JS

        # Health history should NOT have the first appeal's data
        health_value = self.get_value("textarea#health_history")
        assert (
            health_value != first_health
        ), f"Second appeal should not restore first appeal's health history. Got: '{health_value}'"

    def test_meta_tags_present_for_form_persistence(self):
        """
        Test that the meta tags for form persistence are rendered in the page.
        """
        self.open(f"{self.live_server_url}/")
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Fill in and submit to get a session
        self.type("input#store_fname", "MetaTagTest")
        self.type("input#store_lname", "User")
        self.type("input#email", "metatag@test.com")
        self.type(
            "textarea#denial_text",
            """Dear MetaTagTest User;
Your claim has been denied.
Sincerely, InsuranceCo""",
        )
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # On health history page - should have meta tags
        self.assert_title_eventually("Optional: Health History")

        # Check for request method meta tag (should be POST since we came from form submission)
        # Use wait_for_element_present since meta tags aren't visible
        self.wait_for_element_present('meta[name="fhi-request-method"]')
        method_content = self.get_attribute(
            'meta[name="fhi-request-method"]', "content"
        )
        assert (
            method_content == "POST"
        ), f"Request method should be POST, got {method_content}"

        # Session key meta tag should be present after form submission
        self.wait_for_element_present('meta[name="fhi-session-key"]')
        session_content = self.get_attribute('meta[name="fhi-session-key"]', "content")
        assert (
            len(session_content) > 0
        ), "Session key should not be empty after form submission"

    def test_back_button_navigates_with_get_request(self):
        """
        Test that clicking the back button link results in a GET request,
        which should restore localStorage values.
        """
        test_health = "Health history to restore"

        self.open(f"{self.live_server_url}/")
        self.click('a[id="scanlink"]')
        self.type("input#store_fname", "BackGetTest")
        self.type("input#store_lname", "User")
        self.type("input#email", "backget@test.com")
        self.type(
            "textarea#denial_text",
            """Dear BackGetTest User;
Your claim has been denied.
Sincerely, InsuranceCo""",
        )
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # On health history page
        self.assert_title_eventually("Optional: Health History")
        self.type("textarea#health_history", test_health)
        self.click("button#next")

        # On plan documents page
        self.assert_title_eventually("Optional: Add Plan Documents")

        # Click back button link (should be a GET request)
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Back at health history page via GET
        self.assert_title_eventually("Optional: Health History")

        # Check meta tag - should be GET (from link navigation)
        # Use wait_for_element_present since meta tags aren't visible
        self.wait_for_element_present('meta[name="fhi-request-method"]')
        method_content = self.get_attribute(
            'meta[name="fhi-request-method"]', "content"
        )
        assert (
            method_content == "GET"
        ), f"Back button should result in GET, got {method_content}"

        # Health history should be restored from localStorage
        time.sleep(0.5)
        health_value = self.get_value("textarea#health_history")
        assert (
            health_value == test_health
        ), f"Health history should be restored on GET. Expected '{test_health}', got '{health_value}'"

    def test_back_navigation_flow_from_categorize_to_documents(self):
        """
        Test that the back button from Categorize (Entity Extract) goes to
        Plan Documents (not Health History), since processing is a system step.
        """
        test_fname = "BackCatFirst"
        test_lname = "BackCatLast"
        test_email = "backcat@test.com"
        test_denial = """Dear BackCatFirst BackCatLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""

        # Go through the flow to reach Categorize
        self.open(f"{self.live_server_url}/")
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

        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")

        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")

        # Now at Categorize page
        self.assert_title_eventually("Categorize Your Denial")

        # Click back button - should go to Plan Documents (not Health History)
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be at Plan Documents page
        self.assert_title_eventually("Optional: Add Plan Documents")

        # Go forward again - should work without errors
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

    def test_back_navigation_flow_from_questions_to_categorize(self):
        """
        Test that the back button from Questions (Find Next Steps) goes to
        Categorize Review page.
        """
        test_fname = "BackQuestFirst"
        test_lname = "BackQuestLast"
        test_email = "backquest@test.com"
        test_denial = """Dear BackQuestFirst BackQuestLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""

        # Go through the flow to reach Questions
        self.open(f"{self.live_server_url}/")
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

        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")

        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")

        self.assert_title_eventually("Categorize Your Denial")
        # Manually select denial type since channels is not working in test
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "prep")
        self.type("input#id_diagnosis", "high risk homosexual behaviour")
        self.click("button#submit_cat")

        # Now at Questions page
        self.assert_title_eventually("Additional Resources & Questions")

        # Click back button - should go to Categorize Review
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be at Categorize Review page (shows the categorization)
        self.assert_title_eventually("Categorize Your Denial")

        # Go forward again - should work without errors
        self.click("button#submit_cat")
        self.assert_title_eventually("Additional Resources & Questions")

    def test_back_navigation_flow_from_generate_to_questions(self):
        """
        Test that the back button from Generate Appeal goes to Questions page.
        """
        test_fname = "BackGenFirst"
        test_lname = "BackGenLast"
        test_email = "backgen@test.com"
        test_denial = """Dear BackGenFirst BackGenLast;
Your claim for Truvada has been denied as not medically necessary.

Sincerely,
Cheap-O-Insurance-Corp"""

        # Go through the flow to reach Generate Appeal
        self.open(f"{self.live_server_url}/")
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

        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")

        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")

        self.assert_title_eventually("Categorize Your Denial")
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "prep")
        self.type("input#id_diagnosis", "high risk homosexual behaviour")
        self.click("button#submit_cat")

        self.assert_title_eventually("Additional Resources & Questions")
        self.type("input#id_medical_reason", "FakeReason")
        self.click("input#submit")

        # Now at Generate Appeal page
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

        # Click back button - should go to Questions page
        # Use js_click to avoid element click interception from overlays
        self.js_click("a[class*='btn-secondary']")

        # Should be at Questions page
        self.assert_title_eventually("Additional Resources & Questions")

        # Go forward again - should work without errors
        self.click("input#submit")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

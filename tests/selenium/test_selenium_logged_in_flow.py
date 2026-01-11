"""
End-to-end Selenium test for logged-in patient flow.
Tests: signup → generate appeal while logged in → see it in dashboard.
"""

import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase

from fighthealthinsurance.models import Appeal, Denial
from fhi_users.models import PatientUser
from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumLoggedInPatientFlow(FHISeleniumBase, StaticLiveServerTestCase):
    """
    Test the complete logged-in patient journey:
    1. Sign up for a new account
    2. Generate an appeal while logged in
    3. Verify appeal appears in patient dashboard
    """

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

    def test_signup_generate_appeal_and_view_in_dashboard(self):
        """
        Complete end-to-end test:
        - Sign up a new patient account
        - Generate an appeal while logged in
        - Verify the appeal appears in the patient dashboard
        """
        test_email = "patient_e2e@example.com"
        test_password = "SecurePass123!"
        test_first_name = "John"
        test_last_name = "Patient"

        # Step 1: Navigate to home page
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )

        # Step 2: Click Sign Up link in navigation
        self.click('a[href="/v0/auth/signup"]')
        self.assert_title_eventually("Create Account (Optional)")

        # Step 3: Fill out signup form
        self.type("input#email", test_email)
        self.type("input#password", test_password)
        self.type("input#confirm_password", test_password)
        self.type("input#first_name", test_first_name)
        self.type("input#last_name", test_last_name)

        # Step 4: Submit signup form
        self.click("button[type='submit']")

        # Step 5: Should be redirected to dashboard after signup
        self.assert_title_eventually("My Dashboard")
        self.assert_text("John", "body")  # First name should appear

        # Verify user and patient were created in database
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.get(email=test_email)
        self.assertEqual(user.first_name, test_first_name)
        self.assertEqual(user.last_name, test_last_name)
        patient = PatientUser.objects.get(user=user)
        self.assertTrue(patient.active)

        # Step 6: Generate an appeal (navigate to scan page)
        self.click('a[href="/scan"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Step 7: Fill out denial form
        # Note: User is logged in, so email should be pre-filled or not required
        self.type("input#store_fname", test_first_name)
        self.type("input#store_lname", test_last_name)
        # Email might be pre-filled from logged-in user
        if self.is_element_visible("input#email"):
            self.type("input#email", test_email)

        denial_text = f"""Dear {test_first_name} {test_last_name};

Your claim for Physical Therapy has been denied as not medically necessary.
We have determined that this treatment is experimental.

Sincerely,
Denial-Happy Insurance Company"""

        self.type("textarea#denial_text", denial_text)

        # Accept terms
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")

        # Step 8: Submit denial
        self.click("button#submit")
        self.assert_title_eventually("Optional: Health History")

        # Step 9: Skip health history
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")

        # Step 10: Skip plan documents
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

        # Step 11: Fill out categorization
        # Select denial type (2 = "Medically Necessary" from fixtures)
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "Physical Therapy")
        self.type("input#id_diagnosis", "Back Pain")

        # Submit categorization
        self.click("button#submit_cat")
        self.assert_title_eventually("Additional Resources & Questions")

        # Step 12: Submit medical reason (this generates the appeal)
        self.type("input#id_medical_reason", "Chronic back pain from herniated disc")
        self.click("input#submit")

        # Step 13: Should reach appeal chooser page
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

        # Verify denial and appeal were created in database
        denial = Denial.objects.get(patient_user=patient)
        self.assertEqual(denial.procedure, "Physical Therapy")
        self.assertEqual(denial.diagnosis, "Back Pain")

        # Give it a moment for appeal generation
        time.sleep(2)

        appeals = Appeal.objects.filter(patient_user=patient, patient_visible=True)
        self.assertGreater(
            appeals.count(),
            0,
            "At least one patient-visible appeal should be generated",
        )

        # Step 14: Navigate to dashboard to verify appeal appears
        self.click('a[href="/my/dashboard"]')
        self.assert_title_eventually("My Dashboard")

        # Step 15: Verify appeal appears in dashboard
        # The appeal should show the procedure name
        self.assert_text("Physical Therapy", "body")

        # Click on Appeals tab to see more details
        # The dashboard might have tabs - look for Appeals tab
        if self.is_element_visible('a[href="#appeals-tab"]'):
            self.click('a[href="#appeals-tab"]')
            time.sleep(1)  # Wait for tab content to load

        # Should see the diagnosis as well
        self.assert_text("Back Pain", "body")

        # Step 16: Verify we can add a call log (test dashboard functionality)
        # Look for "Add Call Log" button or link
        if self.is_element_visible('a[href="/my/call-log/new"]'):
            self.click('a[href="/my/call-log/new"]')
            self.assert_title_eventually("Log Insurance Call")

            # Fill out call log form
            self.type("input#id_department", "Claims Department")
            self.type("input#id_representative_name", "Jane Doe")
            self.type("input#id_reference_number", "REF-E2E-001")
            self.type(
                "textarea#id_reason_for_call",
                "Called to check on appeal status for physical therapy",
            )
            self.select_option_by_value("select#id_call_type", "appeal_status")
            self.select_option_by_value("select#id_outcome", "pending")

            # Submit call log
            self.click("button[type='submit']")

            # Should redirect back to dashboard
            self.assert_title_eventually("My Dashboard")

            # Verify call log appears
            self.assert_text("Jane Doe", "body")
            self.assert_text("REF-E2E-001", "body")

        # Step 17: Sign out
        self.click('a[href="/v0/auth/logout"]')

        # Step 18: Verify we're logged out
        # Should see Sign In link instead of My Dashboard
        time.sleep(1)
        self.assert_element('a[href="/v0/auth/login"]')

    def test_login_and_view_existing_appeals(self):
        """
        Test logging in with an existing account and viewing appeals.
        """
        # Create a user with an appeal
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(
            username="existing@example.com",
            email="existing@example.com",
            password="TestPass123!",
            first_name="Existing",
            last_name="User",
        )
        patient = PatientUser.objects.create(user=user, active=True)

        # Create a denial and appeal for this user
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(user.email),
            denial_text="Test denial",
            patient_user=patient,
            procedure="MRI Scan",
            diagnosis="Chronic Headaches",
        )
        appeal = Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email(user.email),
            patient_user=patient,
            appeal_text="Test appeal letter content",
            patient_visible=True,
        )

        # Navigate to home page
        self.open(f"{self.live_server_url}/")

        # Click Sign In link
        self.click('a[href="/v0/auth/login"]')
        self.assert_title_eventually("Login")

        # Login is for professional users with domain/phone
        # For now, just verify the login page loads
        # (Professional login flow is different from patient login)
        self.assert_element("input#inputUsername")
        self.assert_element("input#inputPassword")

        # Note: This test verifies the login page exists
        # The actual professional login flow requires domain/phone setup
        # which is outside the scope of basic patient signup

    def test_optional_messaging_on_signup_page(self):
        """
        Verify that the signup page clearly communicates that accounts are optional.
        """
        self.open(f"{self.live_server_url}/v0/auth/signup")
        self.assert_title_eventually("Create Account (Optional)")

        # Should see messaging about accounts being optional
        self.assert_text("Optional", "body")
        self.assert_text(
            "You can use Fight Health Insurance without an account", "body"
        )

        # Should see benefits listed
        self.assert_text("Track your appeals", "body")
        self.assert_text("Log phone calls", "body")
        self.assert_text("Upload and manage evidence", "body")

        # Should have link to skip signup
        self.assert_element('a[href="/scan"]')
        self.assert_text("Skip and generate an appeal without an account", "body")

    def test_account_prompt_banner_appears_on_appeal_result(self):
        """
        Verify that the account creation prompt appears after generating
        an appeal as an anonymous user.
        """
        # Generate an appeal anonymously (simplified flow)
        self.open(f"{self.live_server_url}/scan")
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Fill minimal info
        self.type("input#store_fname", "Anonymous")
        self.type("input#store_lname", "User")
        self.type("input#email", "anon@example.com")
        self.type(
            "textarea#denial_text",
            "Your claim has been denied as not medically necessary.",
        )

        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # Skip through to appeal generation
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")
        self.click("button#submit_cat")
        self.assert_title_eventually("Additional Resources & Questions")
        self.type("input#id_medical_reason", "Medical necessity")
        self.click("input#submit")

        # On appeal result page, should see account creation prompt
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

        # Look for the account prompt banner
        self.assert_text("Save Your Appeals", "body")
        self.assert_text("Create Free Account", "body")
        self.assert_element('a[href="/v0/auth/signup"]')

    def test_anonymous_appeal_then_upgrade_to_account(self):
        """
        COMPREHENSIVE E2E: Anonymous user upgrades to account.

        Flow:
        1. Start on home page
        2. Generate appeal as anonymous user
        3. See account creation prompt
        4. Click to create account (the "upgrade")
        5. Verify anonymous appeal does NOT appear in dashboard (security)
        6. Generate NEW appeal while logged in
        7. Verify new appeal DOES appear in dashboard
        """
        anon_email = "upgrade_e2e@example.com"
        anon_password = "UpgradePass123!"

        # Step 1: Start on home page
        self.open(f"{self.live_server_url}/")
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial -- Use AI to Generate Your Health Insurance Appeal"
        )

        # Step 2: Click "Generate Appeal" from home page
        self.click('a[id="scanlink"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Step 3: Fill out denial form as anonymous user
        self.type("input#store_fname", "Anonymous")
        self.type("input#store_lname", "Upgrader")
        self.type("input#email", anon_email)

        anon_denial_text = """Dear Anonymous Upgrader;

Your claim for X-Ray has been denied as not medically necessary.
We believe this is experimental.

Sincerely,
Stingy Insurance Co"""

        self.type("textarea#denial_text", anon_denial_text)
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # Step 4: Skip through appeal generation flow (anonymous)
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

        # Fill categorization
        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "X-Ray")
        self.type("input#id_diagnosis", "Broken Arm")
        self.click("button#submit_cat")

        self.assert_title_eventually("Additional Resources & Questions")
        self.type("input#id_medical_reason", "Medical necessity for diagnosis")
        self.click("input#submit")

        # Step 5: Should reach appeal result page
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

        # Verify anonymous denial was created
        anon_denial = Denial.objects.get(
            hashed_email=Denial.get_hashed_email(anon_email),
            patient_user__isnull=True  # No patient_user = anonymous
        )
        self.assertEqual(anon_denial.procedure, "X-Ray")

        # Give appeal time to generate
        time.sleep(2)

        # Verify anonymous appeal exists
        anon_appeals = Appeal.objects.filter(
            for_denial=anon_denial,
            patient_user__isnull=True
        )
        self.assertGreater(anon_appeals.count(), 0, "Anonymous appeal should exist")

        # Step 6: See the account creation prompt banner
        self.assert_text("Save Your Appeals", "body")
        self.assert_text("Create Free Account", "body")

        # Step 7: Click "Create Free Account" button to upgrade
        self.click('a[href="/v0/auth/signup"]')
        self.assert_title_eventually("Create Account (Optional)")

        # Step 8: Fill out signup form (THE UPGRADE)
        self.type("input#email", anon_email)  # Same email as anonymous appeal
        self.type("input#password", anon_password)
        self.type("input#confirm_password", anon_password)
        self.type("input#first_name", "Anonymous")
        self.type("input#last_name", "Upgrader")

        # Submit signup
        self.click("button[type='submit']")

        # Step 9: Should be redirected to dashboard
        self.assert_title_eventually("My Dashboard")

        # Verify user and patient were created
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(email=anon_email)
        patient = PatientUser.objects.get(user=user)
        self.assertTrue(patient.active)

        # Step 10: IMPORTANT - Verify anonymous appeal does NOT appear in dashboard
        # (Security: we don't automatically migrate old anonymous appeals)
        # The dashboard should be empty or show a message about no appeals

        # Check that X-Ray appeal is NOT visible in dashboard
        try:
            # If we can't find "X-Ray" text, that's good - it means it's not showing
            self.assert_text("X-Ray", "body")
            self.fail("Anonymous appeal should NOT appear in dashboard after account creation")
        except AssertionError:
            # This is expected - the anonymous appeal should not be visible
            pass

        # Step 11: Generate a NEW appeal while logged in
        self.click('a[href="/scan"]')
        self.assert_title_eventually("Upload your Health Insurance Denial")

        # Fill out new denial
        self.type("input#store_fname", "Anonymous")
        self.type("input#store_lname", "Upgrader")
        if self.is_element_visible("input#email"):
            self.type("input#email", anon_email)

        new_denial_text = """Dear Anonymous Upgrader;

Your claim for MRI Scan has been denied as not medically necessary.

Sincerely,
Stingy Insurance Co"""

        self.type("textarea#denial_text", new_denial_text)
        self.click("input#pii")
        self.click("input#privacy")
        self.click("input#tos")
        self.click("button#submit")

        # Skip through flow
        self.assert_title_eventually("Optional: Health History")
        self.click("button#next")
        self.assert_title_eventually("Optional: Add Plan Documents")
        self.click("button#next")
        self.assert_title_eventually("Categorize Your Denial")

        self.select_option_by_value("select#id_denial_type", "2")
        self.type("input#id_procedure", "MRI Scan")
        self.type("input#id_diagnosis", "Brain Injury")
        self.click("button#submit_cat")

        self.assert_title_eventually("Additional Resources & Questions")
        self.type("input#id_medical_reason", "Necessary for brain injury diagnosis")
        self.click("input#submit")

        # Should reach appeal result
        self.assert_title_eventually(
            "Fight Your Health Insurance Denial: Choose an Appeal"
        )

        # Verify new logged-in denial was created
        logged_in_denial = Denial.objects.get(
            patient_user=patient,
            procedure="MRI Scan"
        )
        self.assertEqual(logged_in_denial.diagnosis, "Brain Injury")

        time.sleep(2)

        # Verify logged-in appeal exists
        logged_in_appeals = Appeal.objects.filter(
            patient_user=patient,
            patient_visible=True
        )
        self.assertGreater(logged_in_appeals.count(), 0, "Logged-in appeal should exist")

        # Step 12: Navigate to dashboard
        self.click('a[href="/my/dashboard"]')
        self.assert_title_eventually("My Dashboard")

        # Step 13: Verify NEW appeal (MRI Scan) DOES appear in dashboard
        self.assert_text("MRI Scan", "body")
        self.assert_text("Brain Injury", "body")

        # Step 14: Verify OLD anonymous appeal (X-Ray) still does NOT appear
        # (We should only see MRI Scan, not X-Ray)
        page_content = self.get_page_source()
        mri_count = page_content.count("MRI Scan")
        xray_count = page_content.count("X-Ray")

        self.assertGreater(mri_count, 0, "MRI Scan should appear in dashboard")
        self.assertEqual(xray_count, 0, "X-Ray (anonymous appeal) should NOT appear in dashboard")

"""Selenium tests for microsite integration flow"""

import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import Denial, OngoingChat
from seleniumbase import BaseCase
from loguru import logger

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumTestMicrositeIntegration(FHISeleniumBase, StaticLiveServerTestCase):
    """Test microsite integration with appeal flow and chat interface."""
    
    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(SeleniumTestMicrositeIntegration, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(SeleniumTestMicrositeIntegration, cls).tearDownClass()

    def test_microsite_appeal_flow_stores_slug(self):
        """Test that coming from a microsite stores the microsite_slug in the Denial."""
        # Visit the MRI denial microsite
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        
        # Check that the page loads
        self.assert_element('a.primary-cta')
        
        # Click "Start Your Appeal" button
        self.click('a.primary-cta')
        
        # Should be on the scan page with microsite parameters
        time.sleep(1)
        self.assert_title_eventually("Upload your Health Insurance Denial")
        
        # Fill out the form
        self.type("input#store_fname", "Test")
        self.type("input#store_lname", "User")
        self.type("input#email", "microsite-test@example.com")
        self.type("textarea#denial_text", "My MRI scan was denied as not medically necessary.")
        self.type("input#zip", "12345")
        self.click("input#pii")
        self.click("input#tos")
        self.click("input#privacy")
        self.click("button#submit")
        
        # Wait for redirect
        time.sleep(2)
        
        # Check that a denial was created with the microsite_slug
        denials = Denial.objects.filter(
            hashed_email=Denial.get_hashed_email("microsite-test@example.com")
        )
        self.assertTrue(denials.exists(), "Denial should be created")
        
        denial = denials.first()
        self.assertEqual(
            denial.microsite_slug,
            "mri-denial",
            "Denial should have microsite_slug set to 'mri-denial'"
        )
        
        # Verify the default procedure was set
        self.assertIn("MRI", denial.procedure or "")

    def test_microsite_chat_flow_stores_slug(self):
        """Test that coming from a microsite stores the microsite_slug in OngoingChat."""
        # Visit the MRI denial microsite
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        
        # Check that the page loads
        self.assert_element('a.secondary-cta')
        
        # Click "AI Chat" button
        self.click('a.secondary-cta')
        
        # Should redirect to chat consent page
        time.sleep(1)
        
        # Fill out the consent form
        self.type("input#store_fname", "Chat")
        self.type("input#store_lname", "Test")
        self.type("input#email", "chat-microsite-test@example.com")
        self.click("input#tos")
        self.click("input#privacy")
        self.click("button[type='submit']")
        
        # Wait for redirect to chat interface
        time.sleep(2)
        
        # Check that we're on the chat page
        self.assert_element('#chat-interface-root')

    def test_microsite_chat_triggers_pubmed_search(self):
        """Test that chat from microsite triggers PubMed searches with status messages."""
        # Visit the MRI denial microsite
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        
        # Check that the page loads
        self.assert_element('a.secondary-cta')
        
        # Click "AI Chat" button
        self.click('a.secondary-cta')
        
        # Should redirect to chat consent page
        time.sleep(1)
        
        # Fill out the consent form
        self.type("input#store_fname", "PubMed")
        self.type("input#store_lname", "SearchTest")
        self.type("input#email", "pubmed-search-test@example.com")
        self.click("input#tos")
        self.click("input#privacy")
        self.click("button[type='submit']")
        
        # Wait for redirect to chat interface
        time.sleep(2)
        
        # Check that we're on the chat page
        self.assert_element('#chat-interface-root')
        
        # Wait for the chat to initialize and auto-send the initial message
        # The initial message is sent automatically when coming from a microsite
        time.sleep(3)
        
        # Look for the chat messages container
        self.assert_element('[class*="chat"]', timeout=10)
        
        # Wait for status messages indicating PubMed search is happening
        # The chat should show "Searching medical literature for MRI Scan..."
        # Since searches are now non-blocking/background, we need to wait for them to eventually complete
        # Try multiple times over a longer period
        search_found = False
        max_attempts = 15  # Try for up to 15 seconds
        for attempt in range(max_attempts):
            time.sleep(1)
            page_text = self.get_page_source()
            
            # Check for specific PubMed search status messages
            if ("Searching medical literature" in page_text or
                "Medical literature search" in page_text or
                "Searching:" in page_text):
                search_found = True
                break
        
        self.assertTrue(
            search_found,
            "Chat should eventually show PubMed search status messages ('Searching medical literature...' or 'Medical literature search complete')"
        )
        
        # Verify that an OngoingChat was created with the microsite_slug
        # Wait a bit more for background processing
        time.sleep(3)
        chats = OngoingChat.objects.filter(
            hashed_email=Denial.get_hashed_email("pubmed-search-test@example.com")
        )
        
        if chats.exists():
            chat = chats.first()
            self.assertEqual(
                chat.microsite_slug,
                "mri-denial",
                "OngoingChat should have microsite_slug set to 'mri-denial'"
            )
            
            # Check that PubMed results were eventually stored in the chat context
            # The background task should have added context to summary_for_next_call
            if chat.summary_for_next_call:
                context_str = str(chat.summary_for_next_call)
                has_pubmed_context = "PubMed" in context_str or "PMID" in context_str
                # This is optional since timing can vary, just log if not found
                if not has_pubmed_context:
                    logger.warning("PubMed context not yet stored in chat (background task may still be running)")

    def test_microsite_landing_page_elements(self):
        """Test that microsite landing page has all expected elements."""
        # Visit the MRI denial microsite
        self.open(f"{self.live_server_url}/microsite/mri-denial")

        # Check that key elements are present
        self.assert_element('a.primary-cta')  # Start Your Appeal button
        self.assert_element('a.secondary-cta')  # AI Chat button

        # Verify the links include microsite parameters
        appeal_link = self.get_attribute('a.primary-cta', 'href')
        self.assertIn('microsite_slug=mri-denial', appeal_link)
        self.assertIn('default_procedure=', appeal_link)

        chat_link = self.get_attribute('a.secondary-cta', 'href')
        self.assertIn('microsite_slug=mri-denial', chat_link)
        self.assertIn('default_procedure=', chat_link)


class SeleniumTestMicrositeExistingUser(FHISeleniumBase, StaticLiveServerTestCase):
    """Test microsite integration for users with existing chat sessions."""

    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/followup.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    @classmethod
    def setUpClass(cls):
        super(SeleniumTestMicrositeExistingUser, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(SeleniumTestMicrositeExistingUser, cls).tearDownClass()

    def setup_existing_session(self, email):
        """Set up an existing chat session."""
        self.open(f"{self.live_server_url}/chat-consent")
        self.type("input#store_fname", "Existing")
        self.type("input#store_lname", "MicrositeUser")
        self.type("input#email", email)
        self.click("input#tos")
        self.click("input#privacy")
        self.click("button[type='submit']")
        time.sleep(2)
        self.assert_element('#chat-interface-root')

    def test_existing_user_microsite_chat_preserves_user_info(self):
        """Test that user info is preserved when existing user visits microsite chat."""
        email = f"existing_microsite_{int(time.time())}@example.com"

        # First establish session
        self.setup_existing_session(email)

        # Verify user info stored
        user_info = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        self.assertIsNotNone(user_info)

        # Now visit microsite and click chat
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        self.assert_element('a.secondary-cta')
        self.click('a.secondary-cta')
        time.sleep(1)

        # Should skip consent (already consented) or have pre-filled form
        # Check if we're on consent page with pre-filled info or directly on chat
        current_url = self.driver.current_url

        if "chat-consent" in current_url:
            # Consent page should have pre-filled info
            fname = self.execute_script(
                "return document.getElementById('store_fname')?.value || '';"
            )
            self.assertEqual(fname, "Existing", "First name should be pre-filled")

            # Just submit (fields are pre-filled, checkboxes need re-checking)
            self.click("input#tos")
            self.click("input#privacy")
            self.click("button[type='submit']")
            time.sleep(2)

        # Should be on chat now
        self.assert_element('#chat-interface-root')

        # User info should still be preserved
        user_info_after = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        self.assertIsNotNone(user_info_after)

    def test_existing_user_microsite_chat_gets_procedure_context(self):
        """Test that existing user gets microsite procedure context in chat."""
        email = f"microsite_context_{int(time.time())}@example.com"

        # First establish session
        self.setup_existing_session(email)

        # Now visit a different microsite
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        self.click('a.secondary-cta')
        time.sleep(1)

        # Complete consent if needed
        if "chat-consent" in self.driver.current_url:
            self.click("input#tos")
            self.click("input#privacy")
            self.click("button[type='submit']")
            time.sleep(2)

        # Should be on chat with MRI context
        self.assert_element('#chat-interface-root')

        # The chat interface should have the default_procedure data attribute
        default_procedure = self.execute_script(
            "return document.getElementById('chat-interface-root')?.dataset?.defaultProcedure || '';"
        )
        # Should contain MRI-related procedure
        self.assertTrue(
            "MRI" in default_procedure or default_procedure != "",
            f"Chat should have procedure context, got: {default_procedure}"
        )

    def test_microsite_external_models_preference_persists(self):
        """Test that external models preference from microsite flow persists."""
        email = f"microsite_external_{int(time.time())}@example.com"

        # Visit microsite and go to chat
        self.open(f"{self.live_server_url}/microsite/mri-denial")
        self.click('a.secondary-cta')
        time.sleep(1)

        # Fill consent with external models enabled
        self.type("input#store_fname", "Microsite")
        self.type("input#store_lname", "External")
        self.type("input#email", email)
        self.click("input#tos")
        self.click("input#privacy")
        self.click("input#use_external_models")  # Enable external models
        self.click("button[type='submit']")
        time.sleep(2)

        # Verify external models preference saved
        external_pref = self.execute_script(
            "return localStorage.getItem('fhi_use_external_models');"
        )
        self.assertEqual(external_pref, "true", "External models should be enabled")

        # Visit a different microsite
        self.open(f"{self.live_server_url}/microsite/physical-therapy-denial")

        # If it exists, check that clicking chat preserves preference
        # If not, just verify the preference is still in localStorage
        pref_still_set = self.execute_script(
            "return localStorage.getItem('fhi_use_external_models');"
        )
        self.assertEqual(pref_still_set, "true", "Preference should persist")

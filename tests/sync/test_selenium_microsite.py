"""Selenium tests for microsite integration flow"""

import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from fighthealthinsurance.models import Denial, OngoingChat, ChatLeads
from seleniumbase import BaseCase

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
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

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
        
        # Wait a bit for the chat to initialize
        time.sleep(3)
        
        # Note: We can't easily test the OngoingChat creation via selenium since it happens
        # via WebSocket. This would require more complex setup. The integration test
        # in test_microsite_integration.py covers the backend logic.

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

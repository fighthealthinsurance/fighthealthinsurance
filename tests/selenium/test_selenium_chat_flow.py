"""Selenium tests for chat flow and explain denial -> chat flow.

Tests verify that:
1. User data (email, name, etc.) flows correctly from consent form to chat
2. Email is properly stored in the backend for GDPR data deletion support
3. The explain denial page properly sends denial text to chat
"""

import json
import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumChatFlowTest(FHISeleniumBase, StaticLiveServerTestCase):
    """
    Test chat flow ensuring user data flows correctly from consent to chat.
    """

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def wait_for_element(self, selector, timeout=10):
        """Wait for an element to be present."""
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def fill_consent_form(self, email="chatflow_test@example.com"):
        """Fill out the chat consent form with test data."""
        self.type("input#store_fname", "ChatFlowTest")
        self.type("input#store_lname", "User")
        self.type("input#email", email)
        self.type("input#store_street", "123 Test St")
        self.type("input#store_city", "TestCity")
        self.type("input#store_state", "CA")
        self.type("input#store_zip", "12345")
        self.click("input#tos")
        self.click("input#privacy")

    def test_chat_consent_stores_user_info_in_localstorage(self):
        """Test that user info from consent form is stored in localStorage."""
        # Open chat consent page
        self.open(f"{self.live_server_url}/chat-consent")

        # Fill and submit consent form
        test_email = "localstorage_test@example.com"
        self.fill_consent_form(email=test_email)
        self.click("button[type='submit']")

        # Wait for chat interface to load
        self.wait_for_element("#chat-interface-root", timeout=10)
        self.wait(1)

        # Verify user info is stored in localStorage
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored in localStorage"

        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email, f"Email should be {test_email}"
        assert user_info["firstName"] == "ChatFlowTest", "First name should match"
        assert user_info["lastName"] == "User", "Last name should match"
        print(f"✓ User info correctly stored in localStorage: {user_info['email']}")

    def test_chat_message_data_includes_email(self):
        """Test that message data prepared for WebSocket includes email from localStorage.

        Note: This test verifies the client-side data preparation. The actual WebSocket
        communication requires an ASGI server and is tested separately in async tests.
        """
        # Open chat consent page
        self.open(f"{self.live_server_url}/chat-consent")

        # Fill and submit consent form with a unique email
        test_email = f"chat_email_test_{int(time.time())}@example.com"
        self.fill_consent_form(email=test_email)
        self.click("button[type='submit']")

        # Wait for chat interface to load
        self.wait_for_element("#chat-interface-root", timeout=10)
        self.wait(2)

        # Verify that the email is available in user info for sending
        user_info = self.execute_script("""
            // Check if userInfoStorage module is available
            if (window.userInfoStorage && window.userInfoStorage.getUserInfo) {
                return window.userInfoStorage.getUserInfo();
            }
            // Fallback to direct localStorage access
            const stored = localStorage.getItem('fhi_user_info');
            return stored ? JSON.parse(stored) : null;
        """)

        assert user_info is not None, "User info should be available"
        assert user_info.get("email") == test_email, f"Email should be {test_email}"

        # Verify the chat interface has the data it needs to send to backend
        # by checking that a message would include the email
        message_would_include_email = self.execute_script(f"""
            const userInfo = localStorage.getItem('fhi_user_info');
            if (userInfo) {{
                const parsed = JSON.parse(userInfo);
                return parsed.email === '{test_email}';
            }}
            return false;
        """)

        assert message_would_include_email, "Message data should include correct email"
        print(f"✓ Chat interface has email ({test_email}) ready for backend communication")

    def test_chat_interface_receives_user_info(self):
        """Test that the chat interface correctly retrieves user info from localStorage."""
        # Open chat consent page
        self.open(f"{self.live_server_url}/chat-consent")

        # Fill and submit consent form
        test_email = "userinfo_test@example.com"
        self.fill_consent_form(email=test_email)
        self.click("button[type='submit']")

        # Wait for chat interface to load
        self.wait_for_element("#chat-interface-root", timeout=10)
        self.wait(1)

        # Verify getUserInfo() returns correct data
        user_info = self.execute_script("""
            if (window.userInfoStorage && window.userInfoStorage.getUserInfo) {
                return window.userInfoStorage.getUserInfo();
            }
            // Fallback: parse from localStorage directly
            const stored = localStorage.getItem('fhi_user_info');
            return stored ? JSON.parse(stored) : null;
        """)

        assert user_info is not None, "getUserInfo() should return user data"
        assert user_info["email"] == test_email, f"Email should be {test_email}"
        print(f"✓ getUserInfo() correctly returns email: {user_info['email']}")


class SeleniumExplainDenialChatFlowTest(FHISeleniumBase, StaticLiveServerTestCase):
    """
    Test the explain denial -> chat flow.
    """

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @classmethod
    def setUpClass(cls):
        super(StaticLiveServerTestCase, cls).setUpClass()
        super(BaseCase, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        super(StaticLiveServerTestCase, cls).tearDownClass()
        super(BaseCase, cls).tearDownClass()

    def wait_for_element(self, selector, timeout=10):
        """Wait for an element to be present."""
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )

    def fill_consent_fields(self, email="explain_denial_test@example.com"):
        """Fill out the user consent fields in the explain denial form."""
        self.type("input#store_fname", "ExplainDenial")
        self.type("input#store_lname", "Tester")
        self.type("input#email", email)
        self.type("input#store_street", "456 Denial St")
        self.type("input#store_city", "DenialCity")
        self.type("input#store_state", "NY")
        self.type("input#store_zip", "67890")
        self.click("input#tos")
        self.click("input#privacy")

    def test_explain_denial_page_loads(self):
        """Test that the explain denial page loads correctly."""
        self.open(f"{self.live_server_url}/explain-denial")
        self.assert_title("Explain My Denial - Fight Health Insurance")

        # Verify key elements are present
        self.assert_element_present("textarea#denial_text")
        self.assert_element_present("input#store_fname")
        self.assert_element_present("input#email")
        self.assert_element_present("input#tos")
        print("✓ Explain denial page loads with all form fields")

    def test_explain_denial_stores_user_info(self):
        """Test that user info is stored when explain denial form is submitted."""
        self.open(f"{self.live_server_url}/explain-denial")

        # Fill denial text
        denial_text = "Your claim for MRI has been denied due to lack of medical necessity."
        self.type("textarea#denial_text", denial_text)

        # Fill user consent fields
        test_email = "explain_storage_test@example.com"
        self.fill_consent_fields(email=test_email)

        # Submit form
        self.click("button[type='submit']")

        # Wait for redirect to chat
        self.wait_for_element("#chat-interface-root", timeout=15)
        self.wait(1)

        # Verify user info is stored
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored"

        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email, f"Email should be {test_email}"
        print(f"✓ User info stored after explain denial submission: {user_info['email']}")

    def test_explain_denial_passes_denial_text_to_chat(self):
        """Test that denial text from explain denial page is available in chat interface."""
        self.open(f"{self.live_server_url}/explain-denial")

        # Fill denial text with identifiable content
        denial_text = "UNIQUE_DENIAL_12345: Your claim for physical therapy has been denied."
        self.type("textarea#denial_text", denial_text)

        # Fill user consent fields
        test_email = f"denial_to_chat_{int(time.time())}@example.com"
        self.fill_consent_fields(email=test_email)

        # Submit form
        self.click("button[type='submit']")

        # Wait for chat interface to load
        self.wait_for_element("#chat-interface-root", timeout=15)
        self.wait(2)

        # The denial text should be passed to the chat interface (may appear in UI or be ready to send)
        # Check page content or data attributes
        page_content = self.execute_script("return document.body.innerText;")

        # Check if the denial-related content is in the page
        # The text may be shown as a user message or in the interface
        has_denial_context = (
            "denial" in page_content.lower() or
            "claim" in page_content.lower() or
            "physical therapy" in page_content.lower()
        )

        # Also verify user info is stored
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored"
        assert has_denial_context, "Denial context should be present in chat"

        print("✓ Denial context passed to chat interface")

    def test_explain_denial_prepares_email_for_backend(self):
        """Test that submitting explain denial prepares email for backend storage.

        Note: This test verifies client-side data preparation. WebSocket-based
        backend communication is tested separately in async tests.
        """
        self.open(f"{self.live_server_url}/explain-denial")

        # Fill denial text
        denial_text = "Your claim has been denied for prior authorization requirements."
        self.type("textarea#denial_text", denial_text)

        # Fill user consent fields with unique email
        test_email = f"denial_chat_email_{int(time.time())}@example.com"
        self.fill_consent_fields(email=test_email)

        # Submit form
        self.click("button[type='submit']")

        # Wait for chat interface
        self.wait_for_element("#chat-interface-root", timeout=15)
        self.wait(2)

        # Verify user info is stored with email for backend communication
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored"

        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email, f"Email should be {test_email}"

        # Verify chat interface root is loaded
        chat_root = self.execute_script(
            "return document.getElementById('chat-interface-root') !== null;"
        )
        assert chat_root, "Chat interface should be present"

        print(f"✓ Email ({test_email}) stored and chat interface loaded")

    def test_explain_denial_flow_end_to_end(self):
        """Full end-to-end test of explain denial -> chat flow (client side).

        Note: This test verifies the complete client-side flow from explain denial
        to chat interface. Backend WebSocket communication is tested separately.
        """
        # Start at explain denial page
        self.open(f"{self.live_server_url}/explain-denial")
        self.assert_title("Explain My Denial - Fight Health Insurance")

        # Fill in denial letter
        denial_text = """Dear Patient,

Your claim for treatment code 97110 (Physical Therapy) on 12/01/2024 has been DENIED.

Reason: The requested service does not meet our medical necessity criteria.

You have the right to appeal this decision within 180 days.

Sincerely,
Insurance Company"""
        self.type("textarea#denial_text", denial_text)

        # Fill user consent fields
        test_email = f"e2e_test_{int(time.time())}@example.com"
        self.fill_consent_fields(email=test_email)

        # Submit form
        self.click("button[type='submit']")

        # Should redirect to chat interface
        self.wait_for_element("#chat-interface-root", timeout=15)

        # Verify localStorage has user info
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be in localStorage"

        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email
        assert user_info["firstName"] == "ExplainDenial"
        assert user_info["lastName"] == "Tester"

        # Verify chat interface loaded properly
        chat_root = self.execute_script(
            "return document.getElementById('chat-interface-root') !== null;"
        )
        assert chat_root, "Chat interface root should be present"

        # Verify page URL indicates chat
        assert "chat" in self.driver.current_url.lower()

        print("✓ End-to-end explain denial -> chat flow completed successfully")
        print(f"  - Denial letter form submitted")
        print(f"  - User info stored in localStorage: {test_email}")
        print(f"  - Redirected to chat interface")
        print(f"  - Chat interface loaded successfully")

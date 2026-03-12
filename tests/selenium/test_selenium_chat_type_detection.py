"""Selenium tests for chat type detection (patient vs trial professional vs professional).

Tests verify that:
1. Patient chat flow via consent form creates correct session
2. Patient chat flow via explain denial creates correct session
3. Patient consent doesn't create ChatLeads entries
4. Server-side OngoingChat records have correct chat_type
"""

import json
import time
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from seleniumbase import BaseCase
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from fighthealthinsurance.models import ChatType, Denial, OngoingChat

from .fhi_selenium_base import FHISeleniumBase

BaseCase.main(__name__, __file__)


class SeleniumPatientChatTypeTest(FHISeleniumBase, StaticLiveServerTestCase):
    """
    Test that patient chat consent flow correctly sets up patient chat sessions.
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

    def fill_consent_form(self, email="patient_type_test@example.com"):
        """Fill out the chat consent form with test data."""
        self.type("input#store_fname", "PatientTypeTest")
        self.type("input#store_lname", "User")
        self.type("input#email", email)
        self.type("input#store_street", "123 Test St")
        self.type("input#store_city", "TestCity")
        self.type("input#store_state", "CA")
        self.type("input#store_zip", "12345")
        self.click("input#tos")
        self.click("input#privacy")

    def assert_server_side_chat_type(self, email, expected_chat_type, timeout=5):
        """Poll for an OngoingChat by hashed email and assert chat_type.

        Retries until timeout since the chat is created asynchronously via WebSocket.
        """
        import time as _time

        hashed_email = Denial.get_hashed_email(email)
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            chat = OngoingChat.objects.filter(hashed_email=hashed_email).first()
            if chat is not None:
                assert chat.chat_type == expected_chat_type, (
                    f"Expected chat_type={expected_chat_type}, got {chat.chat_type}"
                )
                return
            _time.sleep(0.5)
        raise AssertionError(
            f"No OngoingChat found for email hash after {timeout}s"
        )

    def test_patient_consent_creates_patient_session(self):
        """Test that patient consent flow stores user info in localStorage."""
        self.open(f"{self.live_server_url}/chat-consent")

        test_email = f"patient_chat_type_{int(time.time())}@example.com"
        self.fill_consent_form(email=test_email)
        self.click("button[type='submit']")

        # Wait for chat interface to load and localStorage to be populated
        self.wait_for_page_ready(localstorage_key="fhi_user_info")

        # Verify user info is stored in localStorage (patient flow)
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored in localStorage"

        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email, f"Email should be {test_email}"

        # Server-side: if a chat was created via WebSocket, verify its type
        self.assert_server_side_chat_type(test_email, ChatType.PATIENT)

    def test_patient_session_has_no_session_key_in_leads(self):
        """Test that patient consent doesn't create a ChatLeads entry."""
        from fighthealthinsurance.models import ChatLeads

        initial_count = ChatLeads.objects.count()

        self.open(f"{self.live_server_url}/chat-consent")

        test_email = f"patient_no_lead_{int(time.time())}@example.com"
        self.fill_consent_form(email=test_email)
        self.click("button[type='submit']")

        self.wait_for_page_ready(localstorage_key="fhi_user_info")

        # Patient consent should NOT create a ChatLeads entry
        final_count = ChatLeads.objects.count()
        assert final_count == initial_count, (
            f"Patient consent should not create ChatLeads entries. "
            f"Before: {initial_count}, after: {final_count}"
        )

    def test_explain_denial_creates_patient_session(self):
        """Test that explain denial flow also creates a patient session."""
        self.open(f"{self.live_server_url}/explain-denial")

        denial_text = "My MRI was denied as not medically necessary."
        self.type("textarea#denial_text", denial_text)

        test_email = f"explain_type_{int(time.time())}@example.com"
        self.type("input#store_fname", "ExplainType")
        self.type("input#store_lname", "Tester")
        self.type("input#email", test_email)
        self.click("input#tos")
        self.click("input#privacy")

        self.click("button[type='submit']")

        self.wait_for_page_ready(localstorage_key="fhi_user_info")

        # Verify user info stored
        user_info_json = self.execute_script(
            "return localStorage.getItem('fhi_user_info');"
        )
        assert user_info_json is not None, "User info should be stored"
        user_info = json.loads(user_info_json)
        assert user_info["email"] == test_email

        # Server-side: if a chat was created via WebSocket, verify its type
        self.assert_server_side_chat_type(test_email, ChatType.PATIENT)

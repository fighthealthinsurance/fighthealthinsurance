"""
Tests to verify email notifications for non-professional fax attempts.
These tests create test fax objects and simulate both successful and failed
fax attempts to confirm email notifications are properly generated.
"""

from django.test import TestCase
from django.core import mail
from django.utils import timezone
from django.urls import reverse
import logging

from fighthealthinsurance.models import FaxesToSend, Denial, Appeal

# Patch ray.remote to be a no-op decorator so FaxActor remains the real class
import sys
class FakeRay:
    def remote(self, *args, **kwargs):
        def decorator(cls):
            return cls
        return decorator
sys.modules['ray'] = FakeRay()
from fighthealthinsurance.fax_actor import FaxActor

# Set up logging
logger = logging.getLogger(__name__)

class TestFaxEmailNotification(TestCase):
    """Test case for fax email notification functionality"""
  
    def setUp(self):
        """Set up test data and URL mocking"""
        # Mock reverse URL to avoid URL resolution issues
        self.original_reverse = reverse
        import django.urls
        django.urls.reverse = lambda *args, **kwargs: "/mock/url/path"
        # Create base test denial
        self.denial = Denial.objects.create(
            denial_text="Test denial text",
            hashed_email="test_hash",
            claim_id="TEST123",
            appeal_text="Test appeal content",
            date=timezone.now(),
        )

    def tearDown(self):
        """Restore URL mocking and clear outbox"""
        import django.urls
        django.urls.reverse = self.original_reverse
        mail.outbox = []

    def create_test_fax(self, professional=False, appeal=None):
        """Create a test fax record"""
        fax = FaxesToSend.objects.create(
            email="test@example.com",
            hashed_email="test_hash",
            denial_id=self.denial,
            destination="1234567890",
            name="Test User",
            professional=professional,
            should_send=True,
            appeal_text="Test appeal content",
            paid=False,
            date=timezone.now(),
        )
        if appeal:
            fax.for_appeal = appeal
            fax.save()
        return fax

    def create_test_appeal(self):
        """Create a test appeal record"""
        return Appeal.objects.create(
            for_denial=self.denial,
            appeal_text="Test appeal content",
            sent=False,
            hashed_email="test_hash"
        )

    def test_successful_fax_email(self):
        """Test email notification for a successful non-professional fax"""
        mail.outbox = []
        fax = self.create_test_fax(professional=False)
        FaxActor()._update_fax_for_sent(fax, True)
        self.assertEqual(len(mail.outbox), 1, "Expected one email to be sent")
        email = mail.outbox[0]
        self.assertIn("successful", email.body.lower(), "Email should indicate success")
        self.assertEqual(email.to, ["test@example.com"], "Email should be sent to the right recipient")
    
    def test_failed_fax_email(self):
        """Test email notification for a failed non-professional fax"""
        mail.outbox = []
        fax = self.create_test_fax(professional=False)
        FaxActor()._update_fax_for_sent(fax, False)
        self.assertEqual(len(mail.outbox), 1, "Expected one email to be sent")
        email = mail.outbox[0]
        self.assertIn("problem", email.body.lower(), "Email should indicate a problem")
        self.assertEqual(email.to, ["test@example.com"], "Email should be sent to the right recipient")
    
    def test_professional_fax_no_email(self):
        """Test that no email is sent for professional faxes"""
        mail.outbox = []
        # Create and attach an appeal so professional branch can update it
        appeal = self.create_test_appeal()
        fax = self.create_test_fax(professional=True, appeal=appeal)
        FaxActor()._update_fax_for_sent(fax, True)
        self.assertEqual(len(mail.outbox), 0, "No emails should be sent for professional faxes")



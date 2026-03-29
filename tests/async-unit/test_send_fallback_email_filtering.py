"""Tests for email filtering in send_fallback_email."""

import pytest
from unittest.mock import patch
from django.core import mail

from fighthealthinsurance.utils import send_fallback_email


@pytest.mark.django_db
class TestSendFallbackEmailFiltering:
    """Test that send_fallback_email filters blocked email domains."""

    @patch("fighthealthinsurance.utils.render_to_string", return_value="test content")
    def test_skips_blocked_domain(self, mock_render):
        """Emails to blocked domains should be silently skipped."""
        send_fallback_email(
            subject="Test",
            template_name="followup",
            context={},
            to_email="user@mailinator.com",
        )
        assert len(mail.outbox) == 0

    @patch("fighthealthinsurance.utils.render_to_string", return_value="test content")
    def test_skips_example_com(self, mock_render):
        """Emails to example.com should be silently skipped."""
        send_fallback_email(
            subject="Test",
            template_name="followup",
            context={},
            to_email="test@example.com",
        )
        assert len(mail.outbox) == 0

    @patch("fighthealthinsurance.utils.render_to_string", return_value="test content")
    def test_skips_fake_fighthealthinsurance(self, mock_render):
        """Emails to -fake@fighthealthinsurance.com should be skipped."""
        send_fallback_email(
            subject="Test",
            template_name="followup",
            context={},
            to_email="test-fake@fighthealthinsurance.com",
        )
        assert len(mail.outbox) == 0

    @patch("fighthealthinsurance.utils.render_to_string", return_value="test content")
    def test_sends_to_valid_domain(self, mock_render):
        """Emails to valid domains should be sent."""
        send_fallback_email(
            subject="Test",
            template_name="followup",
            context={},
            to_email="user@gmail.com",
        )
        # Should have sent: 1 to recipient + 1 to BCC
        assert any("user@gmail.com" in m.to for m in mail.outbox)

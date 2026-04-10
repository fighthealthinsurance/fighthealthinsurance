"""Tests for email validation utilities (disposable email domain filtering)."""

import pytest

from fighthealthinsurance.email_utils import (
    BLOCKED_EMAIL_DOMAINS,
    is_blocked_email,
    is_sendable_email,
)


class TestIsBlockedEmail:
    """Test the is_blocked_email function."""

    @pytest.mark.parametrize(
        "domain",
        [
            "example.com",
            "example.net",
            "example.org",
        ],
    )
    def test_blocked_domains_are_rejected(self, domain):
        assert is_blocked_email(f"user@{domain}") is True

    def test_fake_fighthealthinsurance_emails_are_rejected(self):
        assert is_blocked_email("test-fake@fighthealthinsurance.com") is True
        assert is_blocked_email("abc123-fake@fighthealthinsurance.com") is True

    def test_real_fighthealthinsurance_emails_are_not_rejected(self):
        assert is_blocked_email("support42@fighthealthinsurance.com") is False

    @pytest.mark.parametrize(
        "email",
        [
            "user@gmail.com",
            "patient@outlook.com",
            "doctor@hospital.org",
            "admin@fighthealthinsurance.com",
            "user@yahoo.com",
            "name@protonmail.com",
        ],
    )
    def test_valid_domains_are_accepted(self, email):
        assert is_blocked_email(email) is False

    def test_case_insensitive_domain_matching(self):
        assert is_blocked_email("user@GMAIL.COM") is False
        assert is_blocked_email("user@Example.Com") is True
        assert is_blocked_email("user@YAHOO.COM") is False

    def test_empty_email_is_blocked(self):
        assert is_blocked_email("") is True

    def test_none_is_blocked(self):
        assert is_blocked_email(None) is True

    def test_no_at_sign_is_blocked(self):
        assert is_blocked_email("not-an-email") is True

    def test_whitespace_email_is_blocked(self):
        assert is_blocked_email("   ") is True

    def test_email_with_whitespace_is_handled(self):
        assert is_blocked_email(" user@gmail.com ") is False
        assert is_blocked_email(" user@example.com ") is True
        assert is_blocked_email(" user@yahoo.com ") is False

    def test_blocked_domains_set_is_frozen(self):
        assert isinstance(BLOCKED_EMAIL_DOMAINS, frozenset)


class TestIsSendableEmail:
    """Test the is_sendable_email function (inverse of is_blocked_email)."""

    def test_valid_is_sendable(self):
        assert is_sendable_email("user@gmail.com") is True
        assert is_sendable_email("user@yahoo.com") is True
        assert is_sendable_email("test@example.com") is False

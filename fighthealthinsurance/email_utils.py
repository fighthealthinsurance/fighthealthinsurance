"""Email validation utilities for filtering disposable/temporary email domains."""

from typing import Optional

# Known invalid domains (temporary mail is _ok_ and should not be included).
# Emails to these domains will never be delivered successfully or read,
# so we skip sending to avoid wasting resources and hurting sender reputation.
BLOCKED_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        # RFC 2606 reserved domains
        "example.com",
        "example.net",
        "example.org",
        "invalid",
        "test",
    }
)


def get_email_domain(email: Optional[str]) -> Optional[str]:
    """Return the lowercased domain of an email address, or None if unparseable.

    Shared helper so domain extraction (strip/lowercase/split on the last "@")
    lives in one place instead of being re-implemented at each call site.
    """
    if not email or not isinstance(email, str):
        return None
    email = email.strip().lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip()
    return domain or None


def is_blocked_email(email: str) -> bool:
    """Check if an email address should be blocked from sending.

    Returns True for:
    - Emails with domains in the BLOCKED_EMAIL_DOMAINS set
    - Emails matching the test pattern -fake@fighthealthinsurance.com
    - Malformed emails (no @ sign, empty)
    """
    if not email or not isinstance(email, str):
        return True

    # Existing test-email pattern
    if email.strip().lower().endswith("-fake@fighthealthinsurance.com"):
        return True

    domain = get_email_domain(email)
    if not domain:
        return True

    return domain in BLOCKED_EMAIL_DOMAINS


def is_sendable_email(email: str) -> bool:
    """Check if an email address is safe to send to (inverse of is_blocked_email)."""
    return not is_blocked_email(email)

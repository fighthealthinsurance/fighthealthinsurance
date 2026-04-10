"""Email validation utilities for filtering disposable/temporary email domains."""

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
        # Disposable/temporary email providers
        "mailinator.com",
        "guerrillamail.com",
        "tempmail.com",
        "10minutemail.com",
        "trashmail.com",
        "yopmail.com",
    }
)


def is_blocked_email(email: str) -> bool:
    """Check if an email address should be blocked from sending.

    Returns True for:
    - Emails with domains in the BLOCKED_EMAIL_DOMAINS set
    - Emails matching the test pattern -fake@fighthealthinsurance.com
    - Malformed emails (no @ sign, empty)
    """
    if not email or not isinstance(email, str):
        return True

    email = email.strip().lower()

    # Existing test-email pattern
    if email.endswith("-fake@fighthealthinsurance.com"):
        return True

    if "@" not in email:
        return True

    domain = email.rsplit("@", 1)[1].strip()
    if not domain:
        return True

    return domain in BLOCKED_EMAIL_DOMAINS


def is_sendable_email(email: str) -> bool:
    """Check if an email address is safe to send to (inverse of is_blocked_email)."""
    return not is_blocked_email(email)

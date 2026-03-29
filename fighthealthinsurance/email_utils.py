"""Email validation utilities for filtering disposable/temporary email domains."""

# Domains known to provide disposable/temporary email addresses.
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
        # Common disposable/temporary email services
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "guerrillamailblock.com",
        "grr.la",
        "tempmail.com",
        "temp-mail.org",
        "throwaway.email",
        "yopmail.com",
        "yopmail.fr",
        "sharklasers.com",
        "10minutemail.com",
        "minutemail.com",
        "maildrop.cc",
        "mailnesia.com",
        "dispostable.com",
        "trashmail.com",
        "trashmail.net",
        "trashmail.org",
        "fakeinbox.com",
        "mailcatch.com",
        "getnada.com",
        "nada.email",
        "mohmal.com",
        "emailondeck.com",
        "spam4.me",
        "mytemp.email",
        "tempinbox.com",
        "tempr.email",
        "tempail.com",
        "33mail.com",
        "guerrillamail.info",
        "guerrillamail.de",
        "harakirimail.com",
        "mailexpire.com",
        "throwam.com",
        "trash-mail.com",
        "bugmenot.com",
        "devnull.email",
        "mailnator.com",
        "binkmail.com",
        "safetymail.info",
        "filzmail.com",
        "meltmail.com",
        "spamgourmet.com",
        "jetable.org",
        "getairmail.com",
        "tmail.ws",
        "yolanda.dev",
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

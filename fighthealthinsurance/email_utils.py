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


# Internal / obvious-test addresses. These are real rows in our tables (staff
# testing the forms, joke signups) but never real contacts, so outreach and the
# subscriber cleanup queue treat them separately from genuine signups. Lives
# here rather than in a workflow module so every consumer shares one list.
INTERNAL_TEST_EMAILS: frozenset[str] = frozenset(
    {
        "testing@example.com",
        "farts@farts.com",
        "holden@pigscanfly.ca",
        # Also on charts' consumer-analytics exclusion list; without these an
        # internal tester's signup would land (business domains even sort to the
        # FRONT of the pro-connector queue) and get a real Cofactor intro.
        "holden.karau@gmail.com",
        "holden@fighthealthinsurance.com",
        "warrick@fighthealthinsurance.com",
        "test@test.com",
    }
)

# Signups on these TLDs are treated as spam / out of scope.
SPAM_EMAIL_TLDS: tuple[str, ...] = (".ru", ".ua")


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
    local_part, domain = email.rsplit("@", 1)
    # Require a non-empty local part too, so "@example.com" is unparseable.
    if not local_part.strip() or not domain.strip():
        return None
    return domain.strip()


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

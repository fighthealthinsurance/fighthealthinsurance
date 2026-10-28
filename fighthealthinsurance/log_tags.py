"""Short tags that stand in for personal details in log lines.

A tag is a keyed hash: the same input always gives the same tag, so one
file's or one data removal's lines can be found together, but only the
server's SECRET_KEY turns an email or a file name into its tag.
Changing SECRET_KEY changes every tag.
"""

from contextvars import ContextVar
from typing import Optional

from django.utils.crypto import salted_hmac

# Set while a data removal runs, so the stored files it deletes log its tag.
removal_tag: ContextVar[Optional[str]] = ContextVar("removal_tag", default=None)


def _tag(kind: str, value: str) -> str:
    return salted_hmac(
        f"fighthealthinsurance.log_tags.{kind}", value, algorithm="sha256"
    ).hexdigest()[:12]


def file_tag(name: str) -> str:
    """Stands in for a stored file's name, which is often a person's name."""
    return _tag("stored-file", name)


def email_tag(email: str) -> str:
    """The tag a data removal for this email logs under."""
    from fighthealthinsurance.models import Denial

    return _tag("email", Denial.get_hashed_email(email.strip().lower()))


def error_text(e: BaseException) -> str:
    """The error's type, plus an OS error's reason, which never holds the path."""
    if isinstance(e, OSError) and e.strerror:
        return f"{type(e).__name__}: {e.strerror}"
    return type(e).__name__

"""How many files one visitor may upload in an hour and in a day, on every
page that takes files: Explain My Policy, the appeal's plan documents step,
follow-up documents, server-side OCR, appeal attachments and the share page.

Counted in the database, since each web pod keeps its own cache. A visitor
is counted by address (``CF-Connecting-IP``, an IPv6 address by its /64, as
assistant_ip_limit counts it), and also by email or account when the page
has one, so a new address doesn't reset the count for the same email. Each
is kept only as a keyed digest of the period and the value, never the value,
and rows are swept once their day is over.

Files are counted when they arrive, before any is checked, so files that
fail the checks count too. Logged-in staff are not counted. When the count
can't be read, the upload goes ahead: the limit is there to stop a flood,
not to turn a person away.
"""

import datetime
from typing import Any, Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import client_network

_ADDRESS_LABEL = b"fhi-uploads-per-address-v1"
_EMAIL_LABEL = b"fhi-uploads-per-email-v1"
_ACCOUNT_LABEL = b"fhi-uploads-per-account-v1"

LIMIT_MESSAGE = (
    "You've uploaded a lot of files in a short time. "
    "Please wait a while and try again."
)
#: For one upload that is over the hour's limit by itself, where waiting
#: never helps.
BATCH_MESSAGE = "That's more files than we take at once. Please send fewer at a time."


def files_per_hour() -> int:
    return int(getattr(settings, "FHI_UPLOAD_FILES_PER_HOUR", 20))


def files_per_day() -> int:
    return int(getattr(settings, "FHI_UPLOAD_FILES_PER_DAY", 60))


def _address_of(request: HttpRequest) -> str:
    from fighthealthinsurance import assistant_ip_limit

    return assistant_ip_limit.address_of(request)


def _who(request: HttpRequest, email: Optional[str]) -> list[tuple[bytes, str]]:
    """What the upload is counted against: the address, and the email or the
    account when there is one."""
    who = [(_ADDRESS_LABEL, _address_of(request))]
    normalized = (email or "").strip().lower()
    if normalized:
        who.append((_EMAIL_LABEL, normalized))
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        who.append((_ACCOUNT_LABEL, str(user.pk)))
    return who


def refusal_message(files: int) -> str:
    """What to tell a visitor refused for an upload of ``files`` files."""
    return BATCH_MESSAGE if files > files_per_hour() else LIMIT_MESSAGE


def _periods(now: datetime.datetime) -> list[tuple[str, int]]:
    utc = now.astimezone(datetime.timezone.utc)
    return [
        ("h" + utc.strftime("%Y-%m-%dT%H"), files_per_hour()),
        ("d" + utc.strftime("%Y-%m-%d"), files_per_day()),
    ]


def _exempt(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_staff)


def take(request: HttpRequest, files: int, email: Optional[str] = None) -> bool:
    """Count ``files`` uploaded by this visitor. False when that would go
    over the hour's or the day's limit for the address, the email or the
    account, and then nothing is counted. Each count is one conditional
    update, so two pods can't both take the last place."""
    from fighthealthinsurance.models import UploadCount

    if files <= 0 or _exempt(request):
        return True
    now = timezone.now()
    day = now.astimezone(datetime.timezone.utc).date()
    try:
        UploadCount.objects.filter(day__lt=day).delete()
        with transaction.atomic():
            for period, limit in _periods(now):
                for label, value in _who(request, email):
                    key = client_network.period_key(label, period, value)
                    UploadCount.objects.get_or_create(
                        period=period, key=key, defaults={"day": day, "count": 0}
                    )
                    counted = UploadCount.objects.filter(
                        period=period, key=key, count__lte=limit - files
                    ).update(count=F("count") + files)
                    if not counted:
                        # Undo this upload's counts: the whole upload is refused.
                        transaction.set_rollback(True)
                        logger.info(
                            f"Refused {files} upload(s): over the {period[0]} limit"
                        )
                        return False
    except Exception as e:
        logger.warning(f"upload count failed: {type(e).__name__}")
        return True
    return True


def uploaded_files(request: HttpRequest, *names: str) -> int:
    """How many files came with the request in the fields ``names``."""
    return sum(len([f for f in request.FILES.getlist(name) if f]) for name in names)


def refused_form(
    form_class: Any, field: str, initial: Mapping[str, Any], files: int = 0
) -> Any:
    """A form for the page to show again with refusal_message(``files``) on
    ``field``, filled from ``initial``. Unbound, so none of the upload's
    checks run for a refused upload."""
    from django.forms.utils import ErrorDict

    form = form_class(initial=dict(initial))
    form.cleaned_data = {}
    form._errors = ErrorDict()
    form.add_error(field, refusal_message(files))
    return form

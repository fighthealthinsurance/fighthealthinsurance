"""A daily cap on agreements from one address on the assistant terms page.

Counted in the database, since each web pod keeps its own cache. The address
comes from ``CF-Connecting-IP`` only: the generic client-IP helper trusts
headers a caller can set. An IPv6 address counts by its /64, which one
household or phone usually holds whole. The row holds a keyed digest of the
address and the day, never the address, so a day's rows can't be matched
with another day's; they are swept once the day is over.

Only agreements count, not link opens, and an agreement that goes no further
(the assistant budget refuses it, or the link is already used) gives its
place back.
"""

import datetime
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import client_network

HEADER = client_network.CF_IP_META
_LABEL = b"fhi-assistant-agreements-per-ip-v1"
# Requests without a usable header share one bucket, so they can't skip the cap.
_NO_ADDRESS = client_network.NO_ADDRESS


@dataclass(frozen=True)
class Taken:
    day: datetime.date
    key: str


def _today() -> datetime.date:
    return timezone.now().astimezone(datetime.timezone.utc).date()


def address_of(request: HttpRequest) -> str:
    """The address the cap counts: the IPv4 address, or the IPv6 /64."""
    return client_network.prefix_of(
        client_network.cf_ip_from_meta(request.META), v4_bits=32, v6_bits=64
    )


def key_for(address: str, day: datetime.date) -> str:
    return client_network.period_key(_LABEL, day.isoformat(), address)


def daily_cap() -> int:
    return int(getattr(settings, "MCP_ASSISTANT_PER_IP_DAILY", 5))


def take(request: HttpRequest) -> Optional[Taken]:
    """Count one agreement from this address today, or None at the cap.
    One conditional update, so two pods can't both take the last place."""
    from fighthealthinsurance.models import AssistantAgreementCount

    day = _today()
    key = key_for(address_of(request), day)
    try:
        with transaction.atomic():
            AssistantAgreementCount.objects.get_or_create(
                day=day, key=key, defaults={"count": 0}
            )
            counted = AssistantAgreementCount.objects.filter(
                day=day, key=key, count__lt=daily_cap()
            ).update(count=F("count") + 1)
    except Exception as e:
        # The cap can't be checked, so the chat path is refused.
        logger.warning(f"assistant per-address count failed: {type(e).__name__}")
        return None
    return Taken(day=day, key=key) if counted else None


def give_back(taken: Taken) -> None:
    """Return a place for an agreement that went no further."""
    from fighthealthinsurance.models import AssistantAgreementCount

    try:
        AssistantAgreementCount.objects.filter(
            day=taken.day, key=taken.key, count__gt=0
        ).update(count=F("count") - 1)
    except Exception as e:
        logger.warning(f"assistant per-address give back failed: {type(e).__name__}")


def sweep_old(now: Optional[datetime.datetime] = None) -> int:
    """Delete the rows of days before today. Returns how many went."""
    from fighthealthinsurance.models import AssistantAgreementCount

    today = (now or timezone.now()).astimezone(datetime.timezone.utc).date()
    deleted, _ = AssistantAgreementCount.objects.filter(day__lt=today).delete()
    return deleted

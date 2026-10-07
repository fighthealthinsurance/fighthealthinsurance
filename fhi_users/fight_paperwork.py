"""The FIGHT_PAPERWORK_ENABLED switch for Fight Paperwork's account endpoints.

Fight Paperwork is paused, so its sign-up, login, invite, notification and
professional checkout endpoints answer 404 unless the setting is on. The
main Fight Health Insurance site uses none of them.
"""

import functools
from typing import Any, Callable, Iterable, Optional

from django.conf import settings
from django.http import JsonResponse
from django.urls import URLPattern
from loguru import logger

PROFESSIONAL_SUBSCRIPTION_PAYMENT_TYPE = "professional_domain_subscription"

UNAVAILABLE_MESSAGE = "Fight Paperwork accounts are not available."


def fight_paperwork_enabled() -> bool:
    return bool(getattr(settings, "FIGHT_PAPERWORK_ENABLED", False))


def unavailable_response() -> JsonResponse:
    # 404, like the other off-by-default features: the route is not served.
    return JsonResponse({"error": UNAVAILABLE_MESSAGE}, status=404)


def fight_paperwork_only(view: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a URL callback so it answers 404 while the setting is off."""

    # wraps copies csrf_exempt and DRF's cls/actions, which the schema reads.
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not fight_paperwork_enabled():
            logger.info(f"Refused Fight Paperwork route {request.path}")
            return unavailable_response()
        return view(request, *args, **kwargs)

    return wrapped


def gate_patterns(
    patterns: Iterable[Any], names: Optional[set[str]] = None
) -> list[Any]:
    """Gate URL patterns: those named in names, or all of them when None."""
    gated: list[Any] = []
    for pattern in patterns:
        if isinstance(pattern, URLPattern) and (names is None or pattern.name in names):
            pattern = URLPattern(
                pattern.pattern,
                fight_paperwork_only(pattern.callback),
                pattern.default_args,
                pattern.name,
            )
        gated.append(pattern)
    return gated


def stripe_event_payment_type(event: Any) -> Optional[str]:
    """The payment_type in a Stripe event's object metadata, if any."""
    try:
        session = event.data.object
        try:
            metadata = session.metadata
        except (AttributeError, TypeError):
            metadata = session["metadata"]
        if not metadata:
            return None
        payment_type = metadata.get("payment_type")
        return str(payment_type) if payment_type else None
    except Exception:
        return None

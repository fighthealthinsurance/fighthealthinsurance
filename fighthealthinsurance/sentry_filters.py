"""Decisions used by Sentry's before_send filter.

asgi.py defines ``before_send_filter`` inside a settings-dependent block, which
makes it awkward to import from a test. The decisions that need coverage live
here as plain functions instead.
"""

from typing import Any, Dict, List, Optional

# Django raises this when no URL pattern matches at all.
UNROUTED_EXCEPTION_TYPE = "Resolver404"


def is_only_unrouted(exception_values: Optional[List[Dict[str, Any]]]) -> bool:
    """True when an event is *nothing but* URL-resolution 404s.

    Scanner probes (``/.env``, ``/key.pem``) produce a lone Resolver404, and
    those are pure noise -- Django refused them, and each new probe path
    fingerprints as a brand-new Sentry issue.

    Sentry reports chained exceptions as several entries in ``values``, so a
    real failure that merely passed through URL resolution would also carry a
    Resolver404. Requiring EVERY entry to be Resolver404 keeps those events:
    dropping on "any" would silently swallow the genuine error alongside it.

    An empty or missing list is never suppressed. ``all([])`` is True, so
    checking emptiness first is what stops message-only events (no exception
    at all) from being mistaken for a 404.
    """
    if not exception_values:
        return False
    return all(exc.get("type") == UNROUTED_EXCEPTION_TYPE for exc in exception_values)

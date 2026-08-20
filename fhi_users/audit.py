"""
Simple audit logging for security and analytics.

This module provides a straightforward audit logging system that tracks:
- Authentication events (login, logout, failed attempts)
- API access patterns
- Important user actions

Design goals:
- Simple: One model, minimal dependencies
- Privacy-aware: Professionals get full logs, consumers get minimal logs
- Optional: Controlled by ENABLE_AUDIT_LOGGING setting
"""

import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, Protocol, Any
import typing

from django.conf import settings
from django.db import models
from django.http import HttpRequest

from django.contrib.auth.models import AnonymousUser
from loguru import logger

if typing.TYPE_CHECKING:
    from rest_framework.request import Request as DRFRequest


class HasTrackingFields(Protocol):
    """Protocol for objects that have tracking fields."""

    user_agent: str
    asn: str
    asn_name: str
    ip_address: Optional[str]


class EventType(str, Enum):
    """Types of events we track."""

    # Authentication
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"

    # Account
    ACCOUNT_CREATED = "account_created"

    # API access
    API_ACCESS = "api_access"

    # Security
    PERMISSION_DENIED = "permission_denied"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    EXCEPTION_ERROR = "exception_error"


class AuditLog(models.Model):
    """
    Simple audit log entry.

    Stores essential information about user actions for security and analytics.
    """

    id = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # What happened
    event_type = models.CharField(max_length=50, db_index=True)
    description = models.TextField(blank=True, default="")

    # Who did it
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    username = models.CharField(max_length=255, blank=True, default="")
    is_professional = models.BooleanField(default=False)

    # Where from (privacy-aware: only store for professionals)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")

    # Request context
    path = models.CharField(max_length=2048, blank=True, default="")
    method = models.CharField(max_length=10, blank=True, default="")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)

    # Extra data (JSON)
    extra_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["user", "timestamp"]),
            models.Index(fields=["event_type", "timestamp"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.event_type} by {self.username or 'anonymous'} at {self.timestamp}"
        )


def is_audit_enabled() -> bool:
    """Check if audit logging is enabled."""
    return getattr(settings, "ENABLE_AUDIT_LOGGING", False)


def get_client_ip(request: Union[HttpRequest, "DRFRequest"]) -> Optional[str]:
    """Extract client IP from request, handling proxies."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = str(x_forwarded_for).split(",")[0].strip()
        if ip:
            return ip

    x_real_ip = request.META.get("HTTP_X_REAL_IP")
    if x_real_ip:
        return str(x_real_ip).strip()

    remote_addr = request.META.get("REMOTE_ADDR")
    return str(remote_addr) if remote_addr else None


# Client IPs come from the unvalidated, client-controlled X-Forwarded-For header
# and are stored in CharField(max_length=64) columns (and Stripe metadata, which
# Stripe caps at 500 chars). Bound the value so a garbage/over-long header can't
# overflow the column or the metadata limit and fail the write.
CLIENT_IP_MAX_LENGTH = 64


def bound_client_ip(ip: Optional[str]) -> Optional[str]:
    """Truncate a header-derived IP to the stored column width, preserving None.

    The value is stored verbatim (never validated as a real IP) so spoofed/
    malformed input is retained for abuse investigation; only its length is
    bounded so the write can't fail.
    """
    if not ip:
        return None
    return ip[:CLIENT_IP_MAX_LENGTH]


def is_professional_user(user) -> bool:
    """Check if user is a professional (for determining data retention level)."""
    if not user or isinstance(user, AnonymousUser):
        return False

    # Cache the professional status on the user instance to avoid repeated queries.
    cached_value = getattr(user, "_is_professional_cached", None)
    if cached_value is not None:
        return bool(cached_value)

    try:
        from .models import ProfessionalUser

        is_professional = ProfessionalUser.objects.filter(user=user).exists()
    except Exception:
        is_professional = False

    # Store the result on the user object for subsequent calls in this process/request.
    try:
        setattr(user, "_is_professional_cached", is_professional)
    except Exception:
        # If the user instance is immutable or doesn't allow attribute setting,
        # silently ignore caching and just return the computed value.
        pass

    return is_professional


def log_event(
    event_type: Union[EventType, str],
    request: Optional[Union[HttpRequest, "DRFRequest"]] = None,
    user=None,
    description: str = "",
    status_code: Optional[int] = None,
    response_time_ms: Optional[int] = None,
    extra_data: Optional[dict] = None,
) -> Optional[AuditLog]:
    """
    Log an audit event.

    Args:
        event_type: Type of event (use EventType enum)
        request: HTTP request object (optional)
        user: User object (optional, extracted from request if not provided)
        description: Human-readable description
        status_code: HTTP status code (for API events)
        response_time_ms: Response time in milliseconds
        extra_data: Additional JSON data to store

    Returns:
        Created AuditLog or None if logging is disabled
    """
    if not is_audit_enabled():
        return None

    try:
        # Get user from request if not provided
        if user is None and request is not None:
            user = getattr(request, "user", None)
            if isinstance(user, AnonymousUser):
                user = None

        # Determine if professional (affects what we store)
        is_pro = is_professional_user(user)

        # Build log entry
        log_entry = AuditLog(
            event_type=str(
                event_type.value if isinstance(event_type, EventType) else event_type
            ),
            description=description,
            user=user if user and not isinstance(user, AnonymousUser) else None,
            username=getattr(user, "username", "") if user else "",
            is_professional=is_pro,
            extra_data=extra_data or {},
        )

        # Add request context
        if request is not None:
            log_entry.path = request.path[:2048] if request.path else ""
            log_entry.method = request.method or ""

            # Always store user_agent (not privacy-sensitive)
            log_entry.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

            # Only store IP for professionals (privacy-sensitive)
            if is_pro:
                log_entry.ip_address = get_client_ip(request)

        if status_code is not None:
            log_entry.status_code = status_code
        if response_time_ms is not None:
            log_entry.response_time_ms = response_time_ms

        log_entry.save()
        return log_entry

    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to create audit log: {e}")
        return None


# Convenience functions
def log_login_success(request, user) -> Optional[AuditLog]:
    """Log successful login."""
    return log_event(EventType.LOGIN_SUCCESS, request=request, user=user)


def log_login_failure(
    request, username: str = "", reason: str = ""
) -> Optional[AuditLog]:
    """Log failed login attempt."""
    return log_event(
        EventType.LOGIN_FAILED,
        request=request,
        description=(
            f"Failed login for {username}: {reason}"
            if reason
            else f"Failed login for {username}"
        ),
        extra_data={"attempted_username": username, "reason": reason},
    )


def log_logout(request, user) -> Optional[AuditLog]:
    """Log user logout."""
    return log_event(EventType.LOGOUT, request=request, user=user)


def log_exception_error(
    request,
    error_type: str = "",
    error_message: str = "",
) -> Optional[AuditLog]:
    """Log an API exception event with request metadata for debugging."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "") if request else ""
    extra_data = {
        "error_type": error_type[:200],
        "error_message": error_message[:1000],
        "query_string": (request.META.get("QUERY_STRING", "") if request else "")[
            :1000
        ],
        "remote_addr": (request.META.get("REMOTE_ADDR", "") if request else "")[:100],
        "x_forwarded_for": str(x_forwarded_for)[:500],
        "x_real_ip": (request.META.get("HTTP_X_REAL_IP", "") if request else "")[:100],
    }

    return log_event(
        EventType.EXCEPTION_ERROR,
        request=request,
        status_code=500,
        description=f"Unhandled exception: {error_type}"[:1000],
        extra_data=extra_data,
    )


def log_api_access(
    request,
    status_code: Optional[int] = None,
    response_time_ms: Optional[int] = None,
) -> Optional[AuditLog]:
    """Log API endpoint access."""
    return log_event(
        EventType.API_ACCESS,
        request=request,
        status_code=status_code,
        response_time_ms=response_time_ms,
    )


# Tracking utilities for denial/chat flows


def get_user_agent(request: Union[HttpRequest, "DRFRequest"]) -> str:
    """Extract user agent from request."""
    ua = request.META.get("HTTP_USER_AGENT", "")
    # Truncate to reasonable length
    return str(ua)[:500] if ua else ""


def get_asn_info(ip_address: Optional[str]) -> tuple[str, str]:
    """
    Get ASN information for an IP address.

    Returns:
        Tuple of (asn_number, asn_name). Both empty strings if lookup fails.

    Uses the shared cached geoip2fast reader (see _get_geo_reader), which is
    only constructed when FHI_GEOIP_CITY_DB is set. Point it at an ASN-bearing
    database (geoip2fast-city-asn-ipv6.dat.gz) to get ASN data alongside the
    state guess; with the key unset this returns empty strings — the
    pre-geoip2fast behavior.
    """
    if not ip_address:
        return ("", "")

    reader = _get_geo_reader()
    if reader is None:
        return ("", "")
    try:
        result = reader.lookup(str(ip_address).strip())
        if result is None:
            return ("", "")
        # geoip2fast exposes asn_name (and asn_cidr); some builds/forks also
        # expose a numeric asn. Probe defensively.
        asn_num = str(getattr(result, "asn", "") or "")
        asn_name = str(getattr(result, "asn_name", "") or "")[:200]
        return (asn_num, asn_name)
    except Exception:
        # Any lookup failure, return empty
        return ("", "")


# --- IP -> US state guess (for chat context, transient only) ---

# 2-letter postal code -> full state name, used to normalize whatever the geo
# database returns into the full name the chat/Medicaid tooling expects.
_US_STATE_NAMES_BY_CODE = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_US_STATE_NAMES = {name.lower(): name for name in _US_STATE_NAMES_BY_CODE.values()}

# The configuration key that switches IP geo lookups on: path to a
# geoip2fast CITY-level database (state guessing needs subdivision data,
# which the package's bundled country/ASN databases do not carry). Use
# geoip2fast-city-asn-ipv6.dat.gz to also get ASN data for get_asn_info.
# See README "GeoIP" section. Without it, guess_us_state and get_asn_info
# soft-fail to None/empty — a startup warning names this key.
GEOIP_CITY_DB_ENV = "FHI_GEOIP_CITY_DB"

# Lazily-created, cached geo reader (loading the database per lookup would be
# far too slow for a per-message call: the city database takes seconds to
# load). _geo_reader_failed remembers a failed init so we don't retry the
# import/load on every chat message. The FIRST call still pays the load, so
# async callers must wrap lookups in a thread (the chat consumer does).
_geo_reader: Any = None
_geo_reader_failed = False
_geo_reader_lock = threading.Lock()


def _get_geo_reader() -> Any:
    global _geo_reader, _geo_reader_failed
    if _geo_reader is not None or _geo_reader_failed:
        return _geo_reader
    with _geo_reader_lock:
        if _geo_reader is not None or _geo_reader_failed:
            return _geo_reader
        # Deliberately gated on the env key rather than falling back to the
        # package's bundled country database: without city data the state
        # guess can never work, and silently loading a database that can't
        # serve the feature just hides the misconfiguration (the startup
        # check warns instead).
        db_path = os.environ.get(GEOIP_CITY_DB_ENV)
        if not db_path:
            _geo_reader_failed = True
            return None
        try:
            from geoip2fast import GeoIP2Fast

            _geo_reader = GeoIP2Fast(geoip2fast_data_file=db_path)
        except Exception:
            # Missing package or unreadable database file: geo features are
            # an optional enhancement and must never break request handling.
            logger.opt(exception=True).warning(
                f"Failed to load GeoIP database from {GEOIP_CITY_DB_ENV}="
                f"{db_path!r}; IP state guessing and ASN lookups disabled."
            )
            _geo_reader_failed = True
            _geo_reader = None
    return _geo_reader


def _reset_geo_reader_cache_for_tests() -> None:
    """Test hook: clear the cached geo reader / failure flag."""
    global _geo_reader, _geo_reader_failed
    with _geo_reader_lock:
        _geo_reader = None
        _geo_reader_failed = False


def geo_lookup_status() -> tuple[bool, Optional[str]]:
    """Whether IP geo lookups (state guess + ASN) are available, and why not.

    Cheap enough for startup: it checks the key and the package import but
    does NOT load the database (that happens lazily on first lookup).

    Returns:
        (enabled, reason) — reason is None when enabled, otherwise a short
        human-readable explanation naming the fix.
    """
    db_path = os.environ.get(GEOIP_CITY_DB_ENV)
    if not db_path:
        return (
            False,
            f"{GEOIP_CITY_DB_ENV} is not set (path to a geoip2fast city-level "
            f"database, e.g. geoip2fast-city-asn-ipv6.dat.gz)",
        )
    try:
        import geoip2fast  # noqa: F401
    except ImportError:
        return (False, "the geoip2fast package is not installed")
    if not os.path.exists(db_path):
        return (False, f"{GEOIP_CITY_DB_ENV}={db_path!r} does not exist")
    return (True, None)


_geo_startup_warning_emitted = False


def warn_if_geo_lookups_disabled() -> None:
    """Log a one-line startup warning when geo lookups are disabled.

    Soft fail by design: the chat state guess and ASN tracking silently
    degrade without the database, and this warning is the only signal — so
    it names the exact key to set. Called from AppConfig.ready(); guarded so
    repeat ready() calls (tests, autoreload) warn once per process.
    """
    global _geo_startup_warning_emitted
    if _geo_startup_warning_emitted:
        return
    _geo_startup_warning_emitted = True
    enabled, reason = geo_lookup_status()
    if enabled:
        logger.info(
            f"GeoIP lookups enabled ({GEOIP_CITY_DB_ENV}="
            f"{os.environ.get(GEOIP_CITY_DB_ENV)!r})"
        )
        return
    logger.warning(
        f"GeoIP lookups disabled: {reason}. Chat IP-based state guessing "
        f"and ASN tracking will soft-fail (return nothing). See the README "
        f"'GeoIP' section for setup."
    )


def _normalize_state_guess(value: str) -> Optional[str]:
    """Map a raw subdivision value (code or name) to a full US state name."""
    cleaned = value.strip()
    if not cleaned:
        return None
    # Some databases prefix subdivision codes with the country ("US-CA").
    if "-" in cleaned:
        cleaned = cleaned.rsplit("-", 1)[-1].strip()
    if len(cleaned) == 2:
        return _US_STATE_NAMES_BY_CODE.get(cleaned.upper())
    return _US_STATE_NAMES.get(cleaned.lower())


def guess_us_state(ip_address: Optional[str]) -> Optional[str]:
    """Best-effort guess of the US state for an IP address, or None.

    Used to seed the chat with an UNCONFIRMED location hint so the model can
    ask "it looks like you might be in California, is that right?" instead of
    a cold "which state are you in?". Requires geoip2fast with a city-level
    database (see _get_geo_reader); returns None (never raises) when the
    dependency, the database, or subdivision data is unavailable, when the IP
    isn't clearly US, or on any lookup error. Callers must treat the value as
    a transient guess: present it to the model as unconfirmed and never
    persist it for anonymous users.
    """
    if not ip_address:
        return None
    reader = _get_geo_reader()
    if reader is None:
        return None
    try:
        result = reader.lookup(str(ip_address).strip())
    except Exception:
        return None
    if result is None:
        return None
    country = str(getattr(result, "country_code", "") or "").strip().upper()
    if country and country != "US":
        return None
    # Probe the field shapes different geoip2fast versions/databases use for
    # subdivision info, on both the result and its nested city object.
    city_obj = getattr(result, "city", None)
    for source in (result, city_obj):
        if source is None:
            continue
        for attr in (
            "subdivision_code",
            "subdivision_name",
            "region_code",
            "region_name",
            "state",
            "state_code",
            "state_name",
        ):
            raw = getattr(source, attr, None)
            if isinstance(raw, str) and raw.strip():
                state = _normalize_state_guess(raw)
                if state:
                    return state
    return None


def tracking_metadata_for_request(
    request: Optional[Union[HttpRequest, "DRFRequest"]],
) -> dict[str, str]:
    """Return Stripe-metadata-safe client provenance (ip/asn) for ``request``.

    Threaded into checkout-session metadata at creation time so an expired
    session -- whose webhook arrives from Stripe, not the user -- can still be
    tied back to the originating client for abandoned-checkout abuse
    investigation. Returns only the populated fields; all values are strings
    because Stripe metadata values must be strings. The full IP is included
    deliberately (see LostStripeSession.ip_address).
    """
    md: dict[str, str] = {}
    if request is None:
        return md
    ip = get_client_ip(request)
    if ip:
        # Bound the client-controlled value (see bound_client_ip) so an
        # over-long/garbage header can't exceed Stripe's 500-char metadata limit
        # (which would break checkout creation) or overflow the
        # LostStripeSession.ip_address column it is later persisted into.
        md["ip_address"] = ip[:CLIENT_IP_MAX_LENGTH]
    asn, asn_name = get_asn_info(ip)
    if asn:
        md["asn"] = asn
    if asn_name:
        md["asn_name"] = asn_name
    return md


@dataclass
class TrackingInfo:
    """Container for request tracking information."""

    user_agent: str = ""
    ip_address: Optional[str] = None
    asn: str = ""
    asn_name: str = ""

    def to_model_kwargs(self) -> dict[str, Any]:
        """Convert tracking information to kwargs for model creation.

        Returns:
            Dictionary with keys: 'user_agent' (str), 'asn' (str),
            'asn_name' (str), 'ip_address' (Optional[str])
        """
        return {
            "user_agent": self.user_agent,
            "asn": self.asn,
            "asn_name": self.asn_name,
            "ip_address": bound_client_ip(self.ip_address),
        }

    def update_model_fields(self, model_instance: HasTrackingFields) -> None:
        """Update tracking fields on a model instance.

        Args:
            model_instance: Object with user_agent, asn, asn_name, and
                ip_address attributes to update
        """
        model_instance.user_agent = self.user_agent
        model_instance.asn = self.asn
        model_instance.asn_name = self.asn_name
        model_instance.ip_address = bound_client_ip(self.ip_address)


def extract_tracking_info(
    request: Optional[Union[HttpRequest, "DRFRequest"]] = None,
    is_professional: bool = False,
) -> TrackingInfo:
    """
    Extract tracking information from a request.

    Args:
        request: HTTP request object
        is_professional: If True, store IP address. Otherwise, only ASN-level info.

    Returns:
        TrackingInfo with user agent, ASN, and optionally IP address
    """
    if request is None:
        return TrackingInfo()

    user_agent = get_user_agent(request)
    ip_address = get_client_ip(request)

    # Get ASN info for privacy-preserving location tracking
    asn, asn_name = get_asn_info(ip_address)

    return TrackingInfo(
        user_agent=user_agent,
        # Only store IP for professionals
        ip_address=ip_address if is_professional else None,
        asn=asn,
        asn_name=asn_name,
    )


def extract_tracking_info_from_scope(
    scope: Optional[dict] = None,
    is_professional: bool = False,
) -> TrackingInfo:
    """
    Extract tracking information from a websocket scope.

    Args:
        scope: Websocket scope dict (from Django Channels)
        is_professional: If True, store IP address. Otherwise, only ASN-level info.

    Returns:
        TrackingInfo with user agent, ASN, and optionally IP address
    """
    if scope is None:
        return TrackingInfo()

    # Extract user agent from headers
    user_agent = ""
    headers = dict(scope.get("headers", []))
    if b"user-agent" in headers:
        user_agent = headers[b"user-agent"].decode("utf-8", errors="ignore")[:500]

    # Extract IP from scope
    ip_address: Optional[str] = None
    # Check X-Forwarded-For first (for proxied connections)
    if b"x-forwarded-for" in headers:
        forwarded = headers[b"x-forwarded-for"].decode("utf-8", errors="ignore")
        ip_address = forwarded.split(",")[0].strip()
    elif b"x-real-ip" in headers:
        ip_address = headers[b"x-real-ip"].decode("utf-8", errors="ignore").strip()
    else:
        # Fall back to client address from scope
        client = scope.get("client")
        if client:
            ip_address = client[0]

    # Get ASN info for privacy-preserving location tracking
    asn, asn_name = get_asn_info(ip_address)

    return TrackingInfo(
        user_agent=user_agent,
        # Only store IP for professionals
        ip_address=ip_address if is_professional else None,
        asn=asn,
        asn_name=asn_name,
    )

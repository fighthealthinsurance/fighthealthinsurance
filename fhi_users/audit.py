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

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union
import typing

from django.conf import settings
from django.db import models
from django.http import HttpRequest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from loguru import logger

if typing.TYPE_CHECKING:
    from rest_framework.request import Request as DRFRequest


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
        return f"{self.event_type} by {self.username or 'anonymous'} at {self.timestamp}"


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


def is_professional_user(user) -> bool:
    """Check if user is a professional (for determining data retention level)."""
    if not user or isinstance(user, AnonymousUser):
        return False
    try:
        from .models import ProfessionalUser

        return ProfessionalUser.objects.filter(user=user).exists()
    except Exception:
        return False


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
            event_type=str(event_type.value if isinstance(event_type, EventType) else event_type),
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

            # Only store IP/UA for professionals (privacy)
            if is_pro:
                log_entry.ip_address = get_client_ip(request)
                log_entry.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

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


def log_login_failure(request, username: str = "", reason: str = "") -> Optional[AuditLog]:
    """Log failed login attempt."""
    return log_event(
        EventType.LOGIN_FAILED,
        request=request,
        description=f"Failed login for {username}: {reason}" if reason else f"Failed login for {username}",
        extra_data={"attempted_username": username, "reason": reason},
    )


def log_logout(request, user) -> Optional[AuditLog]:
    """Log user logout."""
    return log_event(EventType.LOGOUT, request=request, user=user)


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

    Note: This is a placeholder that can be enhanced with geoip2fast or similar.
    For now, returns empty strings. To enable ASN lookup, install geoip2fast
    and download the ASN database.
    """
    if not ip_address:
        return ("", "")

    try:
        # Try to import geoip2fast if available
        from geoip2fast import GeoIP2Fast

        geoip = GeoIP2Fast()
        result = geoip.lookup(ip_address)
        if result and hasattr(result, "asn"):
            asn_num = str(result.asn) if result.asn else ""
            asn_name = str(result.asn_name)[:200] if result.asn_name else ""
            return (asn_num, asn_name)
    except ImportError:
        # geoip2fast not installed, that's fine
        pass
    except Exception:
        # Any lookup failure, return empty
        pass

    return ("", "")


@dataclass
class TrackingInfo:
    """Container for request tracking information."""

    user_agent: str = ""
    ip_address: Optional[str] = None
    asn: str = ""
    asn_name: str = ""

    def to_model_kwargs(self) -> dict:
        """
        Convert tracking information to kwargs for model creation.
        
        Returns:
            Dictionary with tracking fields suitable for unpacking into model kwargs
        """
        return {
            "user_agent": self.user_agent,
            "asn": self.asn,
            "asn_name": self.asn_name,
            "ip_address": self.ip_address,
        }

    def update_model_fields(self, model_instance: typing.Any) -> None:
        """
        Update tracking fields on a model instance.
        
        Args:
            model_instance: Model instance with user_agent, asn, asn_name, 
                          and ip_address attributes to update
        """
        model_instance.user_agent = self.user_agent
        model_instance.asn = self.asn
        model_instance.asn_name = self.asn_name
        model_instance.ip_address = self.ip_address


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

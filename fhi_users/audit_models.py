"""
Database-backed audit logging models for security, analytics, and compliance.

Design Philosophy:
- Professional users: Full audit trail with IP, ASN, location, full user agent
- Consumer users: Privacy-preserving (ASN only, no IP, simplified user agent)
- All users: Track behavioral patterns that might indicate competitive intelligence

The models support:
1. Authentication events (login, logout, failed attempts)
2. API access patterns (for detecting scraping/bulk access)
3. Permission changes (role escalation, access grants)
4. Suspicious activity detection (datacenter IPs, unusual patterns)
"""

import typing
from enum import Enum
from django.db import models
from django.contrib.auth import get_user_model

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class AuditEventType(str, Enum):
    """Types of events we track in audit logs."""

    # Authentication events
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    EMAIL_VERIFIED = "email_verified"
    MFA_ENABLED = "mfa_enabled"
    MFA_DISABLED = "mfa_disabled"

    # Account lifecycle
    ACCOUNT_CREATED = "account_created"
    ACCOUNT_ACTIVATED = "account_activated"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_DELETED = "account_deleted"

    # Professional-specific events
    PROFESSIONAL_CREATED = "professional_created"
    PROFESSIONAL_ACCEPTED = "professional_accepted"
    PROFESSIONAL_REJECTED = "professional_rejected"
    PROFESSIONAL_SUSPENDED = "professional_suspended"
    ADMIN_GRANTED = "admin_granted"
    ADMIN_REVOKED = "admin_revoked"
    DOMAIN_JOINED = "domain_joined"
    DOMAIN_LEFT = "domain_left"

    # API/Data access events
    API_ACCESS = "api_access"
    BULK_EXPORT = "bulk_export"
    SEARCH_PERFORMED = "search_performed"

    # Security events
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"


class NetworkType(str, Enum):
    """Classification of network/ASN type for detecting non-human traffic."""

    RESIDENTIAL = "residential"
    BUSINESS = "business"
    DATACENTER = "datacenter"  # Cloud providers, hosting
    VPN = "vpn"  # Known VPN providers
    PROXY = "proxy"  # Known proxy services
    TOR = "tor"  # Tor exit nodes
    MOBILE = "mobile"  # Mobile carrier
    EDUCATION = "education"  # Universities, schools
    GOVERNMENT = "government"
    UNKNOWN = "unknown"


class UserType(str, Enum):
    """Type of user for determining what data to store."""

    CONSUMER = "consumer"  # Privacy-preserving logging
    PROFESSIONAL = "professional"  # Full audit trail
    ANONYMOUS = "anonymous"  # Unauthenticated requests


class BaseAuditLog(models.Model):
    """
    Abstract base class for audit logs with common fields.

    All audit logs share these core fields for consistent querying and retention.
    """

    id = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # User identification (nullable for anonymous/failed auth)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_logs",
    )
    user_type = models.CharField(
        max_length=20,
        choices=[(t.value, t.value) for t in UserType],
        default=UserType.ANONYMOUS.value,
    )

    # Session tracking
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)

    # Event classification
    event_type = models.CharField(
        max_length=50,
        choices=[(e.value, e.value) for e in AuditEventType],
        db_index=True,
    )
    success = models.BooleanField(default=True)

    # Network information (stored based on user_type)
    # For professionals: full IP stored
    # For consumers: NULL (privacy)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    # ASN info (stored for all users - not personally identifiable)
    asn_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    asn_org = models.CharField(max_length=255, null=True, blank=True)
    network_type = models.CharField(
        max_length=20,
        choices=[(t.value, t.value) for t in NetworkType],
        default=NetworkType.UNKNOWN.value,
    )

    # Location (granularity varies by user_type)
    # Professionals: country + state
    # Consumers: country only
    country_code = models.CharField(max_length=2, null=True, blank=True, db_index=True)
    state_region = models.CharField(max_length=100, null=True, blank=True)

    # User agent (granularity varies by user_type)
    # Professionals: full user agent
    # Consumers: simplified (browser family + OS family)
    user_agent_full = models.TextField(null=True, blank=True)
    user_agent_browser = models.CharField(max_length=50, null=True, blank=True)
    user_agent_os = models.CharField(max_length=50, null=True, blank=True)
    is_mobile = models.BooleanField(null=True, blank=True)
    is_bot = models.BooleanField(default=False)

    # Request context
    request_path = models.CharField(max_length=500, null=True, blank=True)
    request_method = models.CharField(max_length=10, null=True, blank=True)
    http_referer = models.URLField(max_length=1000, null=True, blank=True)

    # Flexible metadata for event-specific data
    details = models.JSONField(null=True, blank=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=["timestamp", "event_type"]),
            models.Index(fields=["user", "timestamp"]),
            models.Index(fields=["asn_number", "timestamp"]),
            models.Index(fields=["network_type", "timestamp"]),
        ]


class AuthAuditLog(BaseAuditLog):
    """
    Audit log specifically for authentication events.

    Tracks login attempts, logouts, password changes, etc.
    Critical for security monitoring and detecting credential stuffing.
    """

    # Professional/Domain context
    professional_user = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_audit_logs",
    )
    domain = models.ForeignKey(
        "fhi_users.UserDomain",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_audit_logs",
    )

    # Auth-specific fields
    attempted_email = models.EmailField(
        null=True, blank=True
    )  # For failed logins where user not found
    failure_reason = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        verbose_name = "Authentication Audit Log"
        verbose_name_plural = "Authentication Audit Logs"
        indexes = [
            *BaseAuditLog.Meta.indexes,
            models.Index(fields=["attempted_email", "timestamp"]),
            models.Index(fields=["domain", "timestamp"]),
            models.Index(fields=["ip_address", "timestamp"]),
        ]

    def __str__(self) -> str:
        user_str = self.user.email if self.user else self.attempted_email or "unknown"
        return f"{self.event_type} - {user_str} at {self.timestamp}"


class APIAccessLog(BaseAuditLog):
    """
    Audit log for API endpoint access.

    Used to detect:
    - Scraping behavior (high volume, systematic access patterns)
    - Competitive intelligence (specific endpoint patterns)
    - Abuse patterns (bulk data access)
    """

    # Professional/Domain context
    professional_user = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_audit_logs",
    )
    domain = models.ForeignKey(
        "fhi_users.UserDomain",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_audit_logs",
    )

    # API-specific fields
    endpoint = models.CharField(max_length=255, db_index=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)

    # Resource tracking (what was accessed)
    resource_type = models.CharField(
        max_length=50, null=True, blank=True
    )  # e.g., "denial", "appeal", "chat"
    resource_id = models.CharField(max_length=100, null=True, blank=True)
    resource_count = models.PositiveIntegerField(
        null=True, blank=True
    )  # For list endpoints

    # Query/search tracking
    search_query = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        verbose_name = "API Access Log"
        verbose_name_plural = "API Access Logs"
        indexes = [
            *BaseAuditLog.Meta.indexes,
            models.Index(fields=["endpoint", "timestamp"]),
            models.Index(fields=["domain", "endpoint", "timestamp"]),
            models.Index(fields=["resource_type", "timestamp"]),
        ]

    def __str__(self) -> str:
        user_str = self.user.email if self.user else "anonymous"
        return f"{self.request_method} {self.endpoint} - {user_str} at {self.timestamp}"


class ProfessionalActivityLog(BaseAuditLog):
    """
    Audit log for professional user management activities.

    Tracks permission changes, role escalations, and domain membership.
    Critical for compliance and detecting insider threats.
    """

    # Required professional context
    professional_user = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_audit_logs",
    )
    domain = models.ForeignKey(
        "fhi_users.UserDomain",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_audit_logs",
    )

    # Who performed the action (may differ from the affected user)
    performed_by_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_activity_logs",
    )
    performed_by_professional = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_by_activity_logs",
    )

    # Target of the action (for actions affecting another user)
    target_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="target_activity_logs",
    )
    target_professional = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="target_activity_logs",
    )

    # Change tracking
    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Professional Activity Log"
        verbose_name_plural = "Professional Activity Logs"
        indexes = [
            *BaseAuditLog.Meta.indexes,
            models.Index(fields=["professional_user", "timestamp"]),
            models.Index(fields=["performed_by_user", "timestamp"]),
            models.Index(fields=["target_user", "timestamp"]),
        ]

    def __str__(self) -> str:
        performer = (
            self.performed_by_user.email if self.performed_by_user else "system"
        )
        return f"{self.event_type} by {performer} at {self.timestamp}"


class SuspiciousActivityLog(models.Model):
    """
    Aggregated log for flagged suspicious activity patterns.

    This is populated by background analysis jobs that look for:
    - Datacenter/VPN access patterns
    - Unusual access times
    - Bulk download behavior
    - Failed authentication patterns
    """

    id = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # What triggered the flag
    trigger_type = models.CharField(max_length=50, db_index=True)
    severity = models.CharField(
        max_length=20,
        choices=[
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        default="low",
    )

    # Associated user/domain if known
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_activity_logs",
    )
    professional_user = models.ForeignKey(
        "fhi_users.ProfessionalUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_activity_logs",
    )
    domain = models.ForeignKey(
        "fhi_users.UserDomain",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_activity_logs",
    )

    # Network info
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    asn_number = models.PositiveIntegerField(null=True, blank=True)
    asn_org = models.CharField(max_length=255, null=True, blank=True)

    # Details about what was flagged
    description = models.TextField()
    evidence = models.JSONField(null=True, blank=True)  # Supporting data

    # Resolution tracking
    reviewed = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_suspicious_logs",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "Suspicious Activity Log"
        verbose_name_plural = "Suspicious Activity Logs"
        indexes = [
            models.Index(fields=["timestamp", "severity"]),
            models.Index(fields=["user", "timestamp"]),
            models.Index(fields=["trigger_type", "timestamp"]),
            models.Index(fields=["reviewed", "severity"]),
        ]

    def __str__(self) -> str:
        user_str = self.user.email if self.user else "unknown"
        return f"{self.severity} - {self.trigger_type} - {user_str} at {self.timestamp}"


# Known datacenter/cloud provider ASNs for flagging non-residential traffic
KNOWN_DATACENTER_ASNS: set[int] = {
    # Major cloud providers
    16509,  # Amazon AWS
    14618,  # Amazon AWS
    15169,  # Google Cloud
    8075,  # Microsoft Azure
    396982,  # Google Cloud
    13335,  # Cloudflare
    20940,  # Akamai
    54113,  # Fastly
    14061,  # DigitalOcean
    63949,  # Linode
    20473,  # Vultr
    132203,  # Tencent Cloud
    45102,  # Alibaba Cloud
    # VPN/Proxy providers (partial list)
    9009,  # M247 (commonly used by VPNs)
    62041,  # NordVPN
    212238,  # NordVPN
    # Hosting companies
    24940,  # Hetzner
    16276,  # OVH
    51167,  # Contabo
}

# Known VPN provider ASNs
KNOWN_VPN_ASNS: set[int] = {
    62041,  # NordVPN
    212238,  # NordVPN
    9009,  # M247 (NordVPN, ExpressVPN)
    60068,  # CDN77 (often used by VPNs)
    207137,  # Private Internet Access
    46562,  # Proton VPN
}

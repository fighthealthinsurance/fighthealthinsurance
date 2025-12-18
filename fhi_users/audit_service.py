"""
Audit logging service for creating and managing audit log entries.

This module provides high-level functions for logging various events:
- Authentication (login, logout, failed attempts)
- API access
- Professional user management activities
- Suspicious activity flagging

Usage:
    from fhi_users.audit_service import audit_service

    # Log a successful login
    audit_service.log_login_success(request, user, domain)

    # Log a failed login attempt
    audit_service.log_login_failure(request, email, reason="invalid_password")

    # Log API access
    audit_service.log_api_access(request, endpoint="/api/v1/denials/", resource_type="denial")
"""

import typing
from datetime import datetime
from typing import Optional, Any, Union

from django.conf import settings
from django.http import HttpRequest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from loguru import logger

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
    from rest_framework.request import Request as DRFRequest
    from .models import ProfessionalUser, UserDomain
else:
    User = get_user_model()

# Type alias for request objects (Django HttpRequest or DRF Request)
# Both have compatible .META, .user, .session, .path, .method attributes
RequestType = Union[HttpRequest, "DRFRequest"]

# Type alias for user objects that can come from request.user
UserOrAbstract = Union["User", AbstractBaseUser, AnonymousUser, None]

from .audit_models import (
    AuditEventType,
    AuthAuditLog,
    APIAccessLog,
    ProfessionalActivityLog,
    SuspiciousActivityLog,
    ObjectActivityContext,
    UserType,
    NetworkType,
)
from .audit_utils import (
    get_client_ip,
    get_request_context,
    determine_user_type,
    sanitize_for_privacy,
    IPInfo,
    UserAgentInfo,
)


class AuditService:
    """
    Service class for creating audit log entries.

    Handles privacy considerations automatically based on user type.
    Respects the ENABLE_AUDIT_LOGGING feature flag.
    """

    def _is_enabled(self) -> bool:
        """
        Check if audit logging is enabled via the feature flag.

        Returns:
            bool: True if audit logging is enabled, False otherwise.
        """
        return getattr(settings, 'ENABLE_AUDIT_LOGGING', False)

    def _get_professional_user(
        self, user: UserOrAbstract
    ) -> Optional["ProfessionalUser"]:
        """
        Return the ProfessionalUser associated with the given Django User if present.

        Returns:
            The matching `ProfessionalUser` instance, or `None` if no user is provided or no associated professional user exists.
        """
        if not user or isinstance(user, AnonymousUser):
            return None
        try:
            from .models import ProfessionalUser

            return ProfessionalUser.objects.filter(user=user).first()
        except Exception:
            return None

    def _get_domain_from_request(
        self, request: HttpRequest
    ) -> Optional["UserDomain"]:
        """
        Retrieve the UserDomain referenced in the request session, if present.

        Checks the request session for a "domain_id" and returns the corresponding `UserDomain` instance when found and resolvable.

        Returns:
            UserDomain: The matching `UserDomain` instance if `domain_id` exists and a record is found, `None` otherwise.
        """
        try:
            from .models import UserDomain

            domain_id = request.session.get("domain_id")
            if domain_id:
                return UserDomain.objects.filter(id=domain_id).first()
        except Exception:
            pass
        return None

    def _create_base_log_data(
        self,
        request: HttpRequest,
        event_type: AuditEventType,
        user: UserOrAbstract = None,
        success: bool = True,
    ) -> dict:
        """
        Assemble the base field values for an audit log entry, applying user-type aware privacy sanitization.

        Parameters:
            request (HttpRequest): Request used to derive client, session, IP, and user-agent context.
            event_type (AuditEventType): Audit event type to record.
            user (UserOrAbstract): Associated Django user; only included in output if authenticated.
            success (bool): Whether the audited event succeeded.

        Returns:
            dict: Mapping of audit model field names to values. Includes sanitized session_key, IP and ASN fields (IP may be None when redacted), network_type, country/state, user agent fields, request_path, request_method, http_referer, event_type, user (authenticated only), user_type, and success.
        """
        # Determine user type for privacy handling
        if user and hasattr(user, 'is_authenticated') and user.is_authenticated:
            user_type = determine_user_type(request)
        else:
            user = None
            user_type = UserType.ANONYMOUS

        # Get full context
        context = get_request_context(request)

        # Sanitize based on user type
        sanitized = sanitize_for_privacy(context, user_type)

        ip_info: IPInfo = sanitized["ip_info"]
        ua_info: UserAgentInfo = sanitized["ua_info"]

        return {
            "event_type": event_type.value,
            "user": user if user and hasattr(user, 'is_authenticated') and user.is_authenticated else None,
            "user_type": user_type.value,
            "success": success,
            "session_key": sanitized.get("session_key"),
            # IP info (may be None for consumers)
            "ip_address": ip_info.ip_address if ip_info.ip_address else None,
            "asn_number": ip_info.asn_number,
            "asn_org": ip_info.asn_org,
            "network_type": ip_info.network_type.value,
            "country_code": ip_info.country_code,
            "state_region": ip_info.state_region,
            # User agent info
            "user_agent_full": ua_info.full_user_agent if ua_info.full_user_agent else None,
            "user_agent_browser": ua_info.browser_family,
            "user_agent_os": ua_info.os_family,
            "is_mobile": ua_info.is_mobile,
            "is_bot": ua_info.is_bot,
            # Request context
            "request_path": sanitized.get("request_path"),
            "request_method": sanitized.get("request_method"),
            "http_referer": sanitized.get("http_referer"),
        }

    # ==================== Authentication Events ====================

    def log_login_success(
        self,
        request: HttpRequest,
        user: "User",
        domain: Optional["UserDomain"] = None,
        details: Optional[dict] = None,
    ) -> Optional[AuthAuditLog]:
        """
        Log a successful login event for the given user and request.

        Parameters:
            request (HttpRequest): The incoming HTTP request associated with the login.
            user (User): The user who successfully authenticated.
            domain (UserDomain, optional): The professional domain associated with the login (used for professional accounts).
            details (dict, optional): Arbitrary additional metadata to include in the audit entry.

        Returns:
            AuthAuditLog | None: The created AuthAuditLog entry, or `None` if logging failed or is disabled.
        """
        if not self._is_enabled():
            return None
        
        try:
            data = self._create_base_log_data(
                request, AuditEventType.LOGIN_SUCCESS, user, success=True
            )
            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = domain or self._get_domain_from_request(request)
            data["details"] = details

            log_entry = AuthAuditLog.objects.create(**data)

            # Check for suspicious patterns
            self._check_login_patterns(log_entry)

            return log_entry
        except Exception as e:
            logger.error(f"Failed to create login success audit log: {e}")
            return None

    def log_login_failure(
        self,
        request: HttpRequest,
        attempted_email: Optional[str] = None,
        failure_reason: str = "unknown",
        user: Optional["User"] = None,
        domain: Optional["UserDomain"] = None,
        details: Optional[dict] = None,
    ) -> Optional[AuthAuditLog]:
        """
        Record a failed login attempt as an audit entry.

        Creates an AuthAuditLog populated with the provided context, attempted email, failure reason, optional user and domain, and any additional details. For security, the request IP address is always stored for failed logins even if privacy rules would normally remove it. This action may trigger suspicious-activity detection for brute-force patterns.

        Parameters:
            request (HttpRequest): The incoming Django request associated with the attempt.
            attempted_email (Optional[str]): The email address used in the failed attempt, if any.
            failure_reason (str): Short identifier describing why authentication failed (e.g., "invalid_password", "user_not_found").
            user (Optional[User]): The resolved User when the account exists but authentication failed; omit if unknown.
            domain (Optional[UserDomain]): The UserDomain targeted by the attempt, if applicable.
            details (Optional[dict]): Arbitrary additional information to store with the log (debug or contextual data).

        Returns:
            AuthAuditLog | None: The created AuthAuditLog instance on success, or `None` if log creation failed or is disabled.
        """
        if not self._is_enabled():
            return None
        
        try:
            data = self._create_base_log_data(
                request, AuditEventType.LOGIN_FAILED, user, success=False
            )
            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = domain
            data["attempted_email"] = attempted_email
            data["failure_reason"] = failure_reason
            data["details"] = details

            # For failed logins, always store IP for security (even for consumers)
            # This is a security exception to privacy rules
            context = get_request_context(request)
            ip_info: IPInfo = context["ip_info"]
            data["ip_address"] = ip_info.ip_address

            log_entry = AuthAuditLog.objects.create(**data)

            # Check for brute force patterns
            self._check_failed_login_patterns(log_entry, attempted_email)

            return log_entry
        except Exception as e:
            logger.error(f"Failed to create login failure audit log: {e}")
            return None

    def log_logout(
        self,
        request: HttpRequest,
        user: "User",
        domain: Optional["UserDomain"] = None,
    ) -> Optional[AuthAuditLog]:
        """
        Create an audit log entry for a user logout.

        Parameters:
            request (HttpRequest): The incoming HTTP request associated with the logout.
            user (User): The user who is logging out.
            domain (Optional[UserDomain]): The professional domain associated with the logout, if applicable.

        Returns:
            AuthAuditLog | None: The created `AuthAuditLog` for the logout event, or `None` if the log could not be created or is disabled.
        """
        if not self._is_enabled():
            return None
        
        try:
            data = self._create_base_log_data(
                request, AuditEventType.LOGOUT, user, success=True
            )
            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = domain or self._get_domain_from_request(request)

            return AuthAuditLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create logout audit log: {e}")
            return None

    def log_password_changed(
        self,
        request: HttpRequest,
        user: "User",
        changed_by_self: bool = True,
    ) -> Optional[AuthAuditLog]:
        """
        Record an audit entry for a user's password change.

        Includes the professional user and request domain and records whether the change was self-initiated.

        Parameters:
            request (HttpRequest): The incoming HTTP request associated with the password change.
            user (User): The user whose password was changed.
            changed_by_self (bool): `True` if the user changed their own password, `False` if changed by another actor.

        Returns:
            AuthAuditLog | None: The created AuthAuditLog instance if successful, `None` if log creation failed.
        """
        try:
            data = self._create_base_log_data(
                request, AuditEventType.PASSWORD_CHANGED, user, success=True
            )
            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = self._get_domain_from_request(request)
            data["details"] = {"changed_by_self": changed_by_self}

            return AuthAuditLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create password change audit log: {e}")
            return None

    def log_account_created(
        self,
        request: HttpRequest,
        user: "User",
        account_type: str,  # "professional", "patient", etc.
        domain: Optional["UserDomain"] = None,
        details: Optional[dict] = None,
    ) -> Optional[AuthAuditLog]:
        """
        Record an account creation audit entry using request context and user information.

        Parameters:
            request (HttpRequest): The originating HTTP request used to derive context (IP, user agent, session, etc.).
            user (User): The Django user whose account was created.
            account_type (str): A string describing the type of account created (e.g., "professional", "patient").
            domain (Optional[UserDomain]): Optional domain associated with the account; when omitted, domain may be derived from the request if available.
            details (Optional[dict]): Additional arbitrary details to include in the log; merged into the stored `details` with `account_type`.

        Returns:
            AuthAuditLog | None: The created AuthAuditLog instance on success, or `None` if log creation failed.
        """
        try:
            data = self._create_base_log_data(
                request, AuditEventType.ACCOUNT_CREATED, user, success=True
            )
            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = domain
            data["details"] = {
                "account_type": account_type,
                **(details or {}),
            }

            return AuthAuditLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create account created audit log: {e}")
            return None

    # ==================== API Access Events ====================

    def log_api_access(
        self,
        request: HttpRequest,
        endpoint: str,
        http_status: Optional[int] = None,
        response_time_ms: Optional[int] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        resource_count: Optional[int] = None,
        search_query: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> Optional[APIAccessLog]:
        """
        Record an API access event with request context and optional metadata.

        Parameters:
                request (HttpRequest): The incoming Django request associated with the API call.
                endpoint (str): The API endpoint path or identifier that was accessed.
                http_status (Optional[int]): HTTP response status code; values < 400 are treated as successful.
                response_time_ms (Optional[int]): Response time in milliseconds.
                resource_type (Optional[str]): Logical type of the resource accessed (e.g., "user", "appointment").
                resource_id (Optional[str]): Identifier of the specific resource affected or returned.
                resource_count (Optional[int]): Number of resources returned for list endpoints.
                search_query (Optional[str]): Search or filter query string used with the request, if any.
                details (Optional[dict]): Arbitrary additional metadata to include in the log.

        Returns:
                APIAccessLog | None: The created APIAccessLog instance, or `None` if log creation failed or is disabled.
        """
        if not self._is_enabled():
            return None
        
        try:
            user = request.user if request.user.is_authenticated else None
            data = self._create_base_log_data(
                request, AuditEventType.API_ACCESS, user, success=(http_status or 200) < 400
            )

            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = self._get_domain_from_request(request)
            data["endpoint"] = endpoint
            data["http_status"] = http_status
            data["response_time_ms"] = response_time_ms
            data["resource_type"] = resource_type
            data["resource_id"] = resource_id
            data["resource_count"] = resource_count
            data["search_query"] = search_query
            data["details"] = details

            log_entry = APIAccessLog.objects.create(**data)

            # Check for suspicious API patterns
            self._check_api_patterns(log_entry)

            return log_entry
        except Exception as e:
            logger.error(f"Failed to create API access audit log: {e}")
            return None

    # ==================== Professional Activity Events ====================

    def log_professional_created(
        self,
        request: HttpRequest,
        professional_user: "ProfessionalUser",
        domain: "UserDomain",
        created_by: Optional["User"] = None,
        details: Optional[dict] = None,
    ) -> Optional[ProfessionalActivityLog]:
        """
        Record creation of a professional user along with the actor and request context.

        Parameters:
            request (HttpRequest): The originating HTTP request whose context will be recorded.
            professional_user (ProfessionalUser): The professional user that was created.
            domain (UserDomain): The domain/account associated with the created professional user.
            created_by (Optional[User]): The user who performed the creation; if omitted, the authenticated request user is used when available.
            details (Optional[dict]): Optional additional metadata about the creation event.

        Returns:
            ProfessionalActivityLog or None: The created ProfessionalActivityLog instance on success, `None` if log creation fails.
        """
        try:
            user = request.user if request.user.is_authenticated else None
            data = self._create_base_log_data(
                request, AuditEventType.PROFESSIONAL_CREATED, user, success=True
            )

            data["professional_user"] = professional_user
            data["domain"] = domain
            data["performed_by_user"] = created_by or user
            data["performed_by_professional"] = self._get_professional_user(created_by or user)
            data["target_user"] = professional_user.user
            data["target_professional"] = professional_user
            data["details"] = details

            return ProfessionalActivityLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create professional created audit log: {e}")
            return None

    def log_professional_accepted(
        self,
        request: HttpRequest,
        professional_user: "ProfessionalUser",
        domain: "UserDomain",
        accepted_by: "User",
    ) -> Optional[ProfessionalActivityLog]:
        """
        Record that a professional user was accepted into a domain by another user.

        Parameters:
            request (HttpRequest): The HTTP request that initiated the acceptance action.
            professional_user (ProfessionalUser): The professional user who was accepted.
            domain (UserDomain): The domain/context where the professional was accepted.
            accepted_by (User): The user who performed the acceptance action.

        Returns:
            ProfessionalActivityLog: The created audit log entry, or `None` if creation failed.
        """
        try:
            data = self._create_base_log_data(
                request, AuditEventType.PROFESSIONAL_ACCEPTED, accepted_by, success=True
            )

            data["professional_user"] = professional_user
            data["domain"] = domain
            data["performed_by_user"] = accepted_by
            data["performed_by_professional"] = self._get_professional_user(accepted_by)
            data["target_user"] = professional_user.user
            data["target_professional"] = professional_user

            return ProfessionalActivityLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create professional accepted audit log: {e}")
            return None

    def log_admin_granted(
        self,
        request: HttpRequest,
        professional_user: "ProfessionalUser",
        domain: "UserDomain",
        granted_by: "User",
    ) -> Optional[ProfessionalActivityLog]:
        """
        Record that a professional user was granted administrative privileges.

        Parameters:
                request (HttpRequest): The HTTP request that initiated the action.
                professional_user (ProfessionalUser): The professional user who received admin privileges.
                domain (UserDomain): The domain/context where the admin role was granted.
                granted_by (User): The user who granted the admin role.

        Returns:
                ProfessionalActivityLog | None: The created ProfessionalActivityLog instance, or `None` if creation failed.
        """
        try:
            data = self._create_base_log_data(
                request, AuditEventType.ADMIN_GRANTED, granted_by, success=True
            )

            data["professional_user"] = professional_user
            data["domain"] = domain
            data["performed_by_user"] = granted_by
            data["performed_by_professional"] = self._get_professional_user(granted_by)
            data["target_user"] = professional_user.user
            data["target_professional"] = professional_user
            data["old_values"] = {"admin": False}
            data["new_values"] = {"admin": True}

            return ProfessionalActivityLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create admin granted audit log: {e}")
            return None

    def log_permission_denied(
        self,
        request: HttpRequest,
        action: str,
        resource: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> Optional[ProfessionalActivityLog]:
        """
        Record a permission-denied professional activity for the current request.

        Parameters:
            request (HttpRequest): The incoming HTTP request that triggered the permission denial; the authenticated user (if any) is taken from this request.
            action (str): The action that was attempted and denied.
            resource (Optional[str]): Optional identifier or description of the resource on which the action was attempted.
            details (Optional[dict]): Optional additional metadata to include in the log; merged into the stored `details` field.

        Returns:
            ProfessionalActivityLog | None: The created ProfessionalActivityLog instance, or `None` if the log could not be created.
        """
        try:
            user = request.user if request.user.is_authenticated else None
            data = self._create_base_log_data(
                request, AuditEventType.PERMISSION_DENIED, user, success=False
            )

            data["professional_user"] = self._get_professional_user(user)
            data["domain"] = self._get_domain_from_request(request)
            data["performed_by_user"] = user
            data["details"] = {
                "action_attempted": action,
                "resource": resource,
                **(details or {}),
            }

            return ProfessionalActivityLog.objects.create(**data)
        except Exception as e:
            logger.error(f"Failed to create permission denied audit log: {e}")
            return None

    # ==================== Suspicious Activity Detection ====================

    def _check_login_patterns(self, log_entry: AuthAuditLog) -> None:
        """
        Detects and flags suspicious login events based on known login patterns.

        If the provided authentication log indicates the IP belongs to a datacenter or VPN network, creates a corresponding SuspiciousActivityLog entry containing network and ASN details as evidence.

        Parameters:
            log_entry (AuthAuditLog): The authentication log entry to evaluate; its network_type, asn, user, professional_user, domain, and ip_address fields are used when creating a suspicious activity record.
        """
        # Check for datacenter/VPN login
        if log_entry.network_type in [NetworkType.DATACENTER.value, NetworkType.VPN.value]:
            self._flag_suspicious(
                trigger_type="datacenter_login",
                severity="medium",
                user=log_entry.user,
                professional_user=log_entry.professional_user,
                domain=log_entry.domain,
                ip_address=log_entry.ip_address,
                asn_number=log_entry.asn_number,
                asn_org=log_entry.asn_org,
                description=f"Login from {log_entry.network_type} network: {log_entry.asn_org}",
                evidence={"auth_log_id": log_entry.id},
            )

    def _check_failed_login_patterns(
        self, log_entry: AuthAuditLog, attempted_email: Optional[str]
    ) -> None:
        """
        Detects and flags brute-force-like failed login patterns from the same IP.

        If five or more failed login attempts from the same IP are recorded within the past hour,
        creates a suspicious activity entry with trigger_type "brute_force_attempt" and includes
        the failed count, the provided attempted_email (when available), and the auth log id in the evidence.

        Parameters:
            log_entry (AuthAuditLog): The failed authentication log entry to evaluate.
            attempted_email (Optional[str]): The email address used in the failed attempt, included in the suspicious-evidence payload.
        """
        from datetime import timedelta
        from django.utils import timezone

        # Check for multiple failed logins from same IP in last hour
        one_hour_ago = timezone.now() - timedelta(hours=1)
        failed_count = AuthAuditLog.objects.filter(
            ip_address=log_entry.ip_address,
            event_type=AuditEventType.LOGIN_FAILED.value,
            timestamp__gte=one_hour_ago,
        ).count()

        if failed_count >= 5:
            self._flag_suspicious(
                trigger_type="brute_force_attempt",
                severity="high",
                user=log_entry.user,
                ip_address=log_entry.ip_address,
                asn_number=log_entry.asn_number,
                asn_org=log_entry.asn_org,
                description=f"{failed_count} failed login attempts from IP in last hour",
                evidence={
                    "failed_count": failed_count,
                    "attempted_email": attempted_email,
                    "auth_log_id": log_entry.id,
                },
            )

    def _check_api_patterns(self, log_entry: APIAccessLog) -> None:
        """
        Detects high-volume API requests from datacenter or VPN IPs and creates a suspicious-activity record when a threshold is exceeded.

        If the provided API access log indicates the request originated from a datacenter or VPN, counts APIAccessLog entries from the same IP in the last hour; if that count is 100 or greater, creates a SuspiciousActivityLog with severity "high" and evidence containing the request count and the triggering log's id. The created suspicious record will include user, professional_user, domain, IP and ASN details copied from the triggering log entry.

        Parameters:
            log_entry (APIAccessLog): The API access log entry that triggered the check; its network_type and ip_address are used to evaluate the pattern.
        """
        from datetime import timedelta
        from django.utils import timezone

        # Check for high volume from datacenter IPs
        if log_entry.network_type in [NetworkType.DATACENTER.value, NetworkType.VPN.value]:
            one_hour_ago = timezone.now() - timedelta(hours=1)
            request_count = APIAccessLog.objects.filter(
                ip_address=log_entry.ip_address,
                timestamp__gte=one_hour_ago,
            ).count()

            if request_count >= 100:
                self._flag_suspicious(
                    trigger_type="high_volume_datacenter",
                    severity="high",
                    user=log_entry.user,
                    professional_user=log_entry.professional_user,
                    domain=log_entry.domain,
                    ip_address=log_entry.ip_address,
                    asn_number=log_entry.asn_number,
                    asn_org=log_entry.asn_org,
                    description=f"High volume API access ({request_count} requests/hour) from datacenter IP",
                    evidence={
                        "request_count": request_count,
                        "api_log_id": log_entry.id,
                    },
                )

    def _flag_suspicious(
        self,
        trigger_type: str,
        severity: str,
        description: str,
        user: Optional["User"] = None,
        professional_user: Optional["ProfessionalUser"] = None,
        domain: Optional["UserDomain"] = None,
        ip_address: Optional[str] = None,
        asn_number: Optional[int] = None,
        asn_org: Optional[str] = None,
        evidence: Optional[dict] = None,
    ) -> Optional[SuspiciousActivityLog]:
        """
        Create a SuspiciousActivityLog record describing a detected suspicious event.

        Parameters:
            trigger_type (str): Short identifier for the detection trigger (e.g., "brute_force_attempt").
            severity (str): Severity classification for the event (e.g., "low", "medium", "high").
            description (str): Human-readable description of the suspicious activity.
            user (Optional[User]): Associated Django user, if applicable.
            professional_user (Optional[ProfessionalUser]): Associated professional user, if applicable.
            domain (Optional[UserDomain]): Associated user domain/context, if applicable.
            ip_address (Optional[str]): IP address related to the event.
            asn_number (Optional[int]): Autonomous System Number associated with the IP, if known.
            asn_org (Optional[str]): Autonomous System organization name, if known.
            evidence (Optional[dict]): Structured evidence or metadata supporting the flag (arbitrary key/value data).

        Returns:
            SuspiciousActivityLog | None: The created SuspiciousActivityLog instance, or `None` if creation failed.
        """
        try:
            return SuspiciousActivityLog.objects.create(
                trigger_type=trigger_type,
                severity=severity,
                user=user,
                professional_user=professional_user,
                domain=domain,
                ip_address=ip_address,
                asn_number=asn_number,
                asn_org=asn_org,
                description=description,
                evidence=evidence,
            )
        except Exception as e:
            logger.error(f"Failed to create suspicious activity log: {e}")
            return None

    # ==================== Object Activity Context ====================

    def log_object_activity(
        self,
        request: RequestType,
        obj: Any,
        action: str = "create",
    ) -> Optional[ObjectActivityContext]:
        """
        Create an ObjectActivityContext linking a business object to the current request context.

        Records the actor (user/professional), domain, IP/ASN/network info, geolocation, user-agent details, and session key associated with the specified object and action.

        Parameters:
            request (RequestType): The incoming HTTP request (Django HttpRequest or DRF Request) used to derive context.
            obj: The target business object; must have a `pk` attribute used as the object identifier.
            action (str): One of "create", "update", "view", "delete", or "export" describing the operation performed.

        Returns:
            ObjectActivityContext or None: The created ObjectActivityContext entry, or `None` if creation failed or is disabled.
        """
        if not self._is_enabled():
            return None
        
        try:
            from django.contrib.contenttypes.models import ContentType

            user = request.user if request.user.is_authenticated else None
            user_type = determine_user_type(request)

            # Get full context
            context = get_request_context(request)
            # Sanitize based on user type
            sanitized = sanitize_for_privacy(context, user_type)

            ip_info = sanitized["ip_info"]
            ua_info = sanitized["ua_info"]

            # Get content type for the object
            ct = ContentType.objects.get_for_model(obj)

            return ObjectActivityContext.objects.create(
                content_type=ct,
                object_id=str(obj.pk),
                action=action,
                user=user,
                user_type=user_type.value,
                professional_user=self._get_professional_user(user),
                domain=self._get_domain_from_request(request),
                ip_address=ip_info.ip_address if ip_info.ip_address else None,
                asn_number=ip_info.asn_number,
                asn_org=ip_info.asn_org,
                network_type=ip_info.network_type.value,
                country_code=ip_info.country_code,
                state_region=ip_info.state_region,
                user_agent_full=ua_info.full_user_agent if ua_info.full_user_agent else None,
                user_agent_browser=ua_info.browser_family,
                user_agent_os=ua_info.os_family,
                is_mobile=ua_info.is_mobile,
                is_bot=ua_info.is_bot,
                session_key=sanitized.get("session_key"),
            )
        except Exception as e:
            logger.error(f"Failed to create object activity context: {e}")
            return None

    def get_object_context(self, obj: Any) -> Optional[ObjectActivityContext]:
        """
        Retrieve the ObjectActivityContext that records the creation event for the given business object.

        Parameters:
            obj (Any): The business object (typically a Django model instance) whose creation context is requested.

        Returns:
            ObjectActivityContext | None: The creation context for `obj`, or `None` if no creation context exists.
        """
        return ObjectActivityContext.get_creation_context(obj)

    def get_object_history(self, obj: Any) -> list[ObjectActivityContext]:
        """
        Retrieve the chronological activity history for a business object.

        Parameters:
            obj (Any): The business object whose activity history to retrieve.

        Returns:
            list[ObjectActivityContext]: Activity context entries for the object ordered by timestamp (oldest first).
        """
        return list(ObjectActivityContext.get_for_object(obj).order_by("timestamp"))


# Singleton instance
audit_service = AuditService()

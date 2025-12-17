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
from typing import Optional, Any

from django.http import HttpRequest
from django.contrib.auth import get_user_model
from loguru import logger

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
    from .models import ProfessionalUser, UserDomain
else:
    User = get_user_model()

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
    """

    def _get_professional_user(
        self, user: Optional["User"]
    ) -> Optional["ProfessionalUser"]:
        """Get ProfessionalUser for a Django User if one exists."""
        if not user:
            return None
        try:
            from .models import ProfessionalUser

            return ProfessionalUser.objects.filter(user=user).first()
        except Exception:
            return None

    def _get_domain_from_request(
        self, request: HttpRequest
    ) -> Optional["UserDomain"]:
        """Extract UserDomain from request session."""
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
        user: Optional["User"] = None,
        success: bool = True,
    ) -> dict:
        """
        Create base audit log data with privacy considerations.

        Args:
            request: Django HttpRequest
            event_type: Type of event being logged
            user: User associated with the event (may be None for failed auth)
            success: Whether the event was successful

        Returns:
            Dictionary of field values for audit log creation
        """
        # Determine user type for privacy handling
        if user and user.is_authenticated:
            user_type = determine_user_type(request)
        else:
            user_type = UserType.ANONYMOUS

        # Get full context
        context = get_request_context(request)

        # Sanitize based on user type
        sanitized = sanitize_for_privacy(context, user_type)

        ip_info: IPInfo = sanitized["ip_info"]
        ua_info: UserAgentInfo = sanitized["ua_info"]

        return {
            "event_type": event_type.value,
            "user": user if user and user.is_authenticated else None,
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
        Log a successful login event.

        Args:
            request: Django HttpRequest
            user: User who logged in
            domain: UserDomain they logged into (for professionals)
            details: Additional details to store

        Returns:
            Created AuthAuditLog entry
        """
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
        Log a failed login attempt.

        Args:
            request: Django HttpRequest
            attempted_email: Email address that was attempted
            failure_reason: Why the login failed (invalid_password, user_not_found, etc.)
            user: User if found but password wrong
            domain: Domain attempted
            details: Additional details

        Returns:
            Created AuthAuditLog entry
        """
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
        """Log a logout event."""
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
        """Log a password change event."""
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
        """Log account creation."""
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
        Log an API access event.

        Args:
            request: Django HttpRequest
            endpoint: API endpoint accessed
            http_status: HTTP response status code
            response_time_ms: Response time in milliseconds
            resource_type: Type of resource accessed
            resource_id: ID of specific resource
            resource_count: Number of resources returned (for list endpoints)
            search_query: Search query if applicable
            details: Additional details

        Returns:
            Created APIAccessLog entry
        """
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
        """Log professional user creation."""
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
        """Log professional user acceptance."""
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
        """Log admin role grant."""
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
        """Log permission denied event."""
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
        """Check for suspicious login patterns."""
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
        """Check for brute force/credential stuffing patterns."""
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
        """Check for suspicious API access patterns."""
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
        """Create a suspicious activity flag."""
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
        request: HttpRequest,
        obj: Any,
        action: str = "create",
    ) -> Optional[ObjectActivityContext]:
        """
        Log activity context for a business object (Denial, Appeal, etc.).

        This creates a link between the object and the request context,
        allowing you to see who created/modified it and from where.

        Args:
            request: Django HttpRequest
            obj: The business object (must have a pk attribute)
            action: One of "create", "update", "view", "delete", "export"

        Returns:
            Created ObjectActivityContext entry

        Usage:
            # After creating a denial
            denial = Denial.objects.create(...)
            audit_service.log_object_activity(request, denial, action="create")

            # After updating
            denial.save()
            audit_service.log_object_activity(request, denial, action="update")
        """
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
        Get the creation context for a business object.

        Args:
            obj: The business object

        Returns:
            The ObjectActivityContext for when the object was created, or None
        """
        return ObjectActivityContext.get_creation_context(obj)

    def get_object_history(self, obj: Any) -> list[ObjectActivityContext]:
        """
        Get full activity history for a business object.

        Args:
            obj: The business object

        Returns:
            List of ObjectActivityContext entries ordered by timestamp
        """
        return list(ObjectActivityContext.get_for_object(obj).order_by("timestamp"))


# Singleton instance
audit_service = AuditService()

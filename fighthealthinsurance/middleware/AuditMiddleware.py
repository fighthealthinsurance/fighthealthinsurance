"""
Simple audit logging middleware for API requests.

Logs API access with timing information. Only active when ENABLE_AUDIT_LOGGING is True.
"""

import time
from typing import Callable

from django.http import HttpRequest, HttpResponse
from loguru import logger


class AuditMiddleware:
    """
    Middleware to log API requests for audit purposes.

    Only logs requests to API endpoints (/api/).
    Logging is synchronous but failures are swallowed to avoid impacting requests.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Record start time
        start_time = time.time()

        # Process request
        response = self.get_response(request)

        # Log API requests only
        if request.path.startswith("/api/"):
            self._log_request(request, response, start_time)

        return response

    def _log_request(
        self,
        request: HttpRequest,
        response: HttpResponse,
        start_time: float,
    ) -> None:
        """Log the API request. Errors are swallowed."""
        try:
            # Import here to avoid circular imports and allow lazy loading
            from fhi_users.audit import log_api_access, is_audit_enabled

            if not is_audit_enabled():
                return

            response_time_ms = int((time.time() - start_time) * 1000)
            log_api_access(
                request=request,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
            )
        except Exception as e:
            # Never let audit logging break the request
            logger.warning(
                f"Audit logging failed for {request.path}: {e}", exc_info=True
            )

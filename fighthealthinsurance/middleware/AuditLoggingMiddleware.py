"""
Middleware for logging API access to the audit log database.

This middleware:
1. Logs all API endpoint accesses (/api/ and /rest/)
2. Captures response time, status code, and resource information
3. Applies privacy rules based on user type (professional vs consumer)
4. Avoids logging sensitive internal endpoints
"""

import time
import re
from typing import Callable, Optional
from concurrent.futures import Future

from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin
from loguru import logger

# Import the shared thread pool executor for background tasks
from fighthealthinsurance.exec import executor


# Endpoints to exclude from logging (health checks, static files, etc.)
EXCLUDED_PATH_PATTERNS = [
    r"^/health",
    r"^/ready",
    r"^/metrics",
    r"^/static/",
    r"^/favicon\.ico",
    r"^/__debug__/",
    r"^/admin/jsi18n/",  # Django admin JS
]

# Only log these path prefixes
LOGGED_PATH_PREFIXES = [
    "/api/",
    "/rest/",
]

# Compiled regex for exclusions
_EXCLUDED_PATTERNS = [re.compile(p) for p in EXCLUDED_PATH_PATTERNS]


def _should_log_request(path: str) -> bool:
    """
    Determine whether a request path should be recorded in the audit log.

    Parameters:
        path (str): The request path to evaluate.

    Returns:
        bool: `true` if the path should be logged, `false` otherwise.
    """
    # Check if path matches logged prefixes
    if not any(path.startswith(prefix) for prefix in LOGGED_PATH_PREFIXES):
        return False

    # Check exclusions
    if any(pattern.match(path) for pattern in _EXCLUDED_PATTERNS):
        return False

    return True


def _extract_resource_info(path: str, response: HttpResponse) -> dict:
    """
    Extract resource metadata (type, identifier, and count) from a request path and HTTP response.

    Parameters:
        path (str): The request path (e.g., "/api/v1/denials/123/") used to infer resource type and identifier.
        response (HttpResponse): The response object which may contain a `.data` mapping with pagination or results.

    Returns:
        dict: A mapping with keys:
            - resource_type (str|None): The inferred resource name in singular form (e.g., "denial"), or `None` if not found.
            - resource_id (str|None): The resource identifier as a string (numeric or UUID) if present in the path, otherwise `None`.
            - resource_count (int|None): The number of items for list endpoints derived from `response.data["count"]` or the length of `response.data["results"]`, or `None` if unavailable.
    """
    info = {
        "resource_type": None,
        "resource_id": None,
        "resource_count": None,
    }

    # Common API patterns
    # /api/v1/denials/ -> resource_type=denial
    # /api/v1/denials/123/ -> resource_type=denial, resource_id=123
    # /rest/router/professional_user/ -> resource_type=professional_user

    path_parts = [p for p in path.strip("/").split("/") if p]

    if len(path_parts) >= 2:
        # Try to identify resource type from path
        # Skip version numbers like 'v1'
        for i, part in enumerate(path_parts):
            if part.startswith("v") and part[1:].isdigit():
                continue
            if part in ["api", "rest", "router"]:
                continue

            # This is likely the resource type
            info["resource_type"] = part.rstrip("s")  # denials -> denial

            # Check if next part is an ID
            if i + 1 < len(path_parts):
                next_part = path_parts[i + 1]
                # IDs are typically numeric or UUIDs
                if next_part.isdigit() or _is_uuid(next_part):
                    info["resource_id"] = next_part
            break

    # Try to get count from response for list endpoints
    if hasattr(response, "data") and isinstance(response.data, dict):
        if "count" in response.data:
            info["resource_count"] = response.data["count"]
        elif "results" in response.data and isinstance(response.data["results"], list):
            info["resource_count"] = len(response.data["results"])

    return info


def _is_uuid(s: str) -> bool:
    """
    Determine whether a string is a UUID in the canonical 8-4-4-4-12 hexadecimal format.

    Parameters:
        s (str): String to test; hexadecimal digits may be upper- or lower-case.

    Returns:
        bool: True if `s` matches the UUID pattern, False otherwise.
    """
    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
    )
    return bool(uuid_pattern.match(s))


class AuditLoggingMiddleware(MiddlewareMixin):
    """
    Middleware that logs API access to the database for audit purposes.

    Logs are created asynchronously to avoid impacting request latency.
    """

    def process_request(self, request: HttpRequest) -> None:
        """
        Record the request start time on the request object.

        Parameters:
            request (HttpRequest): The incoming HTTP request.

        Sets request._audit_start_time to the current epoch time in seconds (float) for later response-time calculation.
        """
        request._audit_start_time = time.time()

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:
        """
        Log an API access entry when the response is produced, failing silently if logging errors occur.

        This method records an audit entry only for request paths that qualify for logging; any exceptions raised while attempting to log are caught and suppressed (a debug message is emitted).

        Parameters:
            request (HttpRequest): The incoming HTTP request.
            response (HttpResponse): The HTTP response to be returned.

        Returns:
            HttpResponse: The original response object.
        """
        # Skip if path shouldn't be logged
        if not _should_log_request(request.path):
            return response

        try:
            # Calculate response time before dispatching to background thread
            start_time = getattr(request, "_audit_start_time", None)
            response_time_ms = None
            if start_time:
                response_time_ms = int((time.time() - start_time) * 1000)

            # Extract resource info before dispatching to background thread
            resource_info = _extract_resource_info(request.path, response)

            # Extract search query before dispatching to background thread
            search_query = None
            if request.method == "GET":
                search_query = request.GET.get("search") or request.GET.get("q")
            elif request.method == "POST":
                # Note: request.data is for DRF requests, request.POST for standard Django
                search_query = request.POST.get("search") or request.POST.get("q")

            # Extract all needed data from request/response before dispatching
            endpoint = request.path
            http_status = response.status_code

            # Dispatch logging to background thread to avoid blocking request
            # Note: We pass the request object for user/session/IP/UA context.
            # This is safe because we only read from it and the request is no longer
            # being modified after process_response.
            future = executor.submit(
                self._log_api_access,
                request,
                endpoint,
                http_status,
                response_time_ms,
                resource_info,
                search_query,
            )
            # Add callback to log any unhandled exceptions in background task
            future.add_done_callback(self._log_task_exception)
        except Exception as e:
            # Never let audit logging break the request
            logger.debug(f"Failed to submit audit logging task: {e}")

        return response

    def _log_task_exception(self, future: Future) -> None:
        """
        Log any exceptions that occurred in the background logging task.

        Parameters:
            future (Future): The completed Future object from the background task.
        """
        try:
            future.result()  # This will raise if the task failed
        except Exception as e:
            logger.debug(f"Background audit logging task failed: {e}")

    def _log_api_access(
        self,
        request: HttpRequest,
        endpoint: str,
        http_status: int,
        response_time_ms: Optional[int],
        resource_info: dict,
        search_query: Optional[str],
    ) -> None:
        """
        Log an API access event to the audit service.

        Records an audit entry for the given HTTP request by assembling and sending the following observable fields to the audit service: endpoint, HTTP status, response time in milliseconds, resource_type/resource_id/resource_count (derived from the request path and response payload), and an optional search query.

        Parameters:
            request (HttpRequest): The incoming HTTP request; used for user/session/IP/UA context.
            endpoint (str): The API endpoint path that was accessed.
            http_status (int): HTTP response status code.
            response_time_ms (Optional[int]): Response time in milliseconds, or None if not available.
            resource_info (dict): Dictionary containing resource_type, resource_id, and resource_count extracted from the request path and response.
            search_query (Optional[str]): Search query string if applicable (from GET/POST parameters 'search' or 'q'), or None.
        """
        try:
            # Import here to avoid circular imports and allow lazy loading
            from fhi_users.audit_service import audit_service

            # Create the log entry
            audit_service.log_api_access(
                request=request,
                endpoint=endpoint,
                http_status=http_status,
                response_time_ms=response_time_ms,
                resource_type=resource_info.get("resource_type"),
                resource_id=resource_info.get("resource_id"),
                resource_count=resource_info.get("resource_count"),
                search_query=search_query,
            )
        except Exception as e:
            # Catch any exceptions to prevent them from crashing the background task
            logger.debug(f"Audit logging failed: {e}")

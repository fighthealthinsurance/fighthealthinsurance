"""
Fuzz Guard Middleware - Detects and logs fuzzing/scanning attempts.

This middleware runs early in the request cycle to detect and block suspicious
requests that appear to be from security scanners, fuzzers, or other automated
attack tools.

Detection signals (configurable scoring):
- Known probe paths (/wp-admin, /.env, /phpmyadmin, etc.)
- Unusual HTTP methods (TRACE, TRACK, CONNECT)
- Excessive path/querystring length
- Too many query parameters
- Known scanner User-Agents
- Rate limiting violations
- Invalid denial_id format (CRITICAL: +100 score)
"""

import base64
import hashlib
import ipaddress
import json
import random
import re
import uuid
from typing import List, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from fighthealthinsurance.human_verification import TEAPOT_MESSAGE
from loguru import logger

# =============================================================================
# Detection Rules
# =============================================================================

# Known probe paths that scanners commonly try
PROBE_PATHS = [
    r"^/wp-admin",
    r"^/wp-content",
    r"^/wp-includes",
    r"^/wp-login",
    r"^/\.env",
    r"^/\.git",
    r"^/\.htaccess",
    r"^/\.htpasswd",
    r"^/\.aws",
    r"^/\.ssh",
    r"^/phpmyadmin",
    r"^/pma",
    r"^/myadmin",
    r"^/actuator",
    r"^/cgi-bin",
    r"^/server-status",
    r"^/server-info",
    r"^/admin\.php",
    r"^/manager/html",
    r"^/jenkins",
    r"^/solr",
    r"^/console",
    r"^/debug",
    r"^/trace",
    r"^/elmah\.axd",
    r"^/ecp/",
    r"^/owa/",
    r"^/autodiscover",
    r"^/webdav",
    r"^/shell",
    r"^/api/v1/pods",  # Kubernetes probe
    r"\.php$",  # PHP files on a Python app
    r"\.asp$",
    r"\.aspx$",
    r"\.jsp$",
    r"\.cgi$",
]

PROBE_PATH_PATTERNS = [re.compile(p, re.IGNORECASE) for p in PROBE_PATHS]

# Suspicious HTTP methods
SUSPICIOUS_METHODS = {"TRACE", "TRACK", "CONNECT", "DEBUG", "PROPFIND", "PROPPATCH"}

# Scanner User-Agent patterns
SCANNER_UA_PATTERNS = [
    r"sqlmap",
    r"nikto",
    r"ffuf",
    r"dirbuster",
    r"gobuster",
    r"nuclei",
    r"zgrab",
    r"masscan",
    r"wpscan",
    r"nmap",
    r"curl/\d",  # Bare curl often used in scripts
    r"python-requests/\d",  # Default Python requests UA
    r"go-http-client",
    r"libwww-perl",
    r"wget/",
    r"burp",
    r"zap",
    r"acunetix",
    r"nessus",
    r"qualys",
    r"w3af",
    r"arachni",
    r"skipfish",
    r"wfuzz",
    r"feroxbuster",
    r"httpx",
    r"amass",
]

SCANNER_UA_REGEX = re.compile("|".join(SCANNER_UA_PATTERNS), re.IGNORECASE)

# Denial ID validation pattern (must be numeric)
DENIAL_ID_PATTERN = re.compile(r"^-?\d+$")

# SQL injection patterns
SQL_INJECTION_PATTERNS = [
    r"union\s+select",
    r";\s*drop\s+",
    r"'\s*or\s+",
    r"--\s*$",
    r"/\*.*\*/",
    r"xp_cmdshell",
    r"exec\s*\(",
]

SQL_INJECTION_REGEX = re.compile("|".join(SQL_INJECTION_PATTERNS), re.IGNORECASE)


# =============================================================================
# Helper Functions
# =============================================================================


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP from request, handling proxies.

    Only trusts X-Forwarded-For / X-Real-IP when the immediate peer
    (REMOTE_ADDR) is in the configured TRUSTED_PROXIES list.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "0.0.0.0")
    remote_addr = str(remote_addr) if remote_addr else "0.0.0.0"

    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", [])

    if remote_addr in trusted_proxies:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = str(x_forwarded_for).split(",")[0].strip()
            if ip:
                return ip

        x_real_ip = request.META.get("HTTP_X_REAL_IP")
        if x_real_ip:
            return str(x_real_ip).strip()

    return remote_addr


def hash_ip(ip: str, salt: str) -> str:
    """Hash IP address with salt for privacy-preserving tracking."""
    return hashlib.sha256(f"{ip}:{salt}".encode()).hexdigest()


def get_ip_prefix(ip: str) -> str:
    """Get network prefix (IPv4 /24, IPv6 /64) for aggregate analysis."""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 4:
            network = ipaddress.ip_network(f"{ip}/24", strict=False)
            return str(network)
        else:
            network = ipaddress.ip_network(f"{ip}/64", strict=False)
            return str(network)
    except ValueError:
        return "invalid"


def extract_denial_id(request: HttpRequest) -> Optional[str]:
    """Extract denial_id from request (query params, POST data)."""
    # Check query params
    denial_id = request.GET.get("denial_id")
    if denial_id:
        return denial_id

    # Check POST data
    if request.method == "POST":
        denial_id = request.POST.get("denial_id")
        if denial_id:
            return denial_id

    return None


# =============================================================================
# Scoring Functions
# =============================================================================


def score_request(request: HttpRequest) -> Tuple[int, List[str]]:
    """
    Score a request for suspicious indicators.

    Returns:
        Tuple of (total_score, list_of_reasons)
    """
    score = 0
    reasons = []

    path = request.path
    method = (request.method or "GET").upper()
    query_string = request.META.get("QUERY_STRING", "")
    user_agent = request.META.get("HTTP_USER_AGENT", "")

    # 1. Probe path detection (+30)
    for pattern in PROBE_PATH_PATTERNS:
        if pattern.search(path):
            score += 30
            reasons.append(f"probe_path:{pattern.pattern[:30]}")
            break  # Only count once

    # 2. Suspicious HTTP method (+50)
    if method in SUSPICIOUS_METHODS:
        score += 50
        reasons.append(f"suspicious_method:{method}")

    # 3. Excessive path length (+20)
    if len(path) > 1000:
        score += 20
        reasons.append(f"excessive_path_length:{len(path)}")

    # 4. Excessive query string length (+20)
    if len(query_string) > 2000:
        score += 20
        reasons.append(f"excessive_querystring_length:{len(query_string)}")

    # 5. Too many query parameters (+15)
    param_count = len(request.GET)
    if param_count > 20:
        score += 15
        reasons.append(f"excessive_query_params:{param_count}")

    # 6. Scanner User-Agent (+40)
    if user_agent and SCANNER_UA_REGEX.search(user_agent):
        score += 40
        reasons.append(f"scanner_ua:{user_agent[:50]}")

    # 7. Empty User-Agent (+10)
    if not user_agent:
        score += 10
        reasons.append("empty_user_agent")

    # 8. CRITICAL: Invalid denial_id format (+100)
    denial_id = extract_denial_id(request)
    if denial_id is not None and denial_id != "":
        if not DENIAL_ID_PATTERN.match(str(denial_id)):
            score += 100
            reasons.append(f"invalid_denial_id:{str(denial_id)[:50]}")

    # 9. SQL injection patterns in query string (+60)
    if SQL_INJECTION_REGEX.search(query_string):
        score += 60
        reasons.append("sql_injection_pattern")

    # 10. Path traversal attempts (+50)
    if ".." in path or "%2e%2e" in path.lower():
        score += 50
        reasons.append("path_traversal")

    return score, reasons


def check_rate_limit(ip_hash: str) -> Tuple[bool, int]:
    """
    Check if IP has exceeded rate limit.

    Returns:
        Tuple of (is_throttled, current_count)
    """
    rate_limit = getattr(settings, "FUZZ_GUARD_RATE_LIMIT_PER_MINUTE", 60)
    cache_key = f"fuzz_guard:rate:{ip_hash}"

    try:
        # Fixed-window counter: set TTL only on first creation.
        if cache.add(cache_key, 1, timeout=60):
            new_count = 1
        else:
            try:
                new_count = cache.incr(cache_key)
            except ValueError:
                # Key may expire between add() and incr(); retry once.
                if cache.add(cache_key, 1, timeout=60):
                    new_count = 1
                else:
                    new_count = cache.incr(cache_key)
    except NotImplementedError:
        # Fallback for backends without atomic add/incr support.
        current = cache.get(cache_key, 0)
        new_count = int(current) + 1
        cache.set(cache_key, new_count, timeout=60)
    except Exception as e:
        # Conservative deny when rate-limit state cannot be trusted.
        logger.warning(f"Rate limiter cache error, applying conservative deny: {e}")
        return True, rate_limit + 1

    return new_count > rate_limit, new_count


# =============================================================================
# Response Generation
# =============================================================================


def determine_status_code(is_throttled: bool, teapot_prob: float = 0.10) -> int:
    """Decide the HTTP status code for a blocked fuzz request.

    Returns 429 when rate-limited, otherwise 418 or 400 based on teapot_prob.
    """
    if is_throttled:
        return 429
    if random.random() < teapot_prob:
        return 418
    return 400


def generate_fuzz_response(
    status_code: int, is_throttled: bool = False
) -> HttpResponse:
    """Generate appropriate response for blocked request.

    Args:
        status_code: The HTTP status code to return (from determine_status_code).
        is_throttled: Whether the request was rate-limited (adds Retry-After header).
    """
    response = HttpResponse(
        TEAPOT_MESSAGE,
        content_type="text/plain",
        status=status_code,
    )

    if is_throttled:
        # Add Retry-After header for 429
        response["Retry-After"] = "60"

    return response


# =============================================================================
# Middleware Class
# =============================================================================


class FuzzGuardMiddleware(MiddlewareMixin):
    """
    Middleware to detect and log fuzzing/scanning attempts.

    Runs early in the middleware chain to catch malicious requests
    before they consume application resources.
    """

    # Endpoints to bypass (health checks, static files, etc.)
    BYPASS_PATHS = frozenset(["/health", "/ready", "/metrics", "/favicon.ico"])
    BYPASS_PREFIXES = ("/static/", "/media/", "/__debug__/")

    def process_request(self, request: HttpRequest) -> Optional[HttpResponse]:
        """Process incoming request for fuzz detection."""
        # Check if fuzz guard is enabled
        if not getattr(settings, "FUZZ_GUARD_ENABLED", True):
            return None

        # Skip health check and static endpoints
        path = request.path
        if path in self.BYPASS_PATHS:
            return None
        if path.startswith(self.BYPASS_PREFIXES):
            return None

        # Get client IP
        client_ip = get_client_ip(request)
        ip_salt = getattr(settings, "FUZZ_GUARD_IP_SALT", "default-salt")
        ip_hash = hash_ip(client_ip, ip_salt)

        # Check rate limit first
        is_throttled, request_count = check_rate_limit(ip_hash)

        # Score the request
        score, reasons = score_request(request)

        # Add throttle score if rate limited
        if is_throttled:
            score += 50
            reasons.append(f"rate_limit_exceeded:{request_count}")

        # Check threshold
        threshold = getattr(settings, "FUZZ_GUARD_SCORE_THRESHOLD", 50)

        if score >= threshold:
            # Generate request ID for tracing
            request_id = request.META.get(
                "HTTP_X_REQUEST_ID",
                request.META.get("HTTP_X_AMZN_TRACE_ID", str(uuid.uuid4())[:8]),
            )

            # Log the attempt (don't use logger.exception to avoid stack traces)
            logger.warning(
                f"FuzzGuard blocked request: id={request_id} "
                f"ip_hash={ip_hash[:16]}... score={score} reasons={reasons}"
            )

            # Compute status code once, use for both storage and response
            teapot_prob = getattr(settings, "FUZZ_GUARD_TEAPOT_PROB", 0.10)
            status_code = determine_status_code(is_throttled, teapot_prob)
            response = generate_fuzz_response(status_code, is_throttled)

            self._store_fuzz_attempt(
                request=request,
                ip_hash=ip_hash,
                client_ip=client_ip,
                score=score,
                reasons=reasons,
                request_id=request_id,
                status_code=status_code,
            )

            return response

        return None

    def _store_fuzz_attempt(
        self,
        request: HttpRequest,
        ip_hash: str,
        client_ip: str,
        score: int,
        reasons: List[str],
        request_id: str,
        status_code: int,
    ) -> None:
        """Store fuzz attempt record in database."""
        try:
            # Import here to avoid circular imports
            from fighthealthinsurance.models import FuzzAttempt

            # Get user info
            user = None
            session_key = None
            is_authenticated = False

            if hasattr(request, "user") and request.user.is_authenticated:
                user = request.user
                is_authenticated = True

            if hasattr(request, "session") and request.session.session_key:
                session_key = request.session.session_key

            # Check if we should store raw IP
            raw_ip = None
            if getattr(settings, "FUZZ_GUARD_STORE_RAW_IP", False):
                raw_ip = client_ip

            # Create record
            attempt = FuzzAttempt(
                ip_hash=ip_hash,
                ip_prefix=get_ip_prefix(client_ip),
                raw_ip=raw_ip,
                user=user,
                session_key=session_key,
                is_authenticated=is_authenticated,
                method=(request.method or "UNKNOWN")[:20],
                path=request.path[:2048],
                status_returned=status_code,
                reason=json.dumps(reasons),
                score=score,
                request_id=request_id,
                key_version=getattr(settings, "FUZZ_LOG_KEY_VERSION", "v1"),
            )

            # Optionally capture encrypted blob
            if self._should_capture_request():
                self._capture_request_blob(attempt, request)

            attempt.save()

        except Exception as e:
            # Never let logging break the response - fail open for logging
            logger.warning(f"Failed to store fuzz attempt: {e}")

    def _should_capture_request(self) -> bool:
        """Check if request capture is enabled."""
        master_key = getattr(settings, "FUZZ_LOG_MASTER_KEY", "")
        return bool(master_key)

    def _capture_request_blob(self, attempt, request: HttpRequest) -> None:
        """Capture and encrypt request details."""
        try:
            from django.core.files.base import ContentFile

            # Build capture payload
            capture = {
                "timestamp": timezone.now().isoformat(),
                "method": request.method,
                "path": request.get_full_path(),
                "headers": dict(request.headers),
                "remote_addr": get_client_ip(request),
                "x_forwarded_for": request.META.get("HTTP_X_FORWARDED_FOR", ""),
            }

            # Optionally capture body
            capture_body = getattr(settings, "FUZZ_GUARD_CAPTURE_BODY_BYTES", 0)
            if capture_body > 0:
                try:
                    content_type = request.content_type or ""
                    # Skip multipart unless configured
                    if "multipart" in content_type and not getattr(
                        settings, "FUZZ_GUARD_CAPTURE_MULTIPART", False
                    ):
                        capture["body_skipped"] = "multipart"
                    else:
                        body = request.body[:capture_body]
                        capture["body"] = base64.b64encode(body).decode("ascii")
                        capture["body_content_type"] = content_type
                        capture["body_length"] = len(request.body)
                except Exception:
                    capture["body_error"] = "failed to read"

            # Serialize to JSON
            payload = json.dumps(capture).encode("utf-8")

            # Enforce max size
            try:
                max_size = int(
                    getattr(settings, "FUZZ_GUARD_MAX_CAPTURE_SIZE", 64 * 1024)
                )
            except (TypeError, ValueError):
                max_size = 64 * 1024
            if max_size < 1:
                max_size = 1
            if len(payload) > max_size:
                source_text = payload.decode("utf-8", errors="replace")
                best_payload = None

                # Binary search for a character-bounded payload that fits max_size.
                left = 0
                right = len(source_text)
                while left <= right:
                    mid = (left + right) // 2
                    candidate = json.dumps(
                        {
                            "truncated": True,
                            "payload": source_text[:mid],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    if len(candidate) <= max_size:
                        best_payload = candidate
                        left = mid + 1
                    else:
                        right = mid - 1

                if best_payload is None:
                    # Always write valid UTF-8 JSON, even with very small size limits.
                    for candidate in (b'{"truncated":true}', b"{}", b"0"):
                        if len(candidate) <= max_size:
                            best_payload = candidate
                            break

                payload = best_payload or b"{}"

            # Save to encrypted field (EncryptedFileField handles encryption)
            attempt.encrypted_blob.save(
                f"fuzz_{attempt.request_id}.json.enc",
                ContentFile(payload),
                save=False,  # Don't save model yet
            )
            attempt.encrypted_blob_size = len(payload)

        except Exception as e:
            logger.warning(f"Failed to capture request blob: {e}")

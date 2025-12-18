"""
Utilities for audit logging including IP geolocation, ASN lookup, and user agent parsing.

This module provides:
1. IP to ASN/ISP lookup (using geoip2fast for speed)
2. IP to country/region lookup
3. User agent parsing and simplification
4. Network type classification (residential vs datacenter vs VPN)
5. Helper functions for creating audit log entries

Privacy considerations:
- Consumer users: Only ASN and country stored (no IP, no state)
- Professional users: Full details stored for audit compliance
"""

import re
import typing
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Tuple, Union

from django.http import HttpRequest
from loguru import logger

if typing.TYPE_CHECKING:
    from rest_framework.request import Request as DRFRequest

# Type alias for request objects (Django HttpRequest or DRF Request)
# Both have compatible .META, .user, .session, .path, .method attributes
RequestType = Union[HttpRequest, "DRFRequest"]

# Try to import geoip2fast, fall back gracefully if not installed
try:
    from geoip2fast import GeoIP2Fast

    GEOIP_AVAILABLE = True
except ImportError:
    GEOIP_AVAILABLE = False
    logger.warning("geoip2fast not installed - IP geolocation will be unavailable")

# Try to import user_agents for UA parsing
try:
    from user_agents import parse as parse_user_agent

    USER_AGENTS_AVAILABLE = True
except ImportError:
    USER_AGENTS_AVAILABLE = False
    logger.warning("user-agents not installed - user agent parsing will be limited")


from .audit_models import (
    NetworkType,
    UserType,
    KNOWN_DATACENTER_ASNS,
    KNOWN_VPN_ASNS,
)


@dataclass
class IPInfo:
    """Information extracted from an IP address."""

    ip_address: str
    asn_number: Optional[int] = None
    asn_org: Optional[str] = None
    country_code: Optional[str] = None
    state_region: Optional[str] = None
    city: Optional[str] = None
    network_type: NetworkType = NetworkType.UNKNOWN
    is_datacenter: bool = False
    is_vpn: bool = False


@dataclass
class UserAgentInfo:
    """Parsed user agent information."""

    full_user_agent: str
    browser_family: Optional[str] = None
    browser_version: Optional[str] = None
    os_family: Optional[str] = None
    os_version: Optional[str] = None
    device_family: Optional[str] = None
    is_mobile: bool = False
    is_tablet: bool = False
    is_bot: bool = False

    @property
    def simplified(self) -> str:
        """
        Return a compact representation of the user agent combining browser and OS.

        Returns:
            A string in the form "Browser/OS" when one or both are present, or "Unknown" when neither is available.
        """
        parts = []
        if self.browser_family:
            parts.append(self.browser_family)
        if self.os_family:
            parts.append(self.os_family)
        return "/".join(parts) if parts else "Unknown"


# Singleton GeoIP reader - initialized lazily
_geoip_reader: Optional["GeoIP2Fast"] = None


def _get_geoip_reader() -> Optional["GeoIP2Fast"]:
    """
    Return the singleton GeoIP2Fast reader, initializing it on first call.

    Returns:
        GeoIP2Fast or None: The initialized GeoIP2Fast reader, or `None` if GeoIP support is unavailable or initialization failed.
    """
    global _geoip_reader
    if not GEOIP_AVAILABLE:
        return None
    if _geoip_reader is None:
        try:
            # GeoIP2Fast includes its own data file
            _geoip_reader = GeoIP2Fast()
            logger.info("GeoIP2Fast reader initialized")
        except Exception as e:
            logger.error(f"Failed to initialize GeoIP2Fast: {e}")
            return None
    return _geoip_reader


def get_client_ip(request: RequestType) -> Optional[str]:
    """
    Extract the client IP address from a request.

    Handles common proxy headers (X-Forwarded-For, X-Real-IP) and falls back
    to REMOTE_ADDR.

    Args:
        request: Django HttpRequest or DRF Request object

    Returns:
        Client IP address string or None if not determinable
    """
    # Check for proxy headers first
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # Take the first IP in the chain (original client)
        ip = x_forwarded_for.split(",")[0].strip()
        if ip:
            return ip

    x_real_ip = request.META.get("HTTP_X_REAL_IP")
    if x_real_ip:
        return x_real_ip.strip()

    # Fall back to REMOTE_ADDR
    return request.META.get("REMOTE_ADDR")


def lookup_ip_info(ip_address: str) -> IPInfo:
    """
    Look up geographic, ASN, and network-type information for an IP address.

    Performs a best-effort GeoIP lookup and populates an IPInfo instance with any available fields:
    country_code, asn_number, asn_org, network_type, is_datacenter, and is_vpn. If `ip_address` is falsy
    or a GeoIP reader is unavailable, returns an IPInfo containing only the provided ip_address (no exception
    is raised). Lookup failures are handled internally and result in a partially populated IPInfo.

    Parameters:
        ip_address (str): IPv4 or IPv6 address string to look up.

    Returns:
        IPInfo: Dataclass with populated fields where available; fields remain default/None when lookup data is missing.
    """
    info = IPInfo(ip_address=ip_address)

    if not ip_address:
        return info

    reader = _get_geoip_reader()
    if reader is None:
        return info

    try:
        result = reader.lookup(ip_address)
        if result and not result.is_error:
            info.country_code = result.country_code if hasattr(result, "country_code") else None
            info.asn_number = result.asn if hasattr(result, "asn") else None
            info.asn_org = result.asn_name if hasattr(result, "asn_name") else None

            # Classify network type based on ASN
            if info.asn_number:
                info.network_type = classify_network_type(info.asn_number, info.asn_org)
                info.is_datacenter = info.network_type == NetworkType.DATACENTER
                info.is_vpn = info.network_type == NetworkType.VPN

    except Exception as e:
        logger.debug(f"GeoIP lookup failed for {ip_address}: {e}")

    return info


def classify_network_type(
    asn_number: Optional[int], asn_org: Optional[str] = None
) -> NetworkType:
    """
    Determine the network category inferred from an Autonomous System Number (ASN) and its organization name.

    Parameters:
        asn_number (Optional[int]): The Autonomous System Number associated with the IP address; may be None.
        asn_org (Optional[str]): The organization name associated with the ASN; may be None or empty.

    Returns:
        NetworkType: The inferred network type (e.g., DATACENTER, VPN, MOBILE, RESIDENTIAL, EDUCATION, GOVERNMENT, or UNKNOWN).

    Notes:
        Returns NetworkType.UNKNOWN when no ASN is provided or when the inputs do not indicate a specific category.
    """
    if not asn_number:
        return NetworkType.UNKNOWN

    # Check known lists first
    if asn_number in KNOWN_VPN_ASNS:
        return NetworkType.VPN
    if asn_number in KNOWN_DATACENTER_ASNS:
        return NetworkType.DATACENTER

    # Heuristic classification based on org name
    if asn_org:
        org_lower = asn_org.lower()

        # Datacenter/cloud indicators
        datacenter_keywords = [
            "amazon",
            "aws",
            "google",
            "microsoft",
            "azure",
            "digitalocean",
            "linode",
            "vultr",
            "hetzner",
            "ovh",
            "hosting",
            "server",
            "cloud",
            "datacenter",
            "data center",
            "colocation",
            "colo",
        ]
        if any(kw in org_lower for kw in datacenter_keywords):
            return NetworkType.DATACENTER

        # VPN indicators
        vpn_keywords = [
            "vpn",
            "private internet",
            "express vpn",
            "nord",
            "proton",
            "mullvad",
            "surfshark",
        ]
        if any(kw in org_lower for kw in vpn_keywords):
            return NetworkType.VPN

        # Mobile carrier indicators
        mobile_keywords = [
            "mobile",
            "wireless",
            "cellular",
            "t-mobile",
            "verizon wireless",
            "at&t mobility",
            "sprint",
        ]
        if any(kw in org_lower for kw in mobile_keywords):
            return NetworkType.MOBILE

        # ISP/Residential indicators (common ISPs)
        residential_keywords = [
            "comcast",
            "verizon",
            "at&t",
            "charter",
            "spectrum",
            "cox",
            "frontier",
            "centurylink",
            "lumen",
            "xfinity",
            "rogers",
            "bell",
            "telus",
            "shaw",
            "bt ",
            "virgin media",
            "sky ",
            "deutsche telekom",
            "orange",
            "vodafone",
        ]
        if any(kw in org_lower for kw in residential_keywords):
            return NetworkType.RESIDENTIAL

        # Education indicators
        education_keywords = [
            "university",
            "college",
            "school",
            "edu",
            "academic",
            "research",
        ]
        if any(kw in org_lower for kw in education_keywords):
            return NetworkType.EDUCATION

        # Government indicators
        gov_keywords = ["government", "federal", "state of", "city of", "county of"]
        if any(kw in org_lower for kw in gov_keywords):
            return NetworkType.GOVERNMENT

    return NetworkType.UNKNOWN


def parse_user_agent_string(user_agent: Optional[str]) -> UserAgentInfo:
    """
    Parse a raw HTTP User-Agent string into a UserAgentInfo object.

    Parameters:
        user_agent (Optional[str]): Raw User-Agent header value.

    Returns:
        UserAgentInfo: Parsed browser, OS, device families and versions, and flags for `is_mobile`, `is_tablet`, and `is_bot`. If `user_agent` is falsy returns a UserAgentInfo with `full_user_agent` set to an empty string. When available, the external `user_agents` library is used; otherwise a conservative internal parser is used.
    """
    if not user_agent:
        return UserAgentInfo(full_user_agent="")

    info = UserAgentInfo(full_user_agent=user_agent)

    if USER_AGENTS_AVAILABLE:
        try:
            ua = parse_user_agent(user_agent)
            info.browser_family = ua.browser.family
            info.browser_version = ua.browser.version_string
            info.os_family = ua.os.family
            info.os_version = ua.os.version_string
            info.device_family = ua.device.family
            info.is_mobile = ua.is_mobile
            info.is_tablet = ua.is_tablet
            info.is_bot = ua.is_bot
        except Exception as e:
            logger.debug(f"User agent parsing failed: {e}")
            # Fall back to basic parsing
            info = _basic_ua_parse(user_agent)
    else:
        # Basic parsing without the library
        info = _basic_ua_parse(user_agent)

    return info


def _basic_ua_parse(user_agent: str) -> UserAgentInfo:
    """
    Parse a raw User-Agent string into a lightweight UserAgentInfo using conservative, heuristic rules.

    Parameters:
        user_agent (str): Raw User-Agent header value to analyze.

    Returns:
        UserAgentInfo: Object with `full_user_agent` preserved and inferred fields populated:
            - `browser_family` and `os_family` when identifiable,
            - `is_mobile` and `is_tablet` flags for device form factor,
            - `is_bot` flag for common automated clients.
    """
    info = UserAgentInfo(full_user_agent=user_agent)
    ua_lower = user_agent.lower()

    # Detect bots
    bot_indicators = [
        "bot",
        "crawler",
        "spider",
        "scraper",
        "curl",
        "wget",
        "python",
        "java",
        "httpclient",
    ]
    info.is_bot = any(ind in ua_lower for ind in bot_indicators)

    # Basic browser detection
    if "chrome" in ua_lower and "edg" not in ua_lower:
        info.browser_family = "Chrome"
    elif "firefox" in ua_lower:
        info.browser_family = "Firefox"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        info.browser_family = "Safari"
    elif "edg" in ua_lower:
        info.browser_family = "Edge"
    elif "msie" in ua_lower or "trident" in ua_lower:
        info.browser_family = "Internet Explorer"

    # Basic OS detection
    if "windows" in ua_lower:
        info.os_family = "Windows"
    elif "mac os" in ua_lower or "macos" in ua_lower:
        info.os_family = "macOS"
    elif "linux" in ua_lower and "android" not in ua_lower:
        info.os_family = "Linux"
    elif "android" in ua_lower:
        info.os_family = "Android"
        info.is_mobile = True
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        info.os_family = "iOS"
        info.is_mobile = "iphone" in ua_lower
        info.is_tablet = "ipad" in ua_lower

    # Mobile detection
    if not info.is_mobile:
        mobile_indicators = ["mobile", "android", "iphone", "ipod", "blackberry"]
        info.is_mobile = any(ind in ua_lower for ind in mobile_indicators)

    return info


def get_request_context(request: RequestType) -> dict:
    """
    Collect audit-relevant context from an HTTP request.

    Parameters:
        request: Django HttpRequest or DRF Request object used to extract headers, path, method, and session.

    Returns:
        dict: Context with keys:
            - ip_info: IPInfo populated from the client's IP address (may be partially populated if lookup fails).
            - ua_info: UserAgentInfo parsed from the HTTP_USER_AGENT header.
            - request_path: request.path string.
            - request_method: request.method string.
            - http_referer: value of the HTTP_REFERER header or None if absent.
            - session_key: session key string if available, otherwise None.
    """
    ip = get_client_ip(request)
    ip_info = lookup_ip_info(ip) if ip else IPInfo(ip_address="")

    ua_string = request.META.get("HTTP_USER_AGENT", "")
    ua_info = parse_user_agent_string(ua_string)

    # Get session key safely - session could be a dict (in tests) or a SessionBase object
    session_key = None
    if hasattr(request, "session"):
        session = request.session
        if hasattr(session, "session_key"):
            session_key = session.session_key
        elif isinstance(session, dict):
            session_key = session.get("session_key")

    return {
        "ip_info": ip_info,
        "ua_info": ua_info,
        "request_path": request.path,
        "request_method": request.method,
        "http_referer": request.META.get("HTTP_REFERER"),
        "session_key": session_key,
    }


def determine_user_type(request: RequestType) -> UserType:
    """
    Determine the user's privacy classification for audit logging.

    Inspects the request's authenticated user and, when possible, checks for an associated ProfessionalUser with an active ProfessionalDomainRelation to decide whether the user should be treated as PROFESSIONAL. Falls back to CONSUMER if checks cannot be completed or no professional relation is found.

    Parameters:
        request: Django HttpRequest or DRF Request object used to examine authentication and related professional records.

    Returns:
        UserType: `ANONYMOUS` if the request is unauthenticated, `PROFESSIONAL` if an active professional-domain relation is found for the user, `CONSUMER` otherwise.
    """
    if not request.user or not request.user.is_authenticated:
        return UserType.ANONYMOUS

    # Check if user is a professional
    try:
        from .models import ProfessionalUser, ProfessionalDomainRelation

        professional = ProfessionalUser.objects.filter(user=request.user).first()
        if professional:
            # Check if they have any active domain relation
            has_active_relation = ProfessionalDomainRelation.objects.filter(
                professional=professional, active_domain_relation=True
            ).exists()
            if has_active_relation:
                return UserType.PROFESSIONAL
    except Exception as e:
        logger.debug(f"Error checking professional status: {e}")

    return UserType.CONSUMER


def sanitize_for_privacy(
    context: dict, user_type: UserType
) -> dict:
    """
    Sanitize an audit request context to retain or remove sensitive fields based on the user's privacy level.

    For UserType.PROFESSIONAL the input context is returned unchanged. For consumer or anonymous users the function removes the raw IP and granular location (state, city), clears the full user-agent string and http_referer, and preserves non-identifying metadata such as ASN, country_code, network_type, is_datacenter, is_vpn, request_path, request_method, and session_key.

    Parameters:
        context (dict): Audit context produced by get_request_context().
        user_type (UserType): Determines privacy level (PROFESSIONAL preserves full data; CONSUMER/ANONYMOUS are sanitized).

    Returns:
        dict: A sanitized context dictionary suitable for audit logging.
    """
    if user_type == UserType.PROFESSIONAL:
        # Professionals get full audit trail
        return context

    # Consumer/Anonymous: privacy-preserving
    ip_info = context.get("ip_info", IPInfo(ip_address=""))
    ua_info = context.get("ua_info", UserAgentInfo(full_user_agent=""))

    sanitized_ip = IPInfo(
        ip_address="",  # Don't store IP
        asn_number=ip_info.asn_number,
        asn_org=ip_info.asn_org,
        country_code=ip_info.country_code,
        state_region=None,  # Don't store state for consumers
        city=None,
        network_type=ip_info.network_type,
        is_datacenter=ip_info.is_datacenter,
        is_vpn=ip_info.is_vpn,
    )

    sanitized_ua = UserAgentInfo(
        full_user_agent="",  # Don't store full UA
        browser_family=ua_info.browser_family,
        os_family=ua_info.os_family,
        is_mobile=ua_info.is_mobile,
        is_bot=ua_info.is_bot,
    )

    return {
        "ip_info": sanitized_ip,
        "ua_info": sanitized_ua,
        "request_path": context.get("request_path"),
        "request_method": context.get("request_method"),
        # Don't store referer for consumers (could leak info)
        "http_referer": None,
        "session_key": context.get("session_key"),
    }

"""The client's network, as Cloudflare saw it: bucketed, keyed and classed.

Shared by the per-address agreement cap (assistant_ip_limit.py) and the LLM
usage metrics (ml/llm_usage.py), so both read the address one way and key it
one way.

The address comes from ``CF-Connecting-IP`` only. The generic client-IP
helper (fhi_users.audit.get_client_ip) trusts the first X-Forwarded-For
entry, which the caller writes; Cloudflare overwrites this header on every
request. A request without it (local dev, an in-cluster call) has no address.

Nothing here stores anything. A key is a keyed digest of an address prefix
and a period, so a period's keys can't be matched with another period's, and
the address can't be read back without SECRET_KEY.
"""

import datetime
import functools
import hashlib
import hmac
import ipaddress
import re
from typing import Any, Mapping, Optional, Tuple, Union

from django.conf import settings

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

CF_IP_META = "HTTP_CF_CONNECTING_IP"
CF_COUNTRY_META = "HTTP_CF_IPCOUNTRY"
_CF_IP_HEADER = b"cf-connecting-ip"
_CF_COUNTRY_HEADER = b"cf-ipcountry"

# What a missing or unusable address buckets to.
NO_ADDRESS = "none"

# Long enough for any IPv6 literal; a header longer than this is not one, and
# is not worth parsing.
_MAX_ADDRESS_CHARS = 64

# Cloudflare's country header: ISO alpha-2, plus XX (unknown) and T1 (Tor).
_COUNTRY = re.compile(r"[A-Z]{2}|T1")
TOR_COUNTRY = "T1"
_UNKNOWN_COUNTRY = "XX"

# Network classes, a fixed set (they are Prometheus label values):
#   isp          a named network that is not a known host or platform
#   hosting      a cloud or hosting provider (and so iCloud Private Relay
#                and Cloudflare WARP, which egress from them)
#   ai_platform  an AI assistant platform's servers
#   tor          a Tor exit (Cloudflare's T1)
#   unknown      a client was there, but its network couldn't be named
#   none         no client at all (system work)
ISP = "isp"
HOSTING = "hosting"
AI_PLATFORM = "ai_platform"
TOR = "tor"
UNKNOWN = "unknown"
NONE = "none"
NETWORK_CLASSES = (ISP, HOSTING, AI_PLATFORM, TOR, UNKNOWN, NONE)

# AI assistant platforms' published outbound ranges. Anthropic's, checked
# 2026-10-03 (docs/mcp-server.md). Add others with FHI_AI_PLATFORM_CIDRS
# (comma-separated) rather than guessing them here.
AI_PLATFORM_NETWORKS: Tuple[IPNetwork, ...] = (ipaddress.ip_network("160.79.104.0/21"),)
_AI_PLATFORM_TOKENS = frozenset({"ANTHROPIC", "OPENAI"})

# Words in an ASN name that mark a cloud or hosting provider, matched whole
# against the name split on anything not a letter or digit ("AMAZON-02",
# "GOOGLE-CLOUD-PLATFORM", "AS-CHOOPA"). GOOGLE alone is Google's own
# network; GOOGLE-FIBER is a home ISP, so FIBER keeps it out.
_HOSTING_TOKENS = frozenset(
    {
        "AKAMAI",
        "ALIBABA",
        "AMAZON",
        "CHOOPA",
        "CLOUDFLARENET",
        "CONTABO",
        "DATACAMP",
        "DIGITALOCEAN",
        "FASTLY",
        "GOOGLE",
        "HETZNER",
        "IONOS",
        "LEASEWEB",
        "LINODE",
        "M247",
        "MICROSOFT",
        "ORACLE",
        "OVH",
        "SCALEWAY",
        "TENCENT",
        "VULTR",
    }
)
_NOT_HOSTING_TOKENS = frozenset({"FIBER"})
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")


def _scope_header(scope: Optional[Mapping[str, Any]], name: bytes) -> str:
    if not scope:
        return ""
    for key, value in scope.get("headers") or ():
        if key == name:
            try:
                return bytes(value).decode("latin-1").strip()
            except Exception:
                return ""
    return ""


def cf_ip_from_meta(meta: Mapping[str, Any]) -> str:
    """The client address Cloudflare saw, from a Django request's META."""
    return str(meta.get(CF_IP_META, "") or "").strip()


def cf_ip_from_scope(scope: Optional[Mapping[str, Any]]) -> str:
    """The client address Cloudflare saw, from an ASGI (WebSocket) scope."""
    return _scope_header(scope, _CF_IP_HEADER)


def _country(raw: str) -> str:
    code = raw.strip().upper()
    return code if _COUNTRY.fullmatch(code) else ""


def cf_country_from_meta(meta: Mapping[str, Any]) -> str:
    """Cloudflare's country code for the client ("" when absent or not a
    code). May be T1 (Tor) or XX (unknown): see country_label."""
    return _country(str(meta.get(CF_COUNTRY_META, "") or ""))


def cf_country_from_scope(scope: Optional[Mapping[str, Any]]) -> str:
    return _country(_scope_header(scope, _CF_COUNTRY_HEADER))


def country_label(code: str) -> str:
    """A country code fit to store: ISO alpha-2, or "" for unknown and Tor
    (Tor is a network class, not a place)."""
    code = _country(code or "")
    return "" if code in (_UNKNOWN_COUNTRY, TOR_COUNTRY) else code


def parse_ip(raw: Optional[str]) -> Optional[IPAddress]:
    """The address in ``raw``, with an IPv4-mapped IPv6 address unwrapped, or
    None when it isn't a single IP literal."""
    candidate = str(raw or "").strip()
    if not candidate or len(candidate) > _MAX_ADDRESS_CHARS:
        return None
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def prefix_of(raw: Optional[str], *, v4_bits: int, v6_bits: int) -> str:
    """The network ``raw`` is counted under: its IPv4 /``v4_bits`` or IPv6
    /``v6_bits`` ("203.0.113.0/24", "2001:db8:1::/48"), the bare address when
    the length is the whole address, or NO_ADDRESS."""
    ip = parse_ip(raw)
    if ip is None:
        return NO_ADDRESS
    bits = v4_bits if isinstance(ip, ipaddress.IPv4Address) else v6_bits
    if bits >= ip.max_prefixlen:
        return str(ip)
    try:
        return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))
    except ValueError:
        return NO_ADDRESS


def period_key(label: bytes, period: str, address: str) -> str:
    """A keyed digest of ``address`` for ``period``: HMAC under a sub-key of
    SECRET_KEY named by ``label``, so each feature's keys differ and a
    period's keys can't be matched with another period's."""
    secret = settings.SECRET_KEY
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    secret = hmac.new(secret, label, hashlib.sha256).digest()
    message = f"{period}|{address}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def week_start(day: datetime.date) -> datetime.date:
    """The Monday that starts ``day``'s ISO week."""
    return day - datetime.timedelta(days=day.weekday())


@functools.lru_cache(maxsize=8)
def _parse_networks(signature: str) -> Tuple[IPNetwork, ...]:
    networks = []
    for item in signature.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def ai_platform_networks() -> Tuple[IPNetwork, ...]:
    """AI_PLATFORM_NETWORKS plus FHI_AI_PLATFORM_CIDRS (a list, or a
    comma-separated string; bad entries are skipped)."""
    raw = getattr(settings, "FHI_AI_PLATFORM_CIDRS", "") or ""
    signature = raw if isinstance(raw, str) else ",".join(str(r) for r in raw)
    return AI_PLATFORM_NETWORKS + _parse_networks(signature)


def _tokens(asn_name: str) -> frozenset:
    return frozenset(t for t in _TOKEN_SPLIT.split(asn_name.upper()) if t)


def classify_asn(asn_name: Optional[str]) -> str:
    """The network class an ASN name alone gives: ai_platform, hosting, isp,
    or unknown when there is no name."""
    tokens = _tokens(str(asn_name or ""))
    if not tokens:
        return UNKNOWN
    if tokens & _AI_PLATFORM_TOKENS:
        return AI_PLATFORM
    if tokens & _HOSTING_TOKENS and not tokens & _NOT_HOSTING_TOKENS:
        return HOSTING
    return ISP


def classify(ip: Optional[str], asn_name: Optional[str], cf_country: str = "") -> str:
    """The network class of a client: tor (Cloudflare's T1), ai_platform (a
    platform's published range), else what the ASN name gives."""
    if cf_country == TOR_COUNTRY:
        return TOR
    parsed = parse_ip(ip)
    if parsed is not None and any(parsed in net for net in ai_platform_networks()):
        return AI_PLATFORM
    return classify_asn(asn_name)

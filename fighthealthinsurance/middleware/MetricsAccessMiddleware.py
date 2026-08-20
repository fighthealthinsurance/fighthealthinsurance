"""Restrict the Prometheus ``/metrics`` endpoint to in-cluster callers.

``django_prometheus`` mounts ``/metrics`` at the URL root and the nginx Ingress
forwards every path of every host to the same Service, so without a gate the
whole metric set (request volumes and latencies per view, DB connection-pool
stats, ML call counters) is readable by anyone on the internet. Requests that
did not originate inside the cluster get a 404: the endpoint should not even
advertise its existence outward.

"Inside the cluster" needs two independent signals, both required:

* ``REMOTE_ADDR`` -- the peer socket address, which a client cannot forge --
  is inside one of ``settings.METRICS_ALLOWED_CIDRS`` (private + loopback
  ranges by default, which is what pod and node IPs are).
* No proxy-forwarding header is present. The ingress controller always adds
  ``X-Forwarded-For``/``X-Real-IP``/``X-Forwarded-Host`` and a client cannot
  strip them, so their presence means the request arrived from outside through
  the ingress -- where ``REMOTE_ADDR`` alone would just be the (private)
  ingress-controller pod IP and would sail through the check above. A direct
  in-cluster scrape of ``pod-ip:80/metrics`` sends none of them.

This is defense in depth behind the ingress-level deny in ``k8s/deploy.yaml``;
it also covers any future path into the pods that bypasses that Ingress.
"""

import ipaddress
from typing import Callable, Optional, Sequence, Tuple, Union, cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.urls import NoReverseMatch, reverse

# Pod, node and loopback addresses live in these ranges on every cluster we
# run on. Overridable via settings for clusters with public pod CIDRs.
DEFAULT_METRICS_ALLOWED_CIDRS: Tuple[str, ...] = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
)

# Headers the ingress controller stamps on forwarded requests. Any one of them
# means "this came through a proxy", i.e. from outside.
FORWARDING_HEADERS: Tuple[str, ...] = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_REAL_IP",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_FORWARDED",
)

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def _parse_networks(cidrs: Sequence[str]) -> Tuple[IPNetwork, ...]:
    """Parse CIDRs, skipping malformed entries rather than failing startup."""
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


class MetricsAccessMiddleware:
    """404s requests to ``/metrics`` that did not come from inside the cluster."""

    sync_capable = True
    async_capable = True

    # Fallback if the metrics view is not routed under its usual name.
    _DEFAULT_METRICS_PATH = "/metrics"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        # An unset (or empty) setting means "use the defaults"; a setting that
        # is present but unparseable leaves no networks, i.e. deny everything.
        configured = getattr(settings, "METRICS_ALLOWED_CIDRS", None)
        self.allowed_networks = _parse_networks(
            configured if configured else DEFAULT_METRICS_ALLOWED_CIDRS
        )
        self._metrics_path_cache: Optional[str] = None
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest):
        # Under ASGI the caller awaits us, so the block has to happen inside
        # the coroutine -- returning a plain response here would not be
        # awaitable.
        if iscoroutinefunction(self.get_response):
            return self.__acall__(request)
        if self._should_block(request):
            return HttpResponseNotFound()
        return self.get_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        if self._should_block(request):
            return HttpResponseNotFound()
        response = await self.get_response(request)  # type: ignore[misc]
        return cast(HttpResponse, response)

    def _metrics_path(self) -> str:
        if self._metrics_path_cache is None:
            try:
                self._metrics_path_cache = reverse("prometheus-django-metrics")
            except NoReverseMatch:
                self._metrics_path_cache = self._DEFAULT_METRICS_PATH
        return self._metrics_path_cache

    def _should_block(self, request: HttpRequest) -> bool:
        if request.path.rstrip("/") != self._metrics_path().rstrip("/"):
            return False
        return not self._is_in_cluster(request)

    def _is_in_cluster(self, request: HttpRequest) -> bool:
        # Anything a proxy touched came from outside; the ingress is the only
        # proxy in front of these pods and it always sets these.
        for header in FORWARDING_HEADERS:
            if request.META.get(header):
                return False
        try:
            remote_addr = ipaddress.ip_address(
                str(request.META.get("REMOTE_ADDR", "")).strip()
            )
        except ValueError:
            # No/unparseable peer address (scoped IPv6, unix socket): not
            # something we can vouch for, so deny.
            return False
        return any(remote_addr in network for network in self.allowed_networks)

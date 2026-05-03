from typing import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect
from django.http.response import HttpResponseRedirectBase


class HttpResponsePermanentRedirectKeepMethod(HttpResponseRedirectBase):
    """308 Permanent Redirect: tells clients to preserve method and body."""

    status_code = 308


class DomainRedirectMiddleware:
    """
    Permanent-redirects requests served on alternate domains to a canonical host.

    The map is configured via settings.DOMAIN_REDIRECTS as {alt_host: canonical_host}.
    Hostnames are matched case-insensitively and ignore the port. GET/HEAD use 301
    (better understood by crawlers); other methods use 308 so the body is preserved.
    """

    _SAFE_METHODS = frozenset({"GET", "HEAD"})

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.redirects = {
            k.lower(): v for k, v in getattr(settings, "DOMAIN_REDIRECTS", {}).items()
        }

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self.redirects:
            host = request.get_host().split(":")[0].lower()
            target = self.redirects.get(host)
            if target:
                location = f"https://{target}{request.get_full_path()}"
                if request.method in self._SAFE_METHODS:
                    return HttpResponsePermanentRedirect(location)
                return HttpResponsePermanentRedirectKeepMethod(location)
        return self.get_response(request)

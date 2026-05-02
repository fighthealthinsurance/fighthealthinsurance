from typing import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect


class DomainRedirectMiddleware:
    """
    Permanent-redirects requests served on alternate domains to a canonical host.

    The map is configured via settings.DOMAIN_REDIRECTS as {alt_host: canonical_host}.
    Hostnames are matched case-insensitively and ignore the port.
    """

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
                return HttpResponsePermanentRedirect(
                    f"https://{target}{request.get_full_path()}"
                )
        return self.get_response(request)

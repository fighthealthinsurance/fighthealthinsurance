from typing import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponse


def host_is_indexed(request: HttpRequest) -> bool:
    """Whether search engines should index the host this request came in on.

    Only the hosts in settings.SEARCH_INDEXED_HOSTS are; dev, staging, api,
    monitor and the other domains are not."""
    try:
        host = request.get_host().split(":")[0].lower()
    except DisallowedHost:
        return False
    return host in {h.lower() for h in settings.SEARCH_INDEXED_HOSTS}


class NoIndexOffMainHostMiddleware:
    """Sends X-Robots-Tag: noindex on every response from a host that is not
    in settings.SEARCH_INDEXED_HOSTS; robots.txt there disallows everything."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not host_is_indexed(request):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response

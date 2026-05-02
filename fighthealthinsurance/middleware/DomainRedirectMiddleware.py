from django.conf import settings
from django.http import HttpResponsePermanentRedirect

DEFAULT_REDIRECT_DOMAINS = {
    "fuckhealthinsurance.com": "www.fighthealthinsurance.com",
    "www.fuckhealthinsurance.com": "www.fighthealthinsurance.com",
    "fightinsurance.ai": "www.fighthealthinsurance.com",
    "www.fightinsurance.ai": "www.fighthealthinsurance.com",
}


class DomainRedirectMiddleware:
    """Redirects alternate marketing domains to the canonical domain via 301."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.redirect_map = {
            host.lower(): target
            for host, target in getattr(
                settings, "DOMAIN_REDIRECT_MAP", DEFAULT_REDIRECT_DOMAINS
            ).items()
        }

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        target = self.redirect_map.get(host)
        if target:
            scheme = "https" if request.is_secure() else request.scheme
            return HttpResponsePermanentRedirect(
                f"{scheme}://{target}{request.get_full_path()}"
            )
        return self.get_response(request)

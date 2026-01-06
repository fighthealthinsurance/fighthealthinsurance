from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

from loguru import logger


class SessionMiddlewareDynamicDomain(MiddlewareMixin):
    """
    Middleware to update cookie domains dynamically based on the request domain.
    Handles both session and CSRF token cookies.
    Based on https://stackoverflow.com/questions/2116860/django-session-cookie-domain-with-multiple-domains
    """

    def __init__(self, get_response):
        super().__init__(get_response)

    def process_response(self, request, response):
        cookies_to_check = [
            settings.SESSION_COOKIE_NAME,
        ]

        # Add CSRF cookie name if it's being used
        if hasattr(settings, "CSRF_COOKIE_NAME") and settings.CSRF_COOKIE_NAME:
            cookies_to_check.append(settings.CSRF_COOKIE_NAME)

        for cookie_name in cookies_to_check:
            if cookie_name in response.cookies:
                try:
                    # Only proceed if the cookie has a domain attribute
                    if "domain" in response.cookies[cookie_name]:
                        domain_curr = response.cookies[cookie_name]["domain"]

                        request_domain = "." + ".".join(
                            request.get_host().split(":")[0].split(".")[-2:]
                        )

                        if request_domain in settings.SESSION_COOKIE_DOMAIN_DYNAMIC:
                            if domain_curr != request_domain:
                                response.cookies[cookie_name]["domain"] = request_domain
                except Exception as exc:
                    logger.error(
                        f"crash updating domain for {cookie_name} dynamically. Skipped. Error: {exc}"
                    )

        return response

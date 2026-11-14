from django.conf import settings
from django.utils.deprecation import MiddlewareMixin


class PermissionsPolicyMiddleware(MiddlewareMixin):
    """Adds settings.PERMISSIONS_POLICY as the Permissions-Policy header.

    A header a view already set is left as is.
    """

    def process_response(self, request, response):
        policy = getattr(settings, "PERMISSIONS_POLICY", "")
        if policy and "Permissions-Policy" not in response:
            response["Permissions-Policy"] = policy
        return response

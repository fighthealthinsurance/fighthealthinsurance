"""
Middleware to detect and set brand context based on domain or path.
"""

from django.utils.deprecation import MiddlewareMixin

from fighthealthinsurance.brand_config import (
    get_brand_config,
    get_brand_from_domain,
    get_brand_from_path,
)


class BrandMiddleware(MiddlewareMixin):
    """
    Detects brand from request domain or path and attaches to request.

    Priority: path prefix > domain

    Attaches the following to the request object:
    - request.brand_slug: Brand identifier ('fhi' or 'amc')
    - request.brand: Full BrandConfig object
    """

    def process_request(self, request):
        """Detect brand and attach to request."""
        # Check path first (for /appealmyclaims/ prefix)
        brand_from_path = get_brand_from_path(request.path)
        if brand_from_path:
            request.brand_slug = brand_from_path
        else:
            # Fall back to domain detection
            domain = request.get_host().split(":")[0]  # Remove port
            request.brand_slug = get_brand_from_domain(domain)

        # Attach full brand config to request
        request.brand = get_brand_config(request.brand_slug)

        return None

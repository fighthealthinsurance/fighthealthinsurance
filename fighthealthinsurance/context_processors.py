"""Custom context processors for Fight Health Insurance."""

from django.urls import reverse

from fighthealthinsurance.brand_config import get_brand_config


def brand_context(request):
    """
    Add brand configuration to template context.

    Requires BrandMiddleware to run first.

    Provides:
    - brand: Full BrandConfig object
    - is_amc: Boolean indicating if current brand is Appeal My Claims
    - is_fhi: Boolean indicating if current brand is Fight Health Insurance
    - brand_url_names: Dict of brand-aware URL names for key pages
    """
    brand = getattr(request, "brand", None)
    if not brand:
        # Fallback if middleware didn't run
        brand = get_brand_config("fhi")

    # Provide brand-aware URL names so templates can use {% url brand_url_names.privacy_policy %}
    # This ensures AMC pages link to AMC privacy policy, FHI pages link to FHI privacy policy
    # Define all URL keys that should be brand-aware
    # Only include keys that exist as named URLs in BOTH FHI and AMC routes
    BRAND_URL_KEYS = [
        "privacy_policy",
        "tos",
        "about",
        "contact",
        "root",
        "scan",
    ]

    url_prefix = "amc_" if brand.slug == "amc" else ""
    brand_url_names = {key: f"{url_prefix}{key}" for key in BRAND_URL_KEYS}

    return {
        "brand": brand,
        "is_amc": brand.slug == "amc",
        "is_fhi": brand.slug == "fhi",
        "brand_url_names": brand_url_names,
    }


def form_persistence_context(request):
    """
    Add context variables needed for form persistence.

    This provides:
    - fhi_session_key: The denial UUID from the session (used to scope localStorage)
    - fhi_request_method: The HTTP request method (GET or POST)

    These are used by formPersistence.ts to determine whether to restore values
    from localStorage.
    """
    return {
        "fhi_session_key": request.session.get("denial_uuid", ""),
        "fhi_request_method": request.method,
    }


def canonical_url_context(request):
    """
    Add canonical URL context variable.

    Now brand-aware - uses the brand's canonical domain instead of hardcoded FHI.

    This provides:
    - canonical_url: The canonical URL for the current page, pointing to the
      appropriate brand domain (fighthealthinsurance.com or appealmyclaims.com)

    Views can override the canonical URL by setting request.canonical_url before
    this context processor runs (e.g., in middleware or view code).

    The canonical URL uses Django's reverse() function with the resolver_match
    to get the path exactly as defined in URL patterns, ensuring consistent
    trailing slash handling.

    Query parameters are stripped from canonical URLs to prevent duplicate content
    issues from tracking parameters and other query string variations.
    """
    # Allow views to override the canonical URL by setting it on the request
    if hasattr(request, "canonical_url") and request.canonical_url:
        return {"canonical_url": request.canonical_url}

    # Get canonical domain from brand config
    brand = getattr(request, "brand", None)
    if brand:
        canonical_domain = brand.canonical_domain
    else:
        canonical_domain = "https://www.fighthealthinsurance.com"

    # Try to use resolver_match to get the canonical path from URL patterns
    resolver_match = getattr(request, "resolver_match", None)
    if resolver_match and resolver_match.url_name:
        try:
            # Use reverse() to get the path exactly as defined in urlpatterns
            # This ensures consistent trailing slash handling
            namespace = resolver_match.namespace
            url_name = resolver_match.url_name
            if namespace:
                url_name = f"{namespace}:{url_name}"
            path = reverse(
                url_name, args=resolver_match.args, kwargs=resolver_match.kwargs
            )
        except Exception:
            # Fall back to the request path if reverse fails
            path = request.path
    else:
        # Fall back to the request path if no resolver_match
        path = request.path

    # Strip /appealmyclaims prefix from path for canonical URL
    # (canonical should point to root path on brand's domain)
    if path.startswith("/appealmyclaims/"):
        path = path[len("/appealmyclaims") :]
    elif path == "/appealmyclaims":
        path = "/"

    # Build the canonical URL without query parameters
    canonical = f"{canonical_domain}{path}"

    return {
        "canonical_url": canonical,
    }

"""Custom context processors for Fight Health Insurance."""

from django.urls import reverse


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

    This provides:
    - canonical_url: The canonical URL for the current page, always pointing to
      https://www.fighthealthinsurance.com regardless of the domain used to access
      the site.

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

    # Build the canonical URL without query parameters
    canonical = f"{canonical_domain}{path}"

    return {
        "canonical_url": canonical,
    }

"""Custom context processors for Fight Health Insurance."""

from urllib.parse import urlparse, urlunparse


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
      the site. Preserves the full path and query string from the original request.

    The canonical URL uses the exact path that was successfully resolved by Django,
    ensuring the canonical URL points to a valid, working URL.
    """
    # Get the full current URL
    current_url = request.build_absolute_uri()
    
    # Parse the URL to replace just the scheme and netloc (domain)
    parsed = urlparse(current_url)
    
    # Use the path as-is - it's already been validated by Django's URL resolver
    # This ensures the canonical URL points to a URL that actually works
    path = parsed.path
    
    # Rebuild URL with canonical domain
    canonical = urlunparse((
        'https',                           # scheme
        'www.fighthealthinsurance.com',   # netloc (domain)
        path,                              # path as resolved by Django
        parsed.params,                     # params
        parsed.query,                      # query string
        ''                                 # fragment (not included in canonical)
    ))
    
    return {
        "canonical_url": canonical,
    }

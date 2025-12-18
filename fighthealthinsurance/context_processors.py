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

    This handles pages with multiple routes (e.g., with/without trailing slash)
    by using the actual requested URL path, ensuring the canonical URL matches
    what the user accessed.
    """
    # Get the full current URL
    current_url = request.build_absolute_uri()
    
    # Parse the URL to replace just the scheme and netloc (domain)
    parsed = urlparse(current_url)
    
    # Rebuild URL with canonical domain
    canonical = urlunparse((
        'https',                           # scheme
        'www.fighthealthinsurance.com',   # netloc (domain)
        parsed.path,                       # path
        parsed.params,                     # params
        parsed.query,                      # query string
        ''                                 # fragment (not included in canonical)
    ))
    
    return {
        "canonical_url": canonical,
    }

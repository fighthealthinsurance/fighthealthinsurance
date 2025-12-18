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

    For pages with multiple routes (e.g., with/without trailing slash), this
    normalizes to a single canonical version by ensuring paths have trailing slashes
    (following Django convention), except for paths that look like files or specific
    API endpoints.
    """
    # Get the full current URL
    current_url = request.build_absolute_uri()
    
    # Parse the URL to replace just the scheme and netloc (domain)
    parsed = urlparse(current_url)
    
    # Normalize the path to ensure consistent canonical URLs
    # Add trailing slash if not present and path doesn't look like a file
    path = parsed.path
    if path and not path.endswith('/'):
        # Don't add trailing slash if it looks like a file (has extension)
        # or if it's an API endpoint that shouldn't have one
        if '.' not in path.split('/')[-1]:
            path = path + '/'
    
    # Rebuild URL with canonical domain and normalized path
    canonical = urlunparse((
        'https',                           # scheme
        'www.fighthealthinsurance.com',   # netloc (domain)
        path,                              # normalized path
        parsed.params,                     # params
        parsed.query,                      # query string
        ''                                 # fragment (not included in canonical)
    ))
    
    return {
        "canonical_url": canonical,
    }

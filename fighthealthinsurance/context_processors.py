"""Custom context processors for Fight Health Insurance."""


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
        'fhi_session_key': request.session.get('denial_uuid', ''),
        'fhi_request_method': request.method,
    }

"""
Human verification utilities for session-based captcha tracking.

This module provides a mixin and decorator to enforce that users have completed
human verification (captcha) before accessing certain views. This helps prevent
automated tools and fuzzers from accessing downstream pages in the appeal flow.
"""

from functools import wraps

from django.http import HttpRequest, HttpResponse


# The friendly message returned when verification is missing
TEAPOT_MESSAGE = """Hi Friend, your browser appears to be misbehaving. If you've encoutered this error while trying to appeal an insurance claim e-mail support42@fighthealthinsurance.com and we'll look into it. Similarily if your performing fuzzing or other activities please e-mail support42@fighthealthinsurance.com and take a look at our terms of service in the meantime. kthnx byeeeee!"""


def is_human_verified(request: HttpRequest) -> bool:
    """
    Check if the request session indicates human verification has been completed.

    Args:
        request: The Django HttpRequest object

    Returns:
        True if the session has human_verified=True, False otherwise
    """
    return request.session.get("human_verified", False) is True


def create_teapot_response() -> HttpResponse:
    """
    Create a 418 I'm a teapot response with the friendly message.

    Returns:
        HttpResponse with status 418 and text/plain content type
    """
    return HttpResponse(
        TEAPOT_MESSAGE,
        content_type="text/plain",
        status=418,
    )


class HumanVerificationRequiredMixin:
    """
    Mixin for class-based views that require prior human verification.

    Add this mixin to views that should only be accessible after completing
    the initial scan flow with captcha verification. If the session lacks
    the human_verified flag, returns a 418 I'm a teapot response.

    Example:
        class FindNextSteps(HumanVerificationRequiredMixin, generic.FormView):
            ...
    """

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not is_human_verified(request):
            return create_teapot_response()
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


def require_human_verification(view_func):
    """
    Decorator for function-based views requiring human verification.

    Use this decorator on function-based views that should only be accessible
    after completing the initial scan flow with captcha verification.

    Example:
        @require_human_verification
        def my_view(request):
            ...
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not is_human_verified(request):
            return create_teapot_response()
        return view_func(request, *args, **kwargs)

    return wrapper


def set_human_verified(request: HttpRequest) -> None:
    """
    Mark the session as having completed human verification.

    Call this after successful captcha validation to allow access to
    downstream views that require verification.

    Args:
        request: The Django HttpRequest object
    """
    from django.utils import timezone

    request.session["human_verified"] = True
    request.session["human_verified_at"] = timezone.now().isoformat()

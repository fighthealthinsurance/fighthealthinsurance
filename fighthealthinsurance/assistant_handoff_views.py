"""The page an assistant's link opens: /from-your-assistant.

The MCP server's prepare_appeal tool returns
``https://www.fighthealthinsurance.com/from-your-assistant#<code>``. Browsers
never send the part after ``#`` to a server, so the access log, Sentry and
Cloudflare see only the path. The page's first script moves the code into
a form field and clears it from the address bar.

``GET`` shows what happened and one button; link previewers and email
scanners fetch links but never press buttons, so they can't use one up.
``POST`` (the button) opens the link: assistant_handoff.claim_handoff deletes
the stored copy and returns its text, and the usual appeal form is rendered
with the text in the letter box, the way the server-side OCR path renders it.
No Denial is created here; the person submits the ordinary form to /process.

Both answer 404 unless MCP_SERVER_ENABLED and MCP_PREPARE_APPEAL_ENABLED are
both on, read on every request; while off, the 404 is this page's own "doesn't
open a form" state, so an outstanding link still has its code cleared and
meets no analytics. No response here is cached, sends a referrer to another
site or is indexed, the landing page loads no third-party script (base.html's
``no_third_party_scripts``), and an error here reports no local variables.
"""

from typing import Any, Optional

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.debug import SafeExceptionReporterFilter

from fighthealthinsurance.assistant_handoff import claim_handoff, new_binder, v2_enabled

LANDING_TEMPLATE = "assistant_handoff.html"
# Session keys. The binder is this browser's secret for binding links; the
# other two say the open form came from an assistant, for /process to read.
BINDER_KEY = "assistant_handoff_binder"
CHANNEL_KEY = "assistant_handoff_channel"
CLIENT_KEY = "assistant_handoff_client"


def session_binder(request: HttpRequest) -> str:
    binder = request.session.get(BINDER_KEY)
    if not isinstance(binder, str) or not binder:
        binder = new_binder()
        request.session[BINDER_KEY] = binder
    return binder


def handoff_context_for(request: HttpRequest) -> Optional[dict[str, str]]:
    """What the opened form carried from the assistant, read once by
    /process: the channel and the client label, or None for a plain intake.
    The keys are cleared so a later case in the session does not inherit them."""
    channel = request.session.pop(CHANNEL_KEY, None)
    client = request.session.pop(CLIENT_KEY, "")
    if channel != "assistant":
        return None
    return {"channel": "assistant", "assistant_client": str(client or "")}


def handoff_enabled() -> bool:
    return bool(
        getattr(settings, "MCP_SERVER_ENABLED", False)
        and getattr(settings, "MCP_PREPARE_APPEAL_ENABLED", False)
    )


def _private(response: HttpResponse) -> HttpResponse:
    """No referrer to another site, and no index.

    same-origin, not no-referrer: under no-referrer a browser sends
    "Origin: null" with a form's POST, which Django's CSRF check refuses, so
    neither the button here nor the appeal form it opens could be submitted.
    A Referer never carries the part after "#" either way.
    """
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


class _NoLocalVariables(SafeExceptionReporterFilter):
    """Django's error report (the emails to ADMINS in production) without any
    frame's local variables: here they can hold the code or the letter, in
    this view, in assistant_handoff and in the template engine rendering the
    form. The traceback itself stays."""

    def get_traceback_frame_variables(self, request: Any, tb_frame: Any) -> Any:
        return []


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters("token"), name="dispatch")
class AssistantHandoffView(View):
    http_method_names = ["get", "post"]

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponseBase:
        request.exception_reporter_filter = _NoLocalVariables()  # type: ignore[attr-defined]
        if not handoff_enabled():
            # The ordinary 404 page would run the analytics tags with the
            # code still in the address bar; this one clears it first.
            return self._landing(request, dead=True)
        return super().dispatch(request, *args, **kwargs)

    def _landing(self, request: HttpRequest, dead: bool) -> HttpResponse:
        return _private(
            render(
                request,
                LANDING_TEMPLATE,
                {
                    "no_third_party_scripts": True,
                    "dead": dead,
                    "assistant_stop_line": v2_enabled(),
                },
                status=404 if dead else 200,
            )
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._landing(request, dead=False)

    def post(self, request: HttpRequest) -> HttpResponse:
        token = request.POST.get("token", "")
        if v2_enabled():
            content = claim_handoff(token, binder=session_binder(request))
        else:
            content = claim_handoff(token)
        if content is None:
            return self._landing(request, dead=True)
        if v2_enabled():
            request.session[CHANNEL_KEY] = "assistant"
            request.session[CLIENT_KEY] = content.client
        return _private(
            render(
                request,
                "scrub.html",
                {
                    "ocr_result": content.letter,
                    "upload_more": True,
                    "from_assistant": True,
                    # Carried in the form's hidden fields to /process, which
                    # keeps them in the session for the review step, the way
                    # a treatment guide's are.
                    "default_procedure": content.procedure,
                    "default_condition": content.condition,
                },
            )
        )

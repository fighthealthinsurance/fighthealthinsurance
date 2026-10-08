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

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.debug import SafeExceptionReporterFilter

from fighthealthinsurance import consent
from fighthealthinsurance.assistant_handoff import (
    HANDOFF_TTL,
    HandoffContent,
    claim_handoff,
    client_label,
    new_binder,
    v2_enabled,
)

LANDING_TEMPLATE = "assistant_handoff.html"
# The binder is this browser's secret for binding links. It lives in its own
# cookie, never in the session (whose store is a database table in clear);
# the server keeps only a digest. Scoped to this page's paths and the link's
# lifetime.
BINDER_COOKIE = "fhi_handoff_binder"
BINDER_COOKIE_PATH = "/from-your-assistant"
# The forms an opened link filled in, for /process to name the assistant in
# the consent record. Each is kept in the session under a fresh random key,
# which only that form carries, in its hidden FORM_FIELD: so only a
# submission of that form names the assistant, never whichever case the
# browser sends next, and two forms open in two tabs each keep their own.
# The session keeps the newest few, each for a day from the last time it
# was made, rendered again for the flow's Back link, or went through. A key
# names the assistant for one case only: the case its form first goes
# through for, or the case a form continuing one was rendered for, kept in
# its entry (use_site_form).
FORMS_KEY = "assistant_handoff_forms"
FORM_FIELD = "assistant_form"
FORMS_KEPT = 5
FORM_TTL = timedelta(hours=24)
# A hidden field of the same form: its default_procedure and
# default_condition are what the link left, and blank means none.
DEFAULTS_GIVEN_FIELD = "defaults_given"
# What an earlier version kept instead, for the next submission of any form
# in the session. Read by nothing now; dropped where a form's entry is.
_OLD_KEYS = ("assistant_handoff_channel", "assistant_handoff_client")


def request_binder(request: HttpRequest) -> Optional[str]:
    binder = request.COOKIES.get(BINDER_COOKIE)
    return binder if isinstance(binder, str) and len(binder) == 43 else None


def set_binder_cookie(response: HttpResponse, binder: str) -> None:
    response.set_cookie(
        BINDER_COOKIE,
        binder,
        max_age=int(HANDOFF_TTL.total_seconds()),
        path=BINDER_COOKIE_PATH,
        secure=bool(getattr(settings, "SESSION_COOKIE_SECURE", True)),
        httponly=True,
        samesite="Lax",
    )


def _open_forms(request: HttpRequest) -> dict[str, dict[str, Any]]:
    """The session's open forms by key, without any past FORM_TTL, the
    newest FORMS_KEPT, as a new dict to store back."""
    now = time.time()
    kept = request.session.get(FORMS_KEY)
    if not isinstance(kept, dict):
        return {}
    live = [
        (key, entry)
        for key, entry in kept.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("at"), (int, float))
        and now - entry["at"] <= FORM_TTL.total_seconds()
    ]
    live.sort(key=lambda item: item[1]["at"], reverse=True)
    return dict(live[:FORMS_KEPT])


def _store_forms(request: HttpRequest, forms: dict[str, dict[str, Any]]) -> None:
    for key in _OLD_KEYS:
        request.session.pop(key, None)
    if forms:
        request.session[FORMS_KEY] = forms
    else:
        request.session.pop(FORMS_KEY, None)


@dataclass(frozen=True)
class SiteForm:
    """A submitted form that an assistant's link filled in: its key, the
    label the assistant gave itself (self-reported, for analytics only:
    mcp_server._client_name), and, once the key is bound to a case (it went
    through for one, or its form continues one: the flow's Back link to
    /scan), that case's id."""

    key: str
    client: str
    case: Optional[int] = None


def mark_site_form(
    request: HttpRequest, client: str, case: Optional[int] = None
) -> str:
    """Keep, in the session, that the form about to be rendered came from an
    assistant whose label is client, under a fresh key; returns the key for
    its hidden field. With case, the form continues that case and names the
    assistant for it alone."""
    forms = _open_forms(request)
    key = secrets.token_urlsafe(16)
    entry: dict[str, Any] = {"client": client, "at": time.time()}
    if case is not None:
        entry["case"] = int(case)
    forms = {key: entry, **forms}
    _store_forms(request, dict(list(forms.items())[:FORMS_KEPT]))
    return key


def mark_continued_form(request: HttpRequest, denial: Any) -> Optional[str]:
    """A key for the form the flow's Back link to /scan renders for a case an
    assistant brought in, naming that assistant for that case only; None
    for a case the site brought in, or with v2 off. Only the Back link,
    whose reference names the case (views.build_back_url), continues a case:
    a plain /scan form names no assistant, whatever case it ends up reusing.

    A key the session already keeps for this case and assistant is used
    again, so loading the page again doesn't fill the session with new ones
    and push other tabs' forms out."""
    if not v2_enabled():
        return None
    client = consent.assistant_that_brought(denial)
    if client is None:
        return None
    forms = _open_forms(request)
    for key, entry in forms.items():
        if entry.get("case") == denial.denial_id and (
            client_label(entry.get("client", "")) == client
        ):
            # Its day starts again, as the newest.
            forms[key] = {**entry, "at": time.time()}
            _store_forms(request, forms)
            return key
    return mark_site_form(request, client, case=denial.denial_id)


def _posted_form_key(request: HttpRequest) -> str:
    key = request.POST.get(FORM_FIELD, "")
    return key if isinstance(key, str) else ""


def handoff_context_for(request: HttpRequest) -> Optional[SiteForm]:
    """The form being submitted, when an assistant's link filled it in, for
    /process's consent record; None for a plain intake. Only a form an
    opened link filled in (render_site_form), or the Back link rendered for
    a case one brought in, carries a key the session kept, and only that
    form can name the assistant; a field a request makes up names none.

    Read, not used: /process decides with use_site_form once the submission
    goes through, so a page sent back with an error keeps the key, unbound,
    for the corrected retry."""
    key = _posted_form_key(request)
    if not key:
        return None
    entry = _open_forms(request).get(key)
    if entry is None:
        return None
    case = entry.get("case")
    return SiteForm(
        key=key,
        client=client_label(entry.get("client", "")),
        case=case if isinstance(case, int) else None,
    )


def use_site_form(request: HttpRequest, form: SiteForm, denial_id: int) -> bool:
    """Whether this submission of the form, gone through as case denial_id,
    names the assistant. The first time a key goes through, its entry in the
    session is bound to that case: it names the assistant for that case
    again (a reload sends the form again, and so does the browser's Back to
    it, or a second press of Submit) and never for another. A form
    continuing a case is bound to it from the start. Both submissions of a
    double-click name the assistant, so the case the person goes on with
    does. Each time the key goes through, its day starts again, so the form
    sent again shortly after a submission late in that day still names the
    assistant."""
    # Accepted limit: every request saves the session whole, so two
    # submissions at the same instant can leave a used key unbound, and that
    # form sent again for another case would name the assistant too. That
    # can only miscount the analytics label; it grants nothing.
    if form.case is not None and form.case != denial_id:
        return False
    forms = _open_forms(request)
    entry = forms.get(form.key)
    if entry is not None:
        forms[form.key] = {**entry, "case": int(denial_id), "at": time.time()}
        _store_forms(request, forms)
    return True


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


def render_landing(
    request: HttpRequest, dead: bool, after_a_press: bool = False
) -> HttpResponse:
    """The landing page; after_a_press is the terms page's buttons finding
    the link already used."""
    return _private(
        render(
            request,
            LANDING_TEMPLATE,
            {
                "no_third_party_scripts": True,
                "dead": dead,
                "after_a_press": after_a_press,
                "assistant_stop_line": v2_enabled(),
            },
            status=404 if dead else 200,
        )
    )


def render_site_form(
    request: HttpRequest,
    content: HandoffContent,
    letter: Optional[str] = None,
    form: Any = None,
) -> HttpResponse:
    """The usual appeal form, filled in from an opened link."""
    context: dict[str, Any] = {
        "ocr_result": letter if letter else content.letter,
        "upload_more": True,
        "from_assistant": True,
        # Carried in the form's hidden fields to /process, which keeps them
        # in the session for the review step, the way a treatment guide's are.
        "default_procedure": content.procedure,
        "default_condition": content.condition,
    }
    # Both always carried, blank too, whatever the flags say: this form says
    # what the treatment and condition are, and blank means none, so
    # /process doesn't keep one an earlier form left for a case it reuses.
    # A v2 flag turned off after the terms page opened must not undo a
    # clearing done there.
    context["defaults_given"] = True
    if v2_enabled():
        context["assistant_form"] = mark_site_form(request, content.client)
    if form is not None:
        context["form"] = form
    return _private(render(request, "scrub.html", context))


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
        return render_landing(request, dead)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._landing(request, dead=False)

    def _bind(self, request: HttpRequest, token: str) -> HttpResponse:
        """The page script's first request: bind the link to this browser
        without using it up. Answers {"bound": true/false}; a false means the
        page shows its used-link state."""
        binder = request_binder(request) or new_binder()
        content = claim_handoff(token, binder=binder, consume=False)
        response = _private(JsonResponse({"bound": content is not None}))
        if content is not None:
            set_binder_cookie(response, binder)
        return response

    def post(self, request: HttpRequest) -> HttpResponse:
        token = request.POST.get("token", "")
        # Not gated on the flag: a page served before a flag flip or by
        # another pod mid-rollout must still bind, and a bound link still open.
        if request.POST.get("bind") == "1":
            return self._bind(request, token)
        binder = request_binder(request)
        from fighthealthinsurance import assistant_terms_views

        if binder is not None and assistant_terms_views.chat_path_on():
            # A chat link is read, not used up, until the person agrees.
            peek = claim_handoff(token, binder=binder, consume=False)
            if peek is None:
                return self._landing(request, dead=True)
            if assistant_terms_views.opens_terms_page(peek):
                return assistant_terms_views.render_terms(
                    request,
                    token,
                    peek.letter,
                    procedure=peek.procedure,
                    condition=peek.condition,
                )
        content = claim_handoff(token, binder=binder)
        if content is None:
            return self._landing(request, dead=True)
        return render_site_form(request, content)

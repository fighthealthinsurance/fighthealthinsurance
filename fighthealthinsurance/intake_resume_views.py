"""The resume pages the "you left before finishing" email links to.

``/continue/<token>`` is the address in the email. It keeps the token's
digest in the session and redirects to ``/continue`` straight away, so the
token is never in the address bar of a page that renders. ``/continue`` asks
for the email address the case was started with and, when it matches, opens
the case at the step it reached. The design is in ``intake_resume``.
"""

from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from loguru import logger

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance import intake_resume
from fighthealthinsurance.views import (
    DENIAL_REF_QUERY_PARAM,
    issue_denial_ref_token,
    remember_denial_ref_email,
)

# The session holds the digest of the link being opened, never the token.
RESUME_LINK_SESSION_KEY = "intake_resume_link"

TEMPLATE = "intake_resume.html"


def _no_referrer(response):
    response["Referrer-Policy"] = "no-referrer"
    return response


@method_decorator(never_cache, name="dispatch")
class IntakeResumeLinkView(View):
    """The address in the email. Takes the token out of the address bar."""

    def get(self, request, token):
        if not intake_resume.enabled():
            raise Http404()
        digest = (
            intake_resume.token_digest(token)
            if intake_resume.plausible_token(token)
            else ""
        )
        request.session[RESUME_LINK_SESSION_KEY] = digest
        return _no_referrer(redirect("intake_resume"))


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters("email"), name="dispatch")
class IntakeResumeView(View):
    """Ask for the email address, then open the case at its step."""

    def _dead(self, request):
        request.session.pop(RESUME_LINK_SESSION_KEY, None)
        return _no_referrer(
            render(
                request,
                TEMPLATE,
                {"link_works": False, "link_days": intake_resume.link_days()},
                status=404,
            )
        )

    def _ask(self, request, form, wrong_email=False):
        return _no_referrer(
            render(
                request,
                TEMPLATE,
                {
                    "link_works": True,
                    "form": form,
                    "wrong_email": wrong_email,
                    "tries": intake_resume.RESUME_LINK_MAX_WRONG_EMAILS,
                },
            )
        )

    def get(self, request):
        if not intake_resume.enabled():
            raise Http404()
        digest = request.session.get(RESUME_LINK_SESSION_KEY)
        if intake_resume.live_point(digest) is None:
            return self._dead(request)
        return self._ask(request, core_forms.IntakeResumeForm())

    def post(self, request):
        if not intake_resume.enabled():
            raise Http404()
        digest = request.session.get(RESUME_LINK_SESSION_KEY)
        form = core_forms.IntakeResumeForm(request.POST)
        if not form.is_valid():
            if intake_resume.live_point(digest) is None:
                return self._dead(request)
            return self._ask(request, form)
        email = form.cleaned_data["email"]
        outcome, point = intake_resume.open_case(digest, email)
        if outcome == intake_resume.WRONG_EMAIL:
            return self._ask(request, core_forms.IntakeResumeForm(), wrong_email=True)
        if outcome != intake_resume.OPENED or point is None:
            return self._dead(request)
        denial = point.denial
        # Opening a case is a sign in: a fresh session key, so an id planted
        # in this browser beforehand never gains the case.
        request.session.cycle_key()
        request.session.pop(RESUME_LINK_SESSION_KEY, None)
        # Bound the way starting the case binds it (InitialProcessView).
        request.session["denial_uuid"] = str(denial.uuid)
        request.session["denial_id"] = int(denial.denial_id)
        remember_denial_ref_email(request.session, denial.denial_id, email)
        ref = issue_denial_ref_token(
            request, denial.denial_id, email, denial.semi_sekret
        )
        logger.info(
            f"intake resume: denial {denial.denial_id} reopened at {point.step}"
        )
        step = point.step
        if step not in intake_resume.RESUME_STEPS:
            step = intake_resume.FIRST_STEP
        target = reverse(step)
        if ref:
            target = f"{target}?{urlencode({DENIAL_REF_QUERY_PARAM: ref})}"
        return _no_referrer(redirect(target))

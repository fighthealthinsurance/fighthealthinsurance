"""The terms page a chat link opens, and the emailed way back to the letters.

A chat link (payload kind "chat", made by draft_appeal_in_chat) opens here
instead of the appeal form once the landing page has bound it to this
browser. The person checks the letter and the few words the assistant sent
on what was denied and the condition (two short fields they can change or
clear), removes personal details, says who the appeal is for, ticks the
intake page's boxes and gives an email. On agree, in this order: the bot
check, the per-address cap (assistant_ip_limit), one generation from the
assistant budget (spend.reserve_generation). Any refusal opens the
filled-in site form instead. Then the link is used up, the Denial is made
the way /process makes it, the boxes are recorded, the draft is tied to the
denial with the two short fields as the person left them, the continue link
is emailed once and AssistantAppealWorkflow starts. The draft keeps the
reservation, so a run that ends with no letters gives it back.

"Finish on this site instead" opens the site form and tells the assistant
on_site; the two short fields go with it as the person left them, the way
the site form carries what a site link sent. The appeal submitted there
still names the assistant in its consent record, with finish_in "site"
(consent.py). Everything here needs draft_in_chat_enabled(); with it off a
chat link opens the site form as before.

Nothing here logs the letter, the email or a token.
"""

import dataclasses
import hashlib
from typing import Any, Optional
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from loguru import logger

from fighthealthinsurance import (
    assistant_continue,
    assistant_drafts,
    assistant_ip_limit,
    consent,
)
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.assistant_handoff import (
    HandoffContent,
    claim_handoff,
    one_line,
    v2_enabled,
)
from fighthealthinsurance.assistant_handoff_views import (
    _NoLocalVariables,
    _private,
    handoff_enabled,
    render_landing,
    render_site_form,
    request_binder,
)
from fighthealthinsurance.ml import spend

TERMS_TEMPLATE = "assistant_terms.html"
AGREED_TEMPLATE = "assistant_agreed.html"
CONTINUE_TEMPLATE = "assistant_continue.html"

# Field ids for the error list at the top of the page, as on /process.
_FIELD_IDS = {
    "denial_text": "denial_text",
    "email": "email",
    "zip": "store_zip",
    "on_behalf": "on_behalf_me",
    "pii": "pii",
    "privacy": "privacy",
    "tos": "tos",
    "personalonly": "personalonly",
    "captcha": "",
}
# Bot-check errors that ask the person to tick the box again: unticked, a
# tick that ran out or was sent twice, or Google out of reach (which says
# nothing about the person). Anything else is a refusal.
_ASK_AGAIN = frozenset(("required", core_forms.CAPTCHA_EXPIRED, "captcha_error"))
# What this browser's session remembers after agreeing: a digest of the link
# it used up (never the link) and what the agreed page said, so pressing
# Agree again later (after Back, say) shows that page again instead of a
# dead link. It can't help a second press sent while the first is still
# running: that request reads the session before the first one writes it,
# and when the browser drops the first request its session is never saved.
# The page's own one-press guard is what stops a double tap.
AGREED_KEY = "assistant_agreed"
_FIELDS_WITH_A_MESSAGE = frozenset(
    ("denial_text", "email", "on_behalf", "pii", "privacy", "tos", "personalonly")
)
# The page's two short fields under the letter: what the assistant said was
# denied, and the condition. Shown filled in with what it sent; the person
# can change or clear them, and blank means none.
PROCEDURE_FIELD = "procedure"
CONDITION_FIELD = "condition"


def chat_path_on() -> bool:
    return v2_enabled() and assistant_drafts.draft_in_chat_enabled()


def opens_terms_page(content: HandoffContent) -> bool:
    return content.kind == "chat" and chat_path_on()


def short_field(value: str) -> str:
    """One of the two short fields, cleaned the way prepare_appeal cleans
    what an assistant sends (assistant_handoff.one_line) and cut at what a
    draft keeps (assistant_drafts.FIELD_MAX_CHARS)."""
    return one_line(value)[: assistant_drafts.FIELD_MAX_CHARS].strip()


def _as_left(request: HttpRequest, content: HandoffContent) -> HandoffContent:
    """What the assistant sent, with the treatment and condition as the
    person left them on the terms page, through short_field; a cleared one
    is "". A press from a page without the fields (one served before they
    were there) keeps what the assistant sent."""

    def edited(name: str, sent: str) -> str:
        value = request.POST.get(name)
        return sent if value is None else short_field(value)

    return dataclasses.replace(
        content,
        procedure=edited(PROCEDURE_FIELD, content.procedure),
        condition=edited(CONDITION_FIELD, content.condition),
    )


def _error_summary(form: Any) -> list[dict[str, str]]:
    if form is None or not form.is_bound:
        return []
    order = list(_FIELD_IDS)
    summary: list[dict[str, str]] = []
    for name in sorted(
        form.errors, key=lambda n: order.index(n) if n in order else len(order)
    ):
        label = ""
        if name in form.fields and name not in _FIELDS_WITH_A_MESSAGE:
            label = str(form[name].label or "")
        summary.extend(
            {
                "field_id": _FIELD_IDS.get(name, ""),
                "text": f"{label}: {error}" if label else str(error),
            }
            for error in form.errors[name]
        )
    return summary


def _site_form_filled(request: HttpRequest) -> Any:
    """The site form, filled with what the terms page posted: the email,
    ZIP and the optional choices as the person left them."""
    post = request.POST
    return core_forms.DenialForm(
        initial={
            "email": post.get("email", ""),
            "zip": post.get("zip", ""),
            "use_external_models": bool(post.get("use_external_models")),
            "store_raw_email": bool(post.get("store_raw_email")),
            "subscribe": bool(post.get("subscribe")),
        }
    )


def render_terms(
    request: HttpRequest,
    token: str,
    letter: str,
    form: Any = None,
    maybe_twice: bool = False,
    procedure: str = "",
    condition: str = "",
) -> HttpResponse:
    """The terms page, with the two short fields filled in with procedure
    and condition, cleaned as they will be kept."""
    if form is None:
        form = core_forms.AssistantTermsForm()
    return _private(
        render(
            request,
            TERMS_TEMPLATE,
            {
                "no_third_party_scripts": True,
                "token": token,
                "ocr_result": letter,
                "procedure": short_field(procedure),
                "condition": short_field(condition),
                "short_field_max": assistant_drafts.FIELD_MAX_CHARS,
                "form": form,
                "on_behalf_choices": core_forms.AssistantTermsForm.ON_BEHALF_CHOICES,
                "error_summary": _error_summary(form),
                "captcha_enabled": form._is_recaptcha_enabled(),
                "maybe_twice": maybe_twice,
            },
        )
    )


def _code_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _agreed_here(request: HttpRequest, token: str) -> Optional[dict[str, bool]]:
    """What the agreed page said, if this browser agreed with this link."""
    agreed = request.session.get(AGREED_KEY)
    if not token or not isinstance(agreed, dict):
        return None
    if agreed.get("code") != _code_digest(token):
        return None
    return {
        "started": bool(agreed.get("started")),
        "emailed": bool(agreed.get("emailed")),
    }


def render_agreed(request: HttpRequest, started: bool, emailed: bool) -> HttpResponse:
    return _private(
        render(
            request,
            AGREED_TEMPLATE,
            {
                "no_third_party_scripts": True,
                "started": started,
                "emailed": emailed,
                "link_days": assistant_continue.link_days(),
            },
        )
    )


@method_decorator(never_cache, name="dispatch")
@method_decorator(
    sensitive_post_parameters(
        "token",
        "email",
        "denial_text",
        "zip",
        "fname",
        "lname",
        PROCEDURE_FIELD,
        CONDITION_FIELD,
    ),
    name="dispatch",
)
class AssistantAgreeView(View):
    """The terms page's two buttons: agree, or finish on this site."""

    http_method_names = ["post"]

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponseBase:
        request.exception_reporter_filter = _NoLocalVariables()  # type: ignore[attr-defined]
        if not handoff_enabled():
            return render_landing(request, dead=True)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request: HttpRequest) -> HttpResponse:
        token = request.POST.get("token", "")
        agreed = _agreed_here(request, token)
        if agreed is not None:
            return render_agreed(request, **agreed)
        binder = request_binder(request)
        content = claim_handoff(token, binder=binder, consume=False) if binder else None
        if content is None:
            return self._used(request, token)
        letter = request.POST.get("denial_text", "") or content.letter
        # From here on, the treatment and condition are the person's.
        content = _as_left(request, content)
        if request.POST.get("finish") == "site" or not opens_terms_page(content):
            return self._to_site(request, token, binder, content, letter)
        form = core_forms.AssistantTermsForm(request.POST)
        if not form.is_valid():
            codes = {e.code for e in form.errors.as_data().get("captcha", [])}
            if not codes <= _ASK_AGAIN:
                # A token Google rejected is a refusal like the others; an
                # unticked, expired or twice-sent tick just asks again.
                return self._to_site(request, token, binder, content, letter, form)
            twice = core_forms.CAPTCHA_EXPIRED in codes
            if twice and claim_handoff(token, binder=binder, consume=False) is None:
                # The same tick sent twice: the other press used the link.
                return self._used(request, token)
            # The other press may still be on its way to using it.
            return render_terms(
                request,
                token,
                letter,
                form,
                maybe_twice=twice,
                procedure=content.procedure,
                condition=content.condition,
            )
        draft = assistant_drafts.waiting_draft(content.draft)
        if draft is None:
            # Nothing the assistant could collect letters from.
            return self._to_site(request, token, binder, content, letter, form)
        taken = assistant_ip_limit.take(request)
        if taken is None:
            return self._to_site(request, token, binder, content, letter, form)
        reservation = spend.reserve_generation()
        if reservation is None:
            assistant_ip_limit.give_back(taken)
            return self._to_site(request, token, binder, content, letter, form)
        if claim_handoff(token, binder=binder) is None:
            # Another request used the link first.
            assistant_ip_limit.give_back(taken)
            spend.release_generation(reservation)
            return self._used(request, token)
        try:
            denial = self._create_denial(request, form)
        except Exception:
            assistant_ip_limit.give_back(taken)
            spend.release_generation(reservation)
            raise
        return self._agreed(
            request, token, form, content, draft, denial, reservation, taken
        )

    def _to_site(
        self,
        request: HttpRequest,
        token: str,
        binder: Optional[str],
        content: HandoffContent,
        letter: str,
        form: Any = None,
    ) -> HttpResponse:
        """The site's own form, filled in, with the treatment and condition
        as the person left them; the assistant sees on_site."""
        if claim_handoff(token, binder=binder) is None:
            return self._used(request, token)
        assistant_drafts.finish_on_site(assistant_drafts.waiting_draft(content.draft))
        return render_site_form(request, content, letter, _site_form_filled(request))

    @staticmethod
    def _used(request: HttpRequest, token: str) -> HttpResponse:
        """A press found the link used or run out. If the browser sent two
        presses at once, the first may have agreed while this one is the
        answer it shows, so the page says to look for the email first."""
        return render_landing(request, dead=True, after_a_press=True)

    def _create_denial(self, request: HttpRequest, form: Any) -> Any:
        from fhi_users.audit import extract_tracking_info

        from fighthealthinsurance.common_view_logic import DenialCreatorHelper
        from fighthealthinsurance.models import Denial
        from fighthealthinsurance.views import subscribe_from_appeal_flow

        data = form.cleaned_data
        if data.get("subscribe"):
            subscribe_from_appeal_flow(request, data["email"])
        referral_source = request.POST.get("referral_source", "")
        referral_source_details = request.POST.get("referral_source_details", "")
        info = DenialCreatorHelper.create_or_update_denial(
            email=data["email"],
            denial_text=data["denial_text"],
            zip=data.get("zip"),
            pii=data["pii"],
            tos=data["tos"],
            privacy=data["privacy"],
            use_external_models=data.get("use_external_models", False),
            store_raw_email=data.get("store_raw_email", False),
            referral_source=referral_source or None,
            referral_source_details=referral_source_details or None,
            tracking_info=extract_tracking_info(request=request, is_professional=False),
            channel=spend.CHANNEL_ASSISTANT,
        )
        return Denial.objects.get(denial_id=info.denial_id)

    def _agreed(
        self,
        request: HttpRequest,
        token: str,
        form: Any,
        content: HandoffContent,
        draft: Any,
        denial: Any,
        reservation: spend.Reservation,
        taken: assistant_ip_limit.Taken,
    ) -> HttpResponse:
        data = form.cleaned_data
        consent.record_consent(
            denial.denial_id,
            {name: data.get(name) for name in consent.BOXES},
            channel=consent.CHANNEL_ASSISTANT,
            on_behalf=data.get("on_behalf") == "helping",
            finish_in=consent.FINISH_IN_CHAT,
            assistant_client=content.client,
        )
        linked = assistant_drafts.agree(
            draft,
            denial,
            content.procedure,
            content.condition,
            reservation_id=reservation.id,
        )

        def give_back() -> None:
            spend.release_generation(reservation)
            assistant_ip_limit.give_back(taken)
            if linked:
                assistant_drafts.set_status(draft, assistant_drafts.STOPPED)

        try:
            continue_token = assistant_continue.mint(denial)
            emailed = assistant_continue.send(data["email"], continue_token)
        except Exception:
            give_back()
            raise
        started = linked and self._start(denial)
        if not started:
            give_back()
        request.session[AGREED_KEY] = {
            "code": _code_digest(token),
            "started": bool(started),
            "emailed": bool(emailed),
        }
        return render_agreed(request, started=bool(started), emailed=bool(emailed))

    @staticmethod
    def _start(denial: Any) -> bool:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from fighthealthinsurance.temporal_client import (
            start_assistant_appeal_workflow,
        )

        try:
            async_to_sync(start_assistant_appeal_workflow)(
                denial.hashed_email, str(denial.uuid)
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception as e:
            logger.warning(
                f"assistant terms: workflow did not start for denial "
                f"{denial.denial_id}: {type(e).__name__}"
            )
            return False
        return True


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters("token", "email"), name="dispatch")
class AssistantContinueView(View):
    """The emailed link: the email address, then the saved letters."""

    http_method_names = ["get", "post"]

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponseBase:
        request.exception_reporter_filter = _NoLocalVariables()  # type: ignore[attr-defined]
        if not assistant_drafts.draft_in_chat_enabled():
            # Our own dead page, so the token is cleared before anything runs.
            return self._dead(request)
        return super().dispatch(request, *args, **kwargs)

    def _page(
        self,
        request: HttpRequest,
        link_works: bool,
        form: Any = None,
        token: str = "",
        wrong_email: bool = False,
    ) -> HttpResponse:
        response = render(
            request,
            CONTINUE_TEMPLATE,
            {
                "no_third_party_scripts": True,
                "link_works": link_works,
                "form": form,
                "token": token,
                "wrong_email": wrong_email,
                "tries": assistant_continue.MAX_WRONG_EMAILS,
                "link_days": assistant_continue.link_days(),
            },
            status=200 if link_works else 404,
        )
        return _private(response)

    def _dead(self, request: HttpRequest) -> HttpResponse:
        return self._page(request, link_works=False)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._page(request, True, core_forms.IntakeResumeForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        from fighthealthinsurance.views import (
            DENIAL_REF_QUERY_PARAM,
            issue_denial_ref_token,
            remember_denial_ref_email,
        )

        token = request.POST.get("token", "")
        form = core_forms.IntakeResumeForm(request.POST)
        if not form.is_valid():
            if assistant_continue.live_link(token) is None:
                return self._dead(request)
            return self._page(request, True, form, token)
        email = form.cleaned_data["email"]
        outcome, denial = assistant_continue.open_case(token, email)
        if outcome == assistant_continue.WRONG_EMAIL:
            return self._page(
                request, True, core_forms.IntakeResumeForm(), token, wrong_email=True
            )
        if outcome != assistant_continue.OPENED or denial is None:
            return self._dead(request)
        # Bound the way intake_resume_views binds a reopened case.
        request.session.cycle_key()
        request.session["denial_uuid"] = str(denial.uuid)
        request.session["denial_id"] = int(denial.denial_id)
        remember_denial_ref_email(request.session, denial.denial_id, email)
        ref = issue_denial_ref_token(
            request, denial.denial_id, email, denial.semi_sekret
        )
        logger.info(f"assistant continue: denial {denial.denial_id} reopened")
        target = reverse("generate_appeal")
        if ref:
            target = f"{target}?{urlencode({DENIAL_REF_QUERY_PARAM: ref})}"
        return _private(redirect(target))

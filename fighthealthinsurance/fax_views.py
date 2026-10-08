import json
import secrets
from typing import Optional
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.decorators import method_decorator
from django.views import View, generic
from django.views.decorators.cache import never_cache

import stripe
from loguru import logger

from fighthealthinsurance import common_view_logic, forms as core_forms
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.helpers.fax_helpers import (
    FAX_RESEND_WINDOW,
    RESEND_DELIVERED,
    SendFaxHelper,
)
from fighthealthinsurance.models import *
from fighthealthinsurance.stripe_utils import get_or_create_price
from fighthealthinsurance.views import (
    DENIAL_REF_QUERY_PARAM,
    add_pubmed_article_fields,
    build_back_url,
    fax_cancel_ref_choices,
    issue_fax_cancel_ref,
    resolve_fax_cancel_ref,
)

# What the fax links keep in the session for the pages they redirect to,
# whose addresses carry no ids (FaxFollowUpLinkView, SendFaxView).
# The follow-up links opened, as [ref, fax pk] pairs, the newest last.
FAX_FOLLOWUP_SESSION_KEY = "fax_followup"
FAX_SENT_SESSION_KEY = "fax_sent"  # what SendFaxHelper.remote_send_fax returned
# How many follow-up links the session keeps, so each page's form still
# re-sends its own fax when one browser opens several.
FAX_FOLLOWUPS_KEPT = 10


def _fax_link_headers(response: HttpResponse) -> HttpResponse:
    """For a response at an address that carries a fax's (uuid, hashed_email)
    pair, the redirect a known pair gets or the 404: never cached, and no
    referrer."""
    response["Referrer-Policy"] = "no-referrer"
    add_never_cache_headers(response)
    return response


def fax_link_not_found(request) -> HttpResponse:
    """The site's 404 page, without the analytics tags base.html would add.

    For a pair that matches no fax, and (error_views.page_not_found) for an
    address under the fax links' paths that matches no route. It is the one
    page that renders at an address holding a pair, and that pair may be a
    real one changed a little, such as by a ")" a mail client added.
    """
    response = render(request, "404.html", {"no_third_party_scripts": True}, status=404)
    return _fax_link_headers(response)


def _known_fax(kwargs) -> Optional[FaxesToSend]:
    """The fax a link's (uuid, hashed_email) pair names, or None."""
    return FaxesToSend.objects.filter(
        uuid=str(kwargs["uuid"]), hashed_email=kwargs["hashed_email"]
    ).first()


def _followups_opened(session) -> list:
    """The follow-up links this session opened, as [ref, fax pk] pairs, the
    newest last."""
    opened = session.get(FAX_FOLLOWUP_SESSION_KEY)
    return list(opened) if isinstance(opened, list) else []


@method_decorator(never_cache, name="dispatch")
class FaxFollowUpLinkView(View):
    """The address in the fax follow-up email, which carries the fax's (uuid,
    hashed_email) pair.

    It keeps the fax in the session and redirects to the follow-up page
    (FaxFollowUpView), whose address carries nothing. So the pair is never
    the address of a page that renders, where the analytics tags would
    report it, and is not left in the back and forward history. An unknown
    pair is a 404.
    """

    def get(self, request, **kwargs):
        fax = _known_fax(kwargs)
        if fax is None:
            return fax_link_not_found(request)
        # Opening the link lets this browser re-send the fax, so the session
        # gets a fresh key, as when intake_resume_views opens a case: a
        # session id set in the browser beforehand never gains the fax.
        request.session.cycle_key()
        # The fax gets a random ref, which the page's form posts back, so a
        # form re-sends the fax it was shown for even after this browser
        # opens another link. Kept even when the fax went through or is past
        # its window, so the page says so, rather than showing a fax another
        # link opened earlier.
        opened = _followups_opened(request.session)
        opened.append([secrets.token_urlsafe(16), fax.pk])
        request.session[FAX_FOLLOWUP_SESSION_KEY] = opened[-FAX_FOLLOWUPS_KEPT:]
        return _fax_link_headers(redirect("fax-followup-page"))

    # A re-send form rendered before the follow-up page moved posts here. It
    # gets the same redirect, to the page with the form for its fax.
    post = get


@method_decorator(never_cache, name="dispatch")
class FaxFollowUpView(generic.FormView):
    """The fax follow-up page: send the fax again, to the same number or a
    corrected one.

    The page shows the form for the fax of the link this session opened
    last, with that link's ref in the form's fax_ref field. The page says
    which fax that is, by the day it was sent and the number on file, and
    the number fills the fax number box. A POST re-sends
    the fax its ref names in this session, and a ref the session doesn't
    hold sends nothing. With no fax, the page says how to open it. A fax
    that went through, or one staged more than FAX_RESEND_WINDOW ago, gets
    a page that says so in place of the form, and a POST for it sends
    nothing.
    """

    template_name = "faxfollowup.html"
    form_class = core_forms.FaxResendForm
    fax: FaxesToSend
    fax_ref: str

    def dispatch(self, request, *args, **kwargs):
        opened = _followups_opened(request.session)
        if request.method == "POST":
            ref = request.POST.get("fax_ref")
        else:
            ref = opened[-1][0] if opened else None
        fax_id = dict(opened).get(ref) if ref else None
        fax = (
            FaxesToSend.objects.filter(pk=fax_id).first()
            if isinstance(fax_id, int)
            else None
        )
        if fax is None:
            return self.in_place_of_the_form({"missing": True}, status=404)
        self.fax = fax
        self.fax_ref = ref
        refusal = SendFaxHelper.resend_refusal(fax)
        if refusal is not None:
            return self.refused(refusal)
        return super().dispatch(request, *args, **kwargs)

    def in_place_of_the_form(self, context, status: int) -> HttpResponse:
        return render(
            self.request,
            self.template_name,
            {"resend_days": FAX_RESEND_WINDOW.days, **context},
            status=status,
        )

    def refused(self, refusal: str) -> HttpResponse:
        delivered = refusal == RESEND_DELIVERED
        return self.in_place_of_the_form(
            {"refused": refusal, "delivered": delivered},
            # A link past its window is a dead link, a 404 as in
            # intake_resume_views; a delivered fax is a plain answer.
            status=200 if delivered else 404,
        )

    def get_initial(self):
        # The number on file fills the fax number box, so a form for some
        # other fax than the one the person has in mind shows a number they
        # don't expect.
        return {"fax_ref": self.fax_ref, "fax_phone": self.fax.destination}

    def get_context_data(self, **kwargs):
        # Which fax the form is for, in the person's own details: the day
        # they sent it and the number on file. Nothing from the letter.
        return super().get_context_data(
            fax_date=self.fax.date, fax_number=self.fax.destination, **kwargs
        )

    def form_valid(self, form):
        fax_phone = form.cleaned_data["fax_phone"]
        sent = SendFaxHelper.resend(
            fax_phone=fax_phone,
            uuid=str(self.fax.uuid),
            hashed_email=self.fax.hashed_email,
        )
        if not sent:
            # Delivered (or past the window) since dispatch looked.
            self.fax.refresh_from_db()
            return self.refused(
                SendFaxHelper.resend_refusal(self.fax) or RESEND_DELIVERED
            )
        return render(
            self.request,
            "fax_followup_thankyou.html",
            {"fax_date": self.fax.date, "fax_number": fax_phone},
        )


@method_decorator(never_cache, name="dispatch")
class SendFaxView(View):
    """Stripe's success address for a fax payment (StageFaxView's
    success_url), which carries the fax's (uuid, hashed_email) pair.

    It sends the fax as before (SendFaxHelper.remote_send_fax), keeps what
    happened in the session and redirects to the sent page (FaxSentView). A
    reload or the back button then shows that page without sending again,
    and the pair is never the address of a page that renders. An unknown
    pair is a 404. A link to a fax staged more than FAX_RESEND_WINDOW ago
    sends nothing.
    """

    def get(self, request, **kwargs):
        fax = _known_fax(kwargs)
        if fax is None:
            return fax_link_not_found(request)
        if SendFaxHelper.link_expired(fax):
            request.session.pop(FAX_SENT_SESSION_KEY, None)
        else:
            request.session[FAX_SENT_SESSION_KEY] = SendFaxHelper.remote_send_fax(
                **self.kwargs
            )
        return _fax_link_headers(redirect("fax-sent"))


@method_decorator(never_cache, name="dispatch")
class FaxSentView(View):
    """The page SendFaxView redirects to: thanks, or that the fax was
    already delivered. With nothing from SendFaxView in the session, it
    says where to ask about the fax."""

    def get(self, request):
        result = request.session.get(FAX_SENT_SESSION_KEY)
        if result not in ("already_sent", "dispatched"):
            return render(request, "fax_thankyou.html", {"missing": True}, status=404)
        return render(
            request, "fax_thankyou.html", {"already_sent": result == "already_sent"}
        )


@method_decorator(never_cache, name="dispatch")
class FaxPaymentCancelledView(View):
    """Where Stripe sends someone who cancels paying for a fax.

    Cancel used to land on the home page, with the letter the person had
    just edited gone from the screen. This gives it back, editable, from the
    staged fax the server already holds, with the fax form, print and mail.

    Pressing Fax My Appeal staged the fax and started sending it before the
    checkout page opened (SendFaxHelper.stage_appeal_as_fax), so cancelling
    the payment does not stop the fax: paying is optional. The page says so,
    and offers the fax form for a corrected copy rather than as a retry.

    The address carries only a reference that opens in the browser that
    staged the fax (views.issue_fax_cancel_ref). Anything else, a stale or
    tampered reference or one from another browser, gets a page with no
    letter on it. Never cached: the page holds the letter and the case's
    secret.
    """

    def get(self, request):
        token = request.GET.get(DENIAL_REF_QUERY_PARAM)
        ref = resolve_fax_cancel_ref(request, token)
        fax = None
        if ref is not None:
            fax_uuid, hashed_email = ref
            fax = (
                FaxesToSend.objects.filter(uuid=fax_uuid, hashed_email=hashed_email)
                .select_related("denial_id")
                .first()
            )
        denial = fax.denial_id if fax is not None else None
        if fax is None or denial is None:
            return render(request, "fax_payment_cancelled.html", status=404)
        choices = fax_cancel_ref_choices(request, token)

        if fax.sent and fax.fax_success:
            fax_status = "sent"
        elif fax.sent:
            fax_status = "failed"
        else:
            fax_status = "sending"

        fax_form = core_forms.FaxForm(
            initial={
                "denial_id": denial.denial_id,
                "email": fax.email,
                "semi_sekret": denial.semi_sekret,
                "fax_phone": fax.destination or denial.appeal_fax_number,
                # The insurer as the person typed it on the fax form, and
                # whether the health history went, from the reference: the
                # staged fax keeps neither. Their name is not kept anywhere,
                # so they type it again.
                "insurance_company": choices.get("insurer") or denial.insurance_company,
                "include_provided_health_history": choices.get(
                    "include_history", False
                ),
            }
        )
        # Only the articles the person left ticked, so a corrected copy
        # carries what the first one did.
        chosen_pmids = (
            {str(pmid) for pmid in fax.pmids} if isinstance(fax.pmids, list) else None
        )
        add_pubmed_article_fields(
            fax_form,
            common_view_logic.ChooseAppealHelper.candidate_articles(
                denial.denial_id, denial
            ),
            chosen_pmids,
        )
        return render(
            request,
            "appeal.html",
            context={
                # The draft box holds the letter that was sent, and the
                # finished-letter box starts empty, so appeal.ts builds it
                # from the draft and keeps it in step with later edits. A
                # filled finished box reads to the script as the person's own
                # edit, after which edits to the draft never reach the fax.
                "appeal": fax.appeal_text,
                "user_email": fax.email,
                "denial_id": denial.denial_id,
                "semi_sekret": denial.semi_sekret,
                "fax_form": fax_form,
                "fax_payment_cancelled": True,
                "fax_status": fax_status,
                "current_step": 8,
                "back_url": build_back_url(
                    request,
                    "generate_appeal",
                    denial.denial_id,
                    fax.email,
                    denial.semi_sekret,
                ),
                "back_label": "Back to appeals",
                "fhi_always_restore": True,
            },
        )


class StageFaxView(generic.FormView):
    form_class = core_forms.FaxForm
    template_name = "appeal.html"

    def get_context_data(self, **kwargs):
        try:
            ctx = super().get_context_data(**kwargs)
            # Get the form object because it's a form view.
            form = ctx.get("form")

            # Template expects `fax_form`, not `form`
            ctx["fax_form"] = form
            if self.request.method == "POST" and ctx:
                ctx.setdefault(
                    "appeal", self.request.POST.get("completed_appeal_text", "")
                )
                ctx.setdefault("denial_id", self.request.POST.get("denial_id"))
                ctx.setdefault("user_email", self.request.POST.get("email"))
            return ctx
        except Exception as e:
            logger.exception(f"Failed to build template context for StageFaxView: {e}")
            # Return safe empty context to prevent template errors
            return {"fax_form": self.get_form()}

    def form_valid(self, form):
        logger.debug("Valid fax form received")
        form_data = form.cleaned_data
        # Get all of the articles the user wants to send
        pubmed_checkboxes = [
            key[len("pubmed_") :]
            for key, value in self.request.POST.items()
            if key.startswith("pubmed_") and value == "on"
        ]
        form_data["pubmed_ids_parsed"] = pubmed_checkboxes
        logger.debug(f"Pubmed IDs: {pubmed_checkboxes}")
        # Make sure the denial secret is present
        denial = Denial.objects.filter(semi_sekret=form_data["semi_sekret"]).get(
            denial_id=form_data["denial_id"]
        )
        form_data["company_name"] = (
            "Fight Health Insurance -- a service of Totally Legit Co"
        )
        form_data["include_cover"] = True
        denial.appeal_fax_number = form_data["fax_phone"] or self.request.POST.get(
            "fax_phone"
        )
        denial.save()
        appeal = common_view_logic.AppealAssemblyHelper().create_or_update_appeal(
            **form_data
        )
        staged = SendFaxHelper.stage_appeal_as_fax(
            appeal=appeal, email=form_data["email"], fax_number=form_data["fax_phone"]
        )
        if form.placeholders_sent_as_they_are:
            # How many, and nothing else: never the letter or the blanks.
            logger.info(
                "Fax staged with placeholders the sender confirmed: "
                f"{form.placeholders_sent_as_they_are}"
            )
        stripe.api_key = settings.STRIPE_API_SECRET_KEY

        # Get fax amount from form (PWYW) with validation
        # Check both fax_amount (set by JS) and fax_amount_custom (fallback if JS disabled)
        # ----- PWYW handling -----
        fax_pwyw_selection = self.request.POST.get("fax_pwyw", "0")
        fax_amount_hidden = self.request.POST.get("fax_amount")
        fax_amount_custom = self.request.POST.get("fax_amount_custom")

        fax_amount_raw = fax_amount_hidden
        if not fax_amount_raw:
            if fax_pwyw_selection == "custom":
                fax_amount_raw = fax_amount_custom or "0"
                logger.info(
                    "Using fax_amount_custom fallback (JavaScript may be disabled)"
                )
            else:
                fax_amount_raw = fax_pwyw_selection or "0"

        try:
            fax_amount = int(fax_amount_raw)
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid fax_amount received: {fax_amount_raw} (not a valid integer)"
            )
            form.add_error(
                None,
                "Invalid fax amount. Please enter a number between 0 and 1000.",
            )
            return self.form_invalid(form)

        if not (0 <= fax_amount <= 1000):
            logger.warning(
                f"Invalid fax_amount received: {fax_amount} (out of valid range 0-1000)"
            )
            form.add_error(
                None,
                "Fax amount must be between 0 and 1000.",
            )
            return self.form_invalid(form)

        if fax_amount == 0:
            # Free fax - send immediately
            logger.debug(f"Fax amount is zero, sending directly.")
            SendFaxHelper.remote_send_fax(
                uuid=staged.uuid,
                hashed_email=staged.hashed_email,
            )
            return render(self.request, "fax_thankyou.html")

        # Check if the product already exists
        product_id, price_id = get_or_create_price(
            product_name=f"Appeal Fax - ${fax_amount}",
            amount=fax_amount * 100,  # Convert to cents
            currency="usd",
            recurring=False,
        )
        items = [
            {
                "price": price_id,
                "quantity": 1,
            }
        ]
        stripe_recovery_info = StripeRecoveryInfo.objects.create(items=items)
        metadata: dict[str, str] = {
            "payment_type": "fax",
            "fax_request_uuid": str(staged.uuid),
            "recovery_info_id": str(stripe_recovery_info.id),
        }
        # Capture client IP/ASN into the session metadata so an expired
        # checkout can be tied back to the originating client (the expiry
        # webhook comes from Stripe, not the user).
        from fhi_users.audit import tracking_metadata_for_request

        metadata.update(tracking_metadata_for_request(self.request))
        # Cancelling brings the person back to their letter, not the home
        # page. The address names the staged fax only through a reference
        # this browser's session can open.
        cancel_path = reverse("fax_payment_cancelled")
        cancel_ref = issue_fax_cancel_ref(
            self.request,
            staged.uuid,
            staged.hashed_email,
            include_history=bool(form_data.get("include_provided_health_history")),
            insurer=form_data.get("insurance_company"),
        )
        if cancel_ref:
            cancel_path += "?" + urlencode({DENIAL_REF_QUERY_PARAM: cancel_ref})
        checkout = stripe.checkout.Session.create(
            line_items=items,  # type: ignore
            mode="payment",  # No subscriptions
            success_url=self.request.build_absolute_uri(
                reverse(
                    "sendfaxview",
                    kwargs={
                        "uuid": staged.uuid,
                        "hashed_email": staged.hashed_email,
                    },
                ),
            ),
            cancel_url=self.request.build_absolute_uri(cancel_path),
            customer_email=form.cleaned_data["email"],
            metadata=metadata,
        )
        checkout_url = checkout.url
        if checkout_url is None:
            raise Exception("Could not create checkout url")
        else:
            return redirect(checkout_url)

import json
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


def _fax_link_headers(
    response: HttpResponse, form_on_page: bool = False
) -> HttpResponse:
    """Headers for a page whose address is a fax's (uuid, hashed_email) pair.

    The views that use this are never_cache and render with
    no_third_party_scripts, so base.html loads no analytics tags. Pages
    without the re-send form send no referrer at all. The page with the form
    sends strict-origin. A page a link on it opens may load the analytics
    tags, and gets only the site's origin as its referrer, not the pair in
    this path; same-origin would give it the whole address. The form still
    posts with a real Origin, which Django's CSRF check needs: under
    no-referrer a browser posts it with "Origin: null", which the check
    refuses (as in assistant_handoff_views._private).
    """
    response["Referrer-Policy"] = "strict-origin" if form_on_page else "no-referrer"
    return response


def fax_link_not_found(request) -> HttpResponse:
    """The site's 404 page, without the analytics tags base.html would add.

    For a pair that matches no fax, and (error_views.page_not_found) for an
    address under the fax pages' paths that matches no route.
    """
    response = render(request, "404.html", {"no_third_party_scripts": True}, status=404)
    add_never_cache_headers(response)
    return _fax_link_headers(response)


@method_decorator(never_cache, name="dispatch")
class FaxFollowUpView(generic.FormView):
    """The page the fax follow-up email links to: send the fax again, to the
    same number or a corrected one.

    The fax is the one the address names; an unknown pair is a 404. A fax
    that went through, or one staged more than FAX_RESEND_WINDOW ago, gets a
    page that says so in place of the form, and a POST for it sends nothing.
    """

    template_name = "faxfollowup.html"
    form_class = core_forms.FaxResendForm
    fax: FaxesToSend

    def dispatch(self, request, *args, **kwargs):
        fax = FaxesToSend.objects.filter(
            uuid=str(kwargs["uuid"]), hashed_email=kwargs["hashed_email"]
        ).first()
        if fax is None:
            return fax_link_not_found(request)
        self.fax = fax
        refusal = SendFaxHelper.resend_refusal(fax)
        if refusal is not None:
            return self.refused(refusal)
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        # Set the initial arguments to the form based on the URL route params.
        return self.kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["no_third_party_scripts"] = True
        return context

    def render_to_response(self, context, **response_kwargs):
        # The form, on a GET or after a POST that didn't validate.
        return _fax_link_headers(
            super().render_to_response(context, **response_kwargs),
            form_on_page=True,
        )

    def refused(self, refusal: str) -> HttpResponse:
        """What the person sees in place of the form."""
        return _fax_link_headers(
            render(
                self.request,
                "faxfollowup.html",
                {
                    "no_third_party_scripts": True,
                    "refused": refusal,
                    "delivered": refusal == RESEND_DELIVERED,
                    "resend_days": FAX_RESEND_WINDOW.days,
                },
                # A link past its window is a dead link, a 404 as in
                # intake_resume_views; a delivered fax is a plain answer.
                status=200 if refusal == RESEND_DELIVERED else 404,
            )
        )

    def form_valid(self, form):
        # The fax the address names, not the form's hidden copies of its ids.
        sent = SendFaxHelper.resend(
            fax_phone=form.cleaned_data["fax_phone"],
            uuid=str(self.fax.uuid),
            hashed_email=self.fax.hashed_email,
        )
        if not sent:
            # Delivered (or past the window) since dispatch looked.
            self.fax.refresh_from_db()
            return self.refused(
                SendFaxHelper.resend_refusal(self.fax) or RESEND_DELIVERED
            )
        return _fax_link_headers(
            render(
                self.request,
                "fax_followup_thankyou.html",
                {"no_third_party_scripts": True},
            )
        )


@method_decorator(never_cache, name="dispatch")
class SendFaxView(View):
    """Stripe's success page for a fax payment (StageFaxView's success_url).

    An unknown pair is a 404. The page loads no analytics tags, sends no
    referrer and is never cached: its address is the fax's pair.
    """

    def get(self, request, **kwargs):
        try:
            result = SendFaxHelper.remote_send_fax(**self.kwargs)
        except FaxesToSend.DoesNotExist:
            return fax_link_not_found(request)
        return _fax_link_headers(
            render(
                self.request,
                "fax_thankyou.html",
                {
                    "already_sent": result == "already_sent",
                    "no_third_party_scripts": True,
                },
            )
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

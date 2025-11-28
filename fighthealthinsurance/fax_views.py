import stripe
from loguru import logger
from typing import *
import json

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View, generic

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance import common_view_logic
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.models import *
from fighthealthinsurance.utils import *
from fighthealthinsurance.stripe_utils import get_or_create_price


class FaxFollowUpView(generic.FormView):
    template_name = "faxfollowup.html"
    form_class = core_forms.FaxResendForm

    def get_initial(self):
        # Set the initial arguments to the form based on the URL route params.
        return self.kwargs

    def form_valid(self, form):
        common_view_logic.SendFaxHelper.resend(**form.cleaned_data)
        return render(self.request, "fax_followup_thankyou.html")


class SendFaxView(View):

    def get(self, request, **kwargs):
        common_view_logic.SendFaxHelper.remote_send_fax(**self.kwargs)
        return render(self.request, "fax_thankyou.html")


class StageFaxView(generic.FormView):
    form_class = core_forms.FaxForm
    template_name = "appeal.html"

    def get_context_data(self, **kwargs):
        ctx = None
        try:
            ctx = super().get_context_data(**kwargs)
            # Get the form object because it's a form view.
            form = ctx.get("form")

            # Template expects `fax_form`, not `form`
            ctx["fax_form"] = form
        except Exception:
            pass

        if self.request.method == "POST":
            ctx.setdefault("appeal", self.request.POST.get("completed_appeal_text", ""))
            ctx.setdefault("denial_id", self.request.POST.get("denial_id"))
            ctx.setdefault("user_email", self.request.POST.get("email"))
        return ctx

    def form_valid(self, form):
        logger.debug(f"Huzzah valid form.")
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
        denial.appeal_fax_number = form_data["fax_phone"]
        appeal = common_view_logic.AppealAssemblyHelper().create_or_update_appeal(
            **form_data
        )
        staged = common_view_logic.SendFaxHelper.stage_appeal_as_fax(
            appeal=appeal, email=form_data["email"]
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
            common_view_logic.SendFaxHelper.remote_send_fax(
                uuid=staged.uuid,
                hashed_email=staged.hashed_email,
            )
            return render(self.request, "fax_thankyou.html")

        # Check if the product already exists
        (product_id, price_id) = get_or_create_price(
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
        metadata = {
            "payment_type": "fax",
            "fax_request_uuid": staged.uuid,
            "recovery_info_id": stripe_recovery_info.id,
        }
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
            cancel_url=self.request.build_absolute_uri(reverse("root")),
            customer_email=form.cleaned_data["email"],
            metadata=metadata,
        )
        checkout_url = checkout.url
        if checkout_url is None:
            raise Exception("Could not create checkout url")
        else:
            return redirect(checkout_url)

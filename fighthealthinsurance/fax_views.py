import stripe
from loguru import logger
from typing import *
import json

from django.conf import settings
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

    def form_valid(self, form):
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

        # Get the custom amount from form, default to $0
        fax_amount = form_data.get("fax_amount", 0)
        if fax_amount is None:
            fax_amount = 0
        amount_cents = int(float(fax_amount) * 100)  # Convert to cents

        # Check if the product already exists
        (product_id, price_id) = get_or_create_price(
            product_name="Appeal Fax -- PWYW",
            amount=amount_cents,
            currency="usd",
            recurring=False,
        )
        
        # If amount is $0, skip Stripe and go directly to send fax
        if amount_cents == 0:
            return redirect(
                reverse(
                    "sendfaxview",
                    kwargs={
                        "uuid": staged.uuid,
                        "hashed_email": staged.hashed_email,
                    },
                )
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

import json
from typing import *

from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View, generic
from django.http import HttpResponse

import stripe
from fighthealthinsurance.common_view_logic import *
from fighthealthinsurance.core_forms import *
from fighthealthinsurance.generate_appeal import *
from fighthealthinsurance.models import *
from fighthealthinsurance.question_forms import *
from fighthealthinsurance.utils import *


class FollowUpFaxSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = FollowUpTestForm

    def form_valid(self, form):
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = SendFaxHelper.blocking_dosend_all(count=field)
        except ValueError:
            sent = SendFaxHelper.blocking_dosend_target(email=field)
        return HttpResponse(str(sent))


class FaxFollowUpView(generic.FormView):
    template_name = "faxfollowup.html"
    form_class = FaxResendForm

    def get_initial(self):
        # Set the initial arguments to the form based on the URL route params.
        return self.kwargs

    def form_valid(self, form):
        SendFaxHelper.resend(**form.cleaned_data)
        return render(self.request, "fax_followup_thankyou.html")


class SendFaxView(View):

    def get(self, request, **kwargs):
        SendFaxHelper.remote_send_fax(**self.kwargs)
        return render(self.request, "fax_thankyou.html")


class StageFaxView(generic.FormView):
    form_class = FaxForm

    def form_valid(self, form):
        form_data = form.cleaned_data
        # Get all of the articles the user wants to send
        print(f"Items {list(self.request.POST.items())}")
        pubmed_checkboxes = [
            key[len("pubmed_") :]
            for key, value in self.request.POST.items()
            if key.startswith("pubmed_") and value == "on"
        ]
        form_data["pubmed_ids_parsed"] = pubmed_checkboxes
        print(f"Staging fax with {form_data}")
        staged = SendFaxHelper.stage_appeal_fax(**form_data)
        stripe.api_key = settings.STRIPE_API_SECRET_KEY
        stripe.publishable_key = settings.STRIPE_API_PUBLISHABLE_KEY
        product = stripe.Product.create(name="Fax")
        product_price = stripe.Price.create(
            unit_amount=500, currency="usd", product=product["id"]
        )
        items = [
            {
                "price": product_price["id"],
                "quantity": 1,
            }
        ]
        checkout = stripe.checkout.Session.create(
            line_items=items,
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
        )
        checkout_url = checkout.url
        return redirect(checkout_url)

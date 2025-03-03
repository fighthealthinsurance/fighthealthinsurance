import json
import stripe
from stripe import error as stripe_error
import typing
from loguru import logger
from PIL import Image


from django import forms
from django.conf import settings
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views import View, generic
from django.http import HttpRequest, HttpResponseBase, HttpResponse, FileResponse
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponseRedirect

from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance import models
from fighthealthinsurance import followup_emails
from django.template import loader
from django.http import HttpResponseForbidden


def render_ocr_error(request: HttpRequest, text: str) -> HttpResponseBase:
    return render(
        request,
        "server_side_ocr_error.html",
        context={
            "error": text,
        },
    )


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    template = loader.get_template(template_name)
    logger.error(f"CSRF failure: {reason}")
    return HttpResponseForbidden(template.render({"reason": reason}, request))


class FollowUpView(generic.FormView):
    template_name = "followup.html"
    form_class = core_forms.FollowUpForm

    def get_initial(self):
        # Set the initial arguments to the form based on the URL route params.
        # Also make sure we can resolve the denial
        #
        # NOTE: Potential security issue here
        denial = common_view_logic.FollowUpHelper.fetch_denial(**self.kwargs)
        if denial is None:
            raise Exception(f"Could not find denial for {self.kwargs}")
        return self.kwargs

    def form_valid(self, form):
        common_view_logic.FollowUpHelper.store_follow_up_result(**form.cleaned_data)
        return render(self.request, "followup_thankyou.html")


class ProVersionThankYouView(generic.TemplateView):
    template_name = "professional_thankyou.html"


class BRB(generic.TemplateView):
    template_name = "brb.html"

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        response.status_code = 503  # Set HTTP status to 503
        return response


def safe_redirect(request, url):
    """
    Safely redirect to a URL after validating it's safe.
    
    Args:
        request: The HTTP request
        url: The URL to redirect to
    
    Returns:
        HttpResponseRedirect to a safe URL
    
    Raises:
        SuspiciousOperation if the URL is not safe
    """
    ALLOWED_HOSTS = [
        request.get_host(),  
        'checkout.stripe.com',
    ]

    if not url.startswith('/'):
        # For URLs, we need to validate the domain        
        from urllib.parse import urlparse
        parsed_url = urlparse(url)

        if parsed_url.netloc and parsed_url.netloc not in ALLOWED_HOSTS:
            logger.warning(f"Suspicious redirect attempt to: {url}")
            raise SuspiciousOperation(f"Redirect to untrusted domain: {parsed_url.netloc}")
        
        logger.info(f"Redirecting to: {url}")
        
        return HttpResponseRedirect(url)


class ProVersionView(generic.FormView):
    template_name = "professional.html"
    form_class = core_forms.InterestedProfessionalForm
    sender = followup_emails.ThankyouEmailSender()

    def form_valid(self, form):
        interested_professional = form.save()
        try:
            self.sender.dosend(interested_pro=interested_professional)
        except Exception as e:
            logger.opt(exception=True).error("Failed to send thank you email")

        if not (
            "clicked_for_paid" in form.cleaned_data
            and form.cleaned_data["clicked_for_paid"]
        ):
            return render(self.request, "professional_thankyou.html")

        stripe.api_key = settings.STRIPE_API_SECRET_KEY

        # Check if the product already exists
        products = stripe.Product.list(limit=100)
        product = next(
            (p for p in products.data if p.name == "Pre-Signup -- New"), None
        )

        if product is None:
            product = stripe.Product.create(name="Pre-Signup -- New")

        prices = stripe.Price.list(product=product["id"], limit=100)
        product_price = next(
            (
                p
                for p in prices.data
                if p.unit_amount == 1000
                and p.currency == "usd"
                and p.id == product["id"]
            ),
            None,
        )

        if product_price is None:
            product_price = stripe.Price.create(
                unit_amount=1000, currency="usd", product=product["id"]
            )

        checkout = stripe.checkout.Session.create(
            line_items=[
                {
                    "price": product_price["id"],
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=self.request.build_absolute_uri(
                reverse("pro_version_thankyou")
            ),
            cancel_url=self.request.build_absolute_uri(reverse("pro_version_thankyou")),
            customer_email=form.cleaned_data["email"],
            metadata={
                "payment_type": "interested_professional_signup",
                "interested_professional_id": interested_professional.id,
            },
        )

        checkout_url = checkout.url
        if checkout_url is None:
            raise Exception("Could not create checkout url")
        else:
            return safe_redirect(self.request, checkout_url)


class IndexView(generic.TemplateView):
    template_name = "index.html"


class AboutView(generic.TemplateView):
    template_name = "about_us.html"


class OtherResourcesView(generic.TemplateView):
    template_name = "other_resources.html"


class ScanView(generic.TemplateView):
    template_name = "scrub.html"

    def get_context_data(self, **kwargs):
        return {"ocr_result": "", "upload_more": True}


class PrivacyPolicyView(generic.TemplateView):
    template_name = "privacy_policy.html"

    def get_context_data(self, **kwargs):
        return {"title": "Privacy Policy"}


class TermsOfServiceView(generic.TemplateView):
    template_name = "tos.html"

    def get_context_data(self, **kwargs):
        return {"title": "Terms of Service"}


class ContactView(generic.TemplateView):
    template_name = "contact.html"

    def get_context_data(self, **kwargs):
        return {"title": "Contact Us"}


class ShareDenialView(View):
    def get(self, request):
        return render(request, "share_denial.html", context={"title": "Share Denial"})


class ShareAppealView(View):
    def get(self, request):
        return render(request, "share_appeal.html", context={"title": "Share Appeal"})

    def post(self, request):
        form = core_forms.ShareAppealForm(request.POST)
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            hashed_email = models.Denial.get_hashed_email(form.cleaned_data["email"])

            # Update the denial
            denial = models.Denial.objects.filter(
                denial_id=denial_id,
                # Include the hashed e-mail so folks can't brute force denial_id
                hashed_email=hashed_email,
            ).get()
            logger.debug(form.cleaned_data)
            denial.appeal_text = form.cleaned_data["appeal_text"]
            denial.save()
            pa = models.ProposedAppeal(
                appeal_text=form.cleaned_data["appeal_text"],
                for_denial=denial,
                chosen=True,
                editted=True,
            )
            pa.save()
            return render(request, "thankyou.html")


class RemoveDataView(View):
    def get(self, request):
        return render(
            request,
            "remove_data.html",
            context={
                "title": "Remove My Data",
                "form": core_forms.DeleteDataForm(),
            },
        )

    def post(self, request):
        form = core_forms.DeleteDataForm(request.POST)

        if form.is_valid():
            email = form.cleaned_data["email"]
            common_view_logic.RemoveDataHelper.remove_data_for_email(email)
            return render(
                request,
                "removed_data.html",
                context={
                    "title": "Remove My Data",
                },
            )

        return render(
            request,
            "remove_data.html",
            context={
                "title": "Remove My Data",
                "form": form,
            },
        )


class RecommendAppeal(View):
    def post(self, request):
        return render(request, "")


class FindNextSteps(View):
    def post(self, request):
        form = core_forms.PostInferedForm(request.POST)
        if form.is_valid():
            denial_id = form.cleaned_data["denial_id"]
            email = form.cleaned_data["email"]

            next_step_info = common_view_logic.FindNextStepsHelper.find_next_steps(
                **form.cleaned_data
            )
            denial_ref_form = core_forms.DenialRefForm(
                initial={
                    "denial_id": denial_id,
                    "email": email,
                    "semi_sekret": next_step_info.semi_sekret,
                }
            )
            return render(
                request,
                "outside_help.html",
                context={
                    "outside_help_details": next_step_info.outside_help_details,
                    "combined": next_step_info.combined_form,
                    "denial_form": denial_ref_form,
                },
            )

        # If not valid take the user back.
        return render(
            request,
            "categorize.html",
            context={
                "post_infered_form": form,
                "upload_more": True,
            },
        )


class ChooseAppeal(View):
    def post(self, request):
        form = core_forms.ChooseAppealForm(request.POST)

        if not form.is_valid():
            logger.debug(form)
            return

        (
            appeal_fax_number,
            insurance_company,
            candidate_articles,
        ) = common_view_logic.ChooseAppealHelper.choose_appeal(**form.cleaned_data)

        appeal_info_extracted = ""
        fax_form = core_forms.FaxForm(
            initial={
                "denial_id": form.cleaned_data["denial_id"],
                "email": form.cleaned_data["email"],
                "semi_sekret": form.cleaned_data["semi_sekret"],
                "fax_phone": appeal_fax_number,
                "insurance_company": insurance_company,
            }
        )
        # Add the possible articles for inclusion
        if candidate_articles is not None:
            for article in candidate_articles[0:6]:
                article_id = article.pmid
                title = article.title
                link = f"http://www.ncbi.nlm.nih.gov/pubmed/{article_id}"
                label = mark_safe(
                    f"Include Summary* of PubMed Article "
                    f"<a href='{link}'>{title} -- {article_id}</a>"
                )
                fax_form.fields["pubmed_" + article_id] = forms.BooleanField(
                    label=label,
                    required=False,
                    initial=True,
                )

        return render(
            request,
            "appeal.html",
            context={
                "appeal": form.cleaned_data["appeal_text"],
                "user_email": form.cleaned_data["email"],
                "denial_id": form.cleaned_data["denial_id"],
                "appeal_info_extract": appeal_info_extracted,
                "fax_form": fax_form,
            },
        )


class GenerateAppeal(View):
    def post(self, request):
        form = core_forms.DenialRefForm(request.POST)
        if not form.is_valid():
            # TODO: Send user back to fix the form.
            return

        # We copy _most_ of the input over for the form context
        elems = dict(request.POST)
        # Query dict is of lists
        elems = dict((k, v[0]) for k, v in elems.items())
        del elems["csrfmiddlewaretoken"]
        return render(
            request,
            "appeals.html",
            context={
                "form_context": json.dumps(elems),
                "user_email": form.cleaned_data["email"],
                "denial_id": form.cleaned_data["denial_id"],
                "semi_sekret": form.cleaned_data["semi_sekret"],
            },
        )


class OCRView(View):
    def __init__(self):
        # Load easy ocr reader if possible
        try:
            import easyocr

            self._easy_ocr_reader = easyocr.Reader(["en"], gpu=False)
        except Exception:
            pass

    def get(self, request):
        return render(request, "server_side_ocr.html")

    def post(self, request):
        try:
            logger.debug(request.FILES)
            files = dict(request.FILES.lists())
            uploader = files["uploader"]
            doc_txt = self._ocr(uploader)
            return render(
                request,
                "scrub.html",
                context={
                    "ocr_result": doc_txt,
                    "upload_more": False,
                },
            )

        except AttributeError:
            render_ocr_error(request, "Unsupported file")

        except KeyError:
            return render_ocr_error(request, "Missing file")

    def _ocr(self, uploader):
        def ocr_upload(x):
            try:
                import pytesseract

                img = Image.open(x)
                return pytesseract.image_to_string(img)
            except Exception:
                result = self._easy_ocr_reader.readtext(x.read())
                return " ".join([text for _, text, _ in result])

        texts = map(ocr_upload, uploader)
        return "\n".join(texts)


class InitialProcessView(generic.FormView):
    template_name = "scrub.html"
    form_class = core_forms.DenialForm

    def get_ocr_result(self) -> typing.Optional[str]:
        if self.request.method == "POST":
            return self.request.POST.get("denial_text", None)
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ocr_result"] = self.get_ocr_result() or ""
        context["upload_more"] = True
        return context

    def get_success_url(self):
        pass

    def form_valid(self, form):
        denial_response = common_view_logic.DenialCreatorHelper.create_or_update_denial(
            **form.cleaned_data,
        )

        # Store the denial ID in the session to maintain state across the multi-step form process
        # This allows the SessionRequiredMixin to verify the user is working with a valid denial
        self.request.session["denial_uuid"] = denial_response.denial_id

        form = core_forms.HealthHistory(
            initial={
                "denial_id": denial_response.denial_id,
                "email": form.cleaned_data["email"],
                "semi_sekret": denial_response.semi_sekret,
            }
        )

        return render(
            self.request,
            "health_history.html",
            context={
                "form": form,
                "next": reverse("hh"),
            },
        )


class SessionRequiredMixin(View):
    """Verify that the current user has an active session."""

    def dispatch(self, request, *args, **kwargs):
        if not request.session.get("denial_uuid"):
            denial_id = request.POST.get("denial_id") or request.GET.get("denial_id")
            if denial_id:
                request.session["denial_uuid"] = denial_id
            else:
                return redirect("process")
        return super().dispatch(request, *args, **kwargs)


class EntityExtractView(SessionRequiredMixin, View):
    form_class = core_forms.EntityExtractForm
    template_name = "entity_extract.html"

    def form_valid(self, form):
        denial_response = common_view_logic.DenialCreatorHelper.update_denial(
            **form.cleaned_data,
        )

        form = core_forms.PostInferedForm(
            initial={
                "denial_type": denial_response.selected_denial_type,
                "denial_id": denial_response.denial_id,
                "email": form.cleaned_data["email"],
                "your_state": denial_response.your_state,
                "procedure": denial_response.procedure,
                "diagnosis": denial_response.diagnosis,
                "semi_sekret": denial_response.semi_sekret,
            }
        )

        return render(
            self.request,
            "categorize.html",
            context={
                "post_infered_form": form,
                "upload_more": True,
            },
        )


class PlanDocumentsView(SessionRequiredMixin, View):
    form_class = core_forms.HealthHistory
    template_name = "health_history.html"

    def form_valid(self, form):
        denial_response = common_view_logic.DenialCreatorHelper.update_denial(
            **form.cleaned_data,
        )

        form = core_forms.PlanDocumentsForm(
            initial={
                "denial_id": denial_response.denial_id,
                "email": form.cleaned_data["email"],
                "semi_sekret": denial_response.semi_sekret,
            }
        )

        return render(
            self.request,
            "plan_documents.html",
            context={
                "form": form,
                "next": reverse("dvc"),
            },
        )


class DenialCollectedView(SessionRequiredMixin, View):
    form_class = core_forms.PlanDocumentsForm
    template_name = "plan_documents.html"

    def form_valid(self, form):
        # TODO: Make use of the response from this
        common_view_logic.DenialCreatorHelper.update_denial(**form.cleaned_data)

        new_form = core_forms.EntityExtractForm(
            initial={
                "denial_id": form.cleaned_data["denial_id"],
                "email": form.cleaned_data["email"],
                "semi_sekret": form.cleaned_data["semi_sekret"],
            }
        )

        return render(
            self.request,
            "entity_extract.html",
            context={
                "form": new_form,
                "next": reverse("eev"),
                "form_context": {
                    "denial_id": form.cleaned_data["denial_id"],
                    "email": form.cleaned_data["email"],
                    "semi_sekret": form.cleaned_data["semi_sekret"],
                },
            },
        )


class AppealFileView(View):
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponse(status=401)
        appeal_uuid = kwargs.get("appeal_uuid", None)
        if appeal_uuid is None:
            return HttpResponse(status=400)
        current_user = request.user  # type: ignore
        appeal = get_object_or_404(
            models.Appeal.filter_to_allowed_appeals(current_user), uuid=appeal_uuid
        )
        file = appeal.document_enc.open()
        content = Cryptographer.decrypted(file.read())
        return HttpResponse(content, content_type="application/pdf")


class StripeWebhookView(View):
    def post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            return HttpResponse(status=400)
        except stripe_error.SignatureVerificationError as e:  # type: ignore
            logger.error(f"Invalid signature: {e}")
            return HttpResponse(status=400)

        if event.type == "checkout.session.completed":
            session = event.data.object
            payment_type = session.metadata.get("payment_type")

            if payment_type == "interested_professional_signup":
                models.InterestedProfessional.objects.filter(
                    id=session.metadata.get("interested_professional_id")
                ).update(paid=True)

            elif payment_type == "professional_domain_subscription":
                subscription_id = session.get("subscription")
                if subscription_id:
                    models.UserDomain.objects.filter(
                        id=session.metadata.get("id")
                    ).update(
                        stripe_subscription_id=subscription_id,
                        active=True,
                        pending=False,
                    )
                    models.ProfessionalUser.objects.filter(
                        id=session.metadata.get("professional_id")
                    ).update(active=True)
                else:
                    logger.error("No subscription ID in completed checkout session")

            elif payment_type == "fax":
                models.FaxesToSend.objects.filter(
                    uuid=session.metadata.get("uuid")
                ).update(paid=True, should_send=True)
            else:
                logger.error(f"Unknown payment type: {payment_type}")

        return HttpResponse(status=200)

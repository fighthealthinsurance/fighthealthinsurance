import json
import stripe
import typing
from loguru import logger

from PIL import Image
from django import forms
from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views import View, generic

from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance import generate_appeal
from fighthealthinsurance import models
from fighthealthinsurance.forms import questions as question_forms
from fighthealthinsurance import utils

appealGenerator = generate_appeal.AppealGenerator()


def render_ocr_error(request, text):
    return render(
        request,
        "server_side_ocr_error.html",
        context={
            "error": text,
        },
    )


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


class ProVersionView(generic.FormView):
    template_name = "professional.html"
    form_class = core_forms.InterestedProfessionalForm

    def form_valid(self, form):
        form.save()

        if not (
            "clicked_for_paid" in form.cleaned_data
            and form.cleaned_data["clicked_for_paid"]
        ):
            return render(self.request, "professional_thankyou.html")

        stripe.api_key = settings.STRIPE_API_SECRET_KEY
        stripe.publishable_key = settings.STRIPE_API_PUBLISHABLE_KEY

        product = stripe.Product.create(name="Pre-Signup")
        product_price = stripe.Price.create(
            unit_amount=1000, currency="usd", product=product["id"]
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
                reverse("pro_version_thankyou")
            ),
            cancel_url=self.request.build_absolute_uri(reverse("pro_version_thankyou")),
            customer_email=form.cleaned_data["email"],
        )
        checkout_url = checkout.url
        return redirect(checkout_url)


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


class StreamingEntityBackend(View):
    """Streaming Entity Extraction"""

    async def post(self, request):
        logger.debug(request)
        logger.debug(request.POST)
        form = core_forms.DenialRefForm(request.POST)
        if form.is_valid():
            return await common_view_logic.DenialCreatorHelper.extract_entity(
                form.cleaned_data["denial_id"]
            )
        else:
            logger.debug(f"Error processing {form}")


class AppealsBackend(View):
    """Streaming back the appeals as json :D"""

    async def post(self, request):
        logger.debug(request)
        logger.debug(request.POST)
        form = core_forms.DenialRefForm(request.POST)
        if not form.is_valid():
            logger.debug(f"Error processing {form}")
            return

        return await common_view_logic.AppealsBackendHelper.generate_appeals(
            request.POST
        )

    async def get(self, request):
        form = core_forms.DenialRefForm(request.GET)

        if not form.is_valid():
            logger.debug(f"Error processing {form}")
            return

        return await common_view_logic.AppealsBackendHelper.generate_appeals(
            request.GET,
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
        denial_response = common_view_logic.DenialCreatorHelper.create_denial(
            **form.cleaned_data,
        )

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


class EntityExtractView(generic.FormView):
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


class PlanDocumentsView(generic.FormView):
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


class DenialCollectedView(generic.FormView):
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

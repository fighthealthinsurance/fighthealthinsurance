import os
import re

import json
import stripe
from stripe import error as stripe_error
import typing
from typing import TypedDict
from loguru import logger
from PIL import Image

import asyncio

from django import forms
from django.conf import settings
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views import View, generic
from django.http import HttpRequest, HttpResponseBase, HttpResponse
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic.edit import FormView
from django.views.generic.base import TemplateView

from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance import common_view_logic
from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.chat_forms import UserConsentForm
from fighthealthinsurance import models

from fighthealthinsurance.models import StripeRecoveryInfo

from django.template import loader
from django.http import HttpResponseForbidden

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage


class BlogPostMetadata(TypedDict, total=False):
    """Type definition for blog post metadata from frontmatter."""
    slug: str
    title: str
    date: str
    author: str
    description: str
    excerpt: str
    tags: list[str]
    readTime: str


if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


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


class ProVersionThankYouView(TemplateView):
    template_name = "professional_thankyou.html"


class BRB(TemplateView):
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
        "checkout.stripe.com",
    ]

    if not url.startswith("/"):
        # For URLs, we need to validate the domain
        from urllib.parse import urlparse

        parsed_url = urlparse(url)

        if parsed_url.netloc and parsed_url.netloc not in ALLOWED_HOSTS:
            logger.warning(f"Suspicious redirect attempt to: {url}")
            raise SuspiciousOperation(
                f"Redirect to untrusted domain: {parsed_url.netloc}"
            )

        logger.info(f"Redirecting to: {url}")

        return HttpResponseRedirect(url)


class ProVersionView(generic.RedirectView):
    url = "https://www.fightpaperwork.com"


class IndexView(generic.TemplateView):
    template_name = "index.html"


class AboutView(generic.TemplateView):
    template_name = "about_us.html"


class OtherResourcesView(generic.TemplateView):
    template_name = "other_resources.html"
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Fetch RSS feeds synchronously to avoid async issues
        try:
            import requests
            import feedparser
            from datetime import datetime
            
            # KFF Health News RSS feeds
            kff_feeds = {
                'insurance': {
                    'name': 'KFF Health News - Insurance',
                    'url': 'http://kffhealthnews.org/topics/insurance/feed/',
                    'description': 'Insurance-related health policy news'
                },
                'uninsured': {
                    'name': 'KFF Health News - Uninsured', 
                    'url': 'http://kffhealthnews.org/topics/uninsured/feed/',
                    'description': 'News about uninsured populations and coverage'
                },
                'health-industry': {
                    'name': 'KFF Health News - Health Industry',
                    'url': 'http://kffhealthnews.org/topics/health-industry/feed/',
                    'description': 'Health industry news and analysis'
                }
            }
            
            rss_feeds = {}
            
            for feed_key, feed_info in kff_feeds.items():
                try:
                    # Fetch RSS feed synchronously
                    response = requests.get(
                        feed_info['url'], 
                        headers={'User-Agent': 'Mozilla/5.0 (compatible; HealthPolicyRSSBot/1.0)'},
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        feed = feedparser.parse(response.content)
                        
                        articles = []
                        for entry in feed.entries[:3]:  # Limit to 3 most recent articles
                            title = entry.get('title', '').strip()
                            url = entry.get('link', '')
                            
                            if title and url:
                                # Parse date
                                published_date = datetime.now()
                                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                                    try:
                                        published_date = datetime(*entry.published_parsed[:6])
                                    except (ValueError, TypeError):
                                        pass
                                
                                articles.append({
                                    'title': title,
                                    'url': url,
                                    'published_date': published_date,
                                    'formatted_date': published_date.strftime('%b %d, %Y')
                                })
                        
                        rss_feeds[feed_key] = {
                            'name': feed_info['name'],
                            'description': feed_info['description'],
                            'articles': articles
                        }
                    else:
                        logger.error(f"Failed to fetch {feed_info['url']}: {response.status_code}")
                        
                except Exception as e:
                    logger.error(f"Error fetching RSS feed {feed_info['url']}: {e}")
                    continue
            
            context['rss_feeds'] = rss_feeds
            
        except Exception as e:
            logger.error(f"Error setting up RSS feeds: {e}")
            context['rss_feeds'] = {}
        
        return context

class BlogView(generic.TemplateView):
    template_name = "blog.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slugs = []
        try:
            # Find the path to the blog posts JSON using the staticfiles storage.
            blog_posts_path = staticfiles_storage.path('blog_posts.json')
            with open(blog_posts_path, 'r', encoding='utf-8') as f:
                posts = json.load(f)
            # Extract slugs from the loaded posts
            slugs = [post.get('slug') for post in posts if post.get('slug')]
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Could not load or parse blog_posts.json: {e}")
            # Optionally, you could run the management command here as a fallback
            # from django.core.management import call_command
            # call_command('generate_blog_metadata')
            # and then try to load the file again.
            # For now, we'll just log the error and return an empty list.
        except Exception as e:
            logger.error(f"An unexpected error occurred while loading blog posts: {e}")
        
        context['blog_slugs'] = json.dumps(slugs)
        return context


class BlogPostView(generic.TemplateView):
    template_name = "blog_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = kwargs.get("slug", "")
        # Validate slug format
        if not re.match(r'^[a-zA-Z0-9_-]+$', slug):
            logger.warning(f"Invalid slug format: {slug}")
            context.update({"slug": slug, "post_title": None, "post_excerpt": None})
            return context

        try:
            # Securely find the path to the blog post markdown file.
            # staticfiles_storage.path() will handle finding the file and prevent
            # path traversal attacks. It raises an error if the file is not found.
            mdx_path = staticfiles_storage.path(f"blog/{slug}.md")
        except Exception:
            logger.warning(f"Blog post markdown file not found for slug: {slug}")
            context.update({"slug": slug, "post_title": None, "post_excerpt": None})
            return context

        # Load blog metadata from blog_posts.json
        blog_json_path = staticfiles_storage.path('blog_posts.json')
        post_info: BlogPostMetadata = {}
        try:
            with open(blog_json_path, "r", encoding="utf-8") as f:
                posts = json.load(f)

            if not isinstance(posts, list):
                logger.error(f"Invalid blog metadata format in {blog_json_path}: expected list")
                post_info = {}
            else:
                for post in posts:
                    if post.get("slug") == slug:
                        post_info = post
                        break
                else:
                    logger.info(f"Blog post not found for slug: {slug}")

        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load blog metadata from {blog_json_path}: {e}")
            post_info = {}
        except PermissionError as e:
            logger.error(f"Permission denied accessing blog metadata file {blog_json_path}: {e}")
            post_info = {}
        except UnicodeDecodeError as e:
            logger.error(f"Invalid file encoding for blog metadata file {blog_json_path}: {e}")
            post_info = {}
        except Exception as e:
            logger.error(f"Unexpected error loading blog metadata: {e}")
            post_info = {}

        context.update({
            "slug": slug,
            "post_title": post_info.get("title"),
            "post_excerpt": post_info.get("excerpt"),
        })
        return context


class ScanView(generic.TemplateView):
    template_name = "scrub.html"

    def get_context_data(self, **kwargs):
        return {"ocr_result": "", "upload_more": True}


class PrivacyPolicyView(generic.TemplateView):
    template_name = "privacy_policy.html"

    def get_context_data(self, **kwargs):
        return {"title": "Privacy Policy"}


class MHMDAView(generic.TemplateView):
    template_name = "mhmda.html"

    def get_context_data(self, **kwargs):
        return {"title": "Consumer Health Data Privacy Notice"}


class TermsOfServiceView(generic.TemplateView):
    template_name = "tos.html"

    def get_context_data(self, **kwargs):
        return {"title": "Terms of Service"}


class ContactView(generic.TemplateView):
    template_name = "contact.html"

    def get_context_data(self, **kwargs):
        return {"title": "Contact Us"}


class FAQView(generic.TemplateView):
    template_name = "faq.html"

    def get_context_data(self, **kwargs):
        return {"title": "Frequently Asked Questions"}


class MedicaidFAQView(generic.TemplateView):
    template_name = "faq_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = "medicaid-work-requirements-faq"
        
        # Validate slug and load FAQ content from static file
        if not re.match(r'^[a-zA-Z0-9_-]+$', slug):
            logger.warning(f"Invalid slug format: {slug}")
            context.update({"slug": slug, "faq_title": None, "faq_excerpt": None})
            return context

        try:
            # Securely find the path to the FAQ markdown file.
            staticfiles_storage.path(f"faq/{slug}.md")
            context.update({
                "title": "Medicaid Work Requirements FAQ",
                "slug": slug,
                "faq_title": "Medicaid Work Requirements FAQ",
                "faq_excerpt": "Frequently asked questions about Medicaid work requirements and how they may affect your healthcare coverage eligibility.",
            })
        except Exception:
            logger.warning(f"FAQ markdown file not found for slug: {slug}")
            context.update({"slug": slug, "faq_title": None, "faq_excerpt": None})
        
        return context


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

        logger.debug("Finishing up prior to appeal gen.")
        denial_id = form.cleaned_data["denial_id"]
        email = form.cleaned_data["email"]
        denial = models.Denial.objects.filter(
            denial_id=denial_id,
            hashed_email=models.Denial.get_hashed_email(email),
            semi_sekret=form.cleaned_data["semi_sekret"],
        ).get()

        # We copy _most_ of the input over for the form context
        elems = dict(request.POST)
        # Query dict is of lists
        elems = dict((k, v[0]) for k, v in elems.items())
        try:
            generated_questions: list[tuple[str, str]] = denial.generated_questions  # type: ignore
            qa_context_str = denial.qa_context
            qa_context: dict[str, str] = {}
            if qa_context_str:
                try:
                    qa_context = json.loads(qa_context_str)
                except json.JSONDecodeError:
                    qa_context["misc"] = qa_context_str
            restricted = ["csrfmiddlewaretoken", "denial_id", "email", "semi_sekret"]
            for k, v in elems.items():
                key = k
                if "appeal_generated_" in k:
                    key = generated_questions[int(k.split("_")[-1]) - 1][0]
                if isinstance(v, list):
                    elems[k] = v[0]
                if v and v != "" and v != "UNKNOWN" and key not in restricted:
                    qa_context[key] = v
            denial.qa_context = json.dumps(qa_context)
            denial.save()
        except Exception as e:
            logger.error(f"*********************Error updating medical context: {e}")

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
        # Legacy doesn't have denial id
        cleaned_data = form.cleaned_data
        if "denial_id" in cleaned_data:
            del cleaned_data["denial_id"]
        denial_response = common_view_logic.DenialCreatorHelper.create_or_update_denial(
            **form.cleaned_data,
        )

        # Store the denial ID in the session to maintain state across the multi-step form process
        # This allows the SessionRequiredMixin to verify the user is working with a valid denial
        self.request.session["denial_uuid"] = str(denial_response.uuid)
        self.request.session["denial_id"] = denial_response.denial_id

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
        print(request.session)
        # Don't enforce this rule for now in prod we want to wait for everyone to have a session
        force_session = settings.DEBUG or os.environ.get("TESTING", False)
        if (
            force_session
            and not request.session.get("denial_uuid")
            and not request.session.get("denial_id")
        ):
            print("Huzzah doing le check")
            denial_id = request.POST.get("denial_id") or request.GET.get("denial_id")
            if denial_id:
                request.session["denial_id"] = denial_id
            else:
                return redirect("process")
        return super().dispatch(request, *args, **kwargs)  # type: ignore


class EntityExtractView(SessionRequiredMixin, generic.FormView):
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
                "insurance_company": denial_response.insurance_company,
                "plan_id": denial_response.plan_id,
                "claim_id": denial_response.claim_id,
                "date_of_service": denial_response.date_of_service,
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


class PlanDocumentsView(SessionRequiredMixin, generic.FormView):
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


class DenialCollectedView(SessionRequiredMixin, generic.FormView):
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
        try:
            return self.do_post(request)
        except Exception as e:
            logger.opt(exception=True).error("Error processing Stripe webhook")
            return HttpResponse(status=500)

    def do_post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            return HttpResponse(status=401)
        except stripe_error.SignatureVerificationError as e:  # type: ignore
            logger.error(f"Invalid signature: {e}")
            return HttpResponse(status=403)

        common_view_logic.StripeWebhookHelper.handle_stripe_webhook(request, event)
        return HttpResponse(status=200)


class CompletePaymentView(View):
    def get(self, request):
        try:
            # Extract parameters from URL query string
            session_id = request.GET.get("session_id")
            cancel_url = request.GET.get(
                "cancel_url", "https://www.fighthealthinsurance.com/?q=ohno"
            )

            # Create data dictionary similar to what we'd get from POST
            data = {
                "session_id": session_id,
                "cancel_url": cancel_url,
            }

            return self.process_payment(data)
        except Exception as e:
            logger.opt(exception=True).error(
                "Error processing payment completion from GET"
            )
            return HttpResponse(status=500)

    def post(self, request):
        try:
            return self.do_post(request)
        except Exception as e:
            logger.opt(exception=True).error("Error processing payment completion")
            return HttpResponse(status=500)

    def do_post(self, request):
        data = json.loads(request.body)
        return self.process_payment(data)

    def process_payment(self, data):
        session_id = data.get("session_id")

        if not session_id:
            return HttpResponse(
                json.dumps({"error": "Missing session_id"}),
                status=400,
                content_type="application/json",
            )

        try:
            lost_session = models.LostStripeSession.objects.get(session_id=session_id)
            continue_url = lost_session.success_url
            cancel_url = lost_session.cancel_url
            payment_type = lost_session.payment_type
            metadata: dict[str, str] = lost_session.metadata  # type: ignore
            recovery_info_id = metadata.get("recovery_info_id")
            line_items = []
            if not recovery_info_id:
                line_items_json = metadata.get("line_items")
                if not line_items_json:
                    logger.error(f"No recover info found in metadata {metadata}")
                    return HttpResponse(
                        json.dumps({"error": "No recover info found in metadata"}),
                        status=400,
                        content_type="application/json",
                    )
                line_items = json.loads(line_items_json)
            else:
                line_items = StripeRecoveryInfo.objects.get(id=recovery_info_id).items
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=line_items,  # type: ignore
                mode="payment",
                success_url=continue_url or "https://www.fighthealthinsurance.com",
                cancel_url=cancel_url or "https://www.fighthealthinsurance.com",
                metadata=metadata,
            )

            return HttpResponse(
                json.dumps({"next_url": checkout_session.url}),
                status=200,
                content_type="application/json",
            )
        except models.LostStripeSession.DoesNotExist:
            return HttpResponse(
                json.dumps({"error": "Session not found"}),
                status=400,
                content_type="application/json",
            )
        except Exception as e:
            logger.opt(exception=e).error("Error in finishing payment")
            return HttpResponse(
                json.dumps({"error": f"Error {e}"}),
                status=500,
                content_type="application/json",
            )


@ensure_csrf_cookie
def chat_interface_view(request):
    """
    Render the chat interface for all user types:
    - Authenticated professional users
    - Authenticated patient users
    - Anonymous users (session-based)

    If the user hasn't accepted the terms of service yet, redirect to the consent form.
    """
    logger.debug(f"Chat interface view called with session: {request.session}")

    # Check if the user completed the consent process by looking for session data
    consent_completed = request.session.get("consent_completed", False)
    email = request.session.get("email", None)

    # If the user hasn't completed the consent process, redirect to the consent form
    if not consent_completed:
        logger.debug(
            "User has not completed consent process, redirecting to consent form."
        )
        return redirect("chat_consent")

    context = {
        "title": "Chat with FightHealthInsurance",
        "email": email,
    }
    logger.debug(f"Rendering chat interface with context: {context}")
    return render(request, "chat_interface.html", context)


class ChatUserConsentView(FormView):
    """
    View for collecting user consent and information before using the chat interface.
    This form collects personal information that is stored only in the browser's localStorage
    for privacy protection (scrubbing personal information from messages).
    """

    template_name = "chat_consent.html"
    form_class = UserConsentForm
    success_url = (
        "/chat/"  # Redirect to chat interface after successful form submission
    )

    def form_valid(self, form):
        # Mark consent as completed in the session
        self.request.session["consent_completed"] = True
        self.request.session["email"] = form.cleaned_data.get(
            "email"
        )  # Used for data deletion requests
        self.request.session.save()
        if form.cleaned_data.get("subscribe"):
            name = f"{form.cleaned_data.get('first_name')} {form.cleaned_data.get('last_name')}"
            # Does the user want to subscribe to the newsletter?
            models.MailingListSubscriber.objects.create(
                email=form.cleaned_data.get("email"),
                phone=form.cleaned_data.get("phone"),
                name=name,
                comments="From chat consent form",
            )

        # No need to save form data to database - it will be saved in browser localStorage via JavaScript
        return super().form_valid(form)

    def get(self, request, *args, **kwargs):
        # Check if the user already has a session key
        if not request.session.session_key:
            request.session.save()

        # Continue with normal form rendering
        return super().get(request, *args, **kwargs)

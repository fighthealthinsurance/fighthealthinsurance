import asyncio
import json
import os
import re
import typing
from typing import TypedDict

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import SuspiciousOperation
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    HttpResponseForbidden,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template import loader
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views import View, generic
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.views.generic.base import TemplateView
from django.views.generic.edit import FormView

import stripe
from django_encrypted_filefield.crypt import Cryptographer
from loguru import logger
from PIL import Image

from fighthealthinsurance import common_view_logic, forms as core_forms, models
from fighthealthinsurance.chat_forms import UserConsentForm
from fighthealthinsurance.models import StripeRecoveryInfo


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


class PBSNewsHourView(generic.TemplateView):
    template_name = "as_seen_on_pbs.html"


class OtherResourcesView(generic.TemplateView):
    template_name = "other_resources.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Fetch RSS feeds synchronously to avoid async issues
        try:
            from datetime import datetime

            import feedparser
            import requests

            # KFF Health News RSS feeds
            kff_feeds = {
                "insurance": {
                    "name": "KFF Health News - Insurance",
                    "url": "https://kffhealthnews.org/topics/insurance/feed/",
                    "description": "Insurance-related health policy news",
                },
                "uninsured": {
                    "name": "KFF Health News - Uninsured",
                    "url": "https://kffhealthnews.org/topics/uninsured/feed/",
                    "description": "News about uninsured populations and coverage",
                },
                "health-industry": {
                    "name": "KFF Health News - Health Industry",
                    "url": "https://kffhealthnews.org/topics/health-industry/feed/",
                    "description": "Health industry news and analysis",
                },
            }

            rss_feeds = {}

            for feed_key, feed_info in kff_feeds.items():
                try:
                    # Fetch RSS feed synchronously
                    response = requests.get(
                        feed_info["url"],
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; HealthPolicyRSSBot/1.0)"
                        },
                        timeout=10,
                    )

                    if response.status_code == 200:
                        feed = feedparser.parse(response.content)

                        articles = []
                        for entry in feed.entries[
                            :3
                        ]:  # Limit to 3 most recent articles
                            title = entry.get("title", "").strip()
                            url = entry.get("link", "")

                            if title and url:
                                # Parse date
                                published_date = datetime.now()
                                if (
                                    hasattr(entry, "published_parsed")
                                    and entry.published_parsed
                                ):
                                    try:
                                        published_date = datetime(
                                            *entry.published_parsed[:6]
                                        )
                                    except (ValueError, TypeError):
                                        pass

                                articles.append(
                                    {
                                        "title": title,
                                        "url": url,
                                        "published_date": published_date,
                                        "formatted_date": published_date.strftime(
                                            "%b %d, %Y"
                                        ),
                                    }
                                )

                        rss_feeds[feed_key] = {
                            "name": feed_info["name"],
                            "description": feed_info["description"],
                            "articles": articles,
                        }
                    else:
                        logger.error(
                            f"Failed to fetch {feed_info['url']}: {response.status_code}"
                        )

                except Exception as e:
                    logger.error(f"Error fetching RSS feed {feed_info['url']}: {e}")
                    continue

            context["rss_feeds"] = rss_feeds

        except Exception as e:
            logger.error(f"Error setting up RSS feeds: {e}")
            context["rss_feeds"] = {}

        return context


class BlogView(generic.TemplateView):
    template_name = "blog.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slugs = []
        try:
            with staticfiles_storage.open("blog_posts.json", "r") as f:
                contents = f.read()
                if not isinstance(contents, str):
                    contents = contents.decode("utf-8")
                posts = json.loads(contents)
            # Extract slugs from the loaded posts
            slugs = [post.get("slug") for post in posts if post.get("slug")]
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Could not load or parse blog_posts.json: {e}")
            # Optionally, you could run the management command here as a fallback
            # from django.core.management import call_command
            # call_command('generate_blog_metadata')
            # and then try to load the file again.
            # For now, we'll just log the error and return an empty list.
        except Exception as e:
            logger.error(f"An unexpected error occurred while loading blog posts: {e}")
            raise

        context["blog_slugs"] = json.dumps(slugs)
        return context


class BlogPostView(generic.TemplateView):
    template_name = "blog_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = kwargs.get("slug", "")
        # Validate slug format
        if not re.match(r"^[a-zA-Z0-9_-]+$", slug):
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
        blog_json_path = staticfiles_storage.path("blog_posts.json")
        post_info: BlogPostMetadata = {}
        try:
            with open(blog_json_path, "r", encoding="utf-8") as f:
                posts = json.load(f)

            if not isinstance(posts, list):
                logger.error(
                    f"Invalid blog metadata format in {blog_json_path}: expected list"
                )
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
            logger.error(
                f"Permission denied accessing blog metadata file {blog_json_path}: {e}"
            )
            post_info = {}
        except UnicodeDecodeError as e:
            logger.error(
                f"Invalid file encoding for blog metadata file {blog_json_path}: {e}"
            )
            post_info = {}
        except Exception as e:
            logger.error(f"Unexpected error loading blog metadata: {e}")
            post_info = {}

        context.update(
            {
                "slug": slug,
                "post_title": post_info.get("title"),
                "post_excerpt": post_info.get("excerpt"),
            }
        )
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
        if not re.match(r"^[a-zA-Z0-9_-]+$", slug):
            logger.warning(f"Invalid slug format: {slug}")
            context.update({"slug": slug, "faq_title": None, "faq_excerpt": None})
            return context

        try:
            # Securely find the path to the FAQ markdown file.
            staticfiles_storage.path(f"faq/{slug}.md")
            context.update(
                {
                    "title": "Medicaid Work Requirements FAQ",
                    "slug": slug,
                    "faq_title": "Medicaid Work Requirements FAQ",
                    "faq_excerpt": "Frequently asked questions about Medicaid work requirements and how they may affect your healthcare coverage eligibility.",
                }
            )
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


class CategorizeReview(View):
    """View for the categorize/review page that supports GET for back navigation."""

    def get(self, request):
        """Handle GET requests for back navigation to categorize/review page."""
        denial_id = request.GET.get("denial_id")
        email = request.GET.get("email")
        semi_sekret = request.GET.get("semi_sekret")

        if not all([denial_id, email, semi_sekret]):
            return redirect("scan")

        # Validate denial exists
        try:
            denial = models.Denial.objects.get(
                denial_id=denial_id, semi_sekret=semi_sekret
            )
        except models.Denial.DoesNotExist:
            return redirect("scan")

        # Check for microsite default procedure in session
        procedure = denial.procedure
        default_procedure = request.session.get("default_procedure", "")
        microsite_title = request.session.get("microsite_title", "")
        used_default_procedure = False

        if not procedure and default_procedure:
            procedure = default_procedure
            used_default_procedure = True

        # Build the PostInferedForm with denial data
        form = core_forms.PostInferedForm(
            initial={
                "denial_type": list(denial.denial_type.all()),
                "denial_id": denial.denial_id,
                "email": email,
                "your_state": denial.your_state,
                "procedure": procedure,
                "diagnosis": denial.diagnosis,
                "semi_sekret": denial.semi_sekret,
                "insurance_company": denial.insurance_company,
                "plan_id": denial.plan_id,
                "claim_id": denial.claim_id,
                "date_of_service": denial.date_of_service,
            }
        )

        context = {
            "post_infered_form": form,
            "upload_more": True,
            "current_step": 5,
            "back_url": build_back_url("dvc", denial_id, email, semi_sekret),
            "back_label": "Back to plan documents",
        }

        # Add microsite prefill note if applicable
        if used_default_procedure and microsite_title:
            context["microsite_prefill_note"] = True
            context["microsite_title"] = microsite_title
            context["default_procedure"] = default_procedure

        return render(
            request,
            "categorize.html",
            context=context,
        )


class FindNextSteps(View):
    def get(self, request):
        """Handle GET requests for back navigation to outside_help/questions page."""
        denial_id = request.GET.get("denial_id")
        email = request.GET.get("email")
        semi_sekret = request.GET.get("semi_sekret")

        if not all([denial_id, email, semi_sekret]):
            return redirect("scan")

        # Validate denial exists
        try:
            denial = models.Denial.objects.get(
                denial_id=denial_id, semi_sekret=semi_sekret
            )
        except models.Denial.DoesNotExist:
            return redirect("scan")

        # Get the next step info based on denial
        next_step_info = (
            common_view_logic.FindNextStepsHelper.find_next_steps_for_denial(
                denial, email
            )
        )
        denial_ref_form = core_forms.DenialRefForm(
            initial={
                "denial_id": denial_id,
                "email": email,
                "semi_sekret": semi_sekret,
            }
        )
        return render(
            request,
            "outside_help.html",
            context={
                "outside_help_details": next_step_info.outside_help_details,
                "combined": next_step_info.combined_form,
                "denial_form": denial_ref_form,
                "current_step": 6,
                "back_url": build_back_url(
                    "categorize_review", denial_id, email, semi_sekret
                ),
                "back_label": "Back to review",
            },
        )

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
                    "current_step": 6,
                    "back_url": build_back_url(
                        "categorize_review",
                        denial_id,
                        email,
                        next_step_info.semi_sekret,
                    ),
                    "back_label": "Back to review",
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
                "current_step": 8,
                "back_url": build_back_url(
                    "generate_appeal",
                    form.cleaned_data["denial_id"],
                    form.cleaned_data["email"],
                    form.cleaned_data["semi_sekret"],
                ),
                "back_label": "Back to appeals",
                "fhi_always_restore": True,  # Always restore PII from localStorage
            },
        )


class GenerateAppeal(View):
    def get(self, request):
        """Handle GET requests for back navigation to appeals page."""
        denial_id = request.GET.get("denial_id")
        email = request.GET.get("email")
        semi_sekret = request.GET.get("semi_sekret")

        if not all([denial_id, email, semi_sekret]):
            return redirect("scan")

        # Validate denial exists
        try:
            denial = models.Denial.objects.get(
                denial_id=denial_id, semi_sekret=semi_sekret
            )
        except models.Denial.DoesNotExist:
            return redirect("scan")

        # Build form context from denial
        elems = {
            "denial_id": denial_id,
            "email": email,
            "semi_sekret": semi_sekret,
        }

        return render(
            request,
            "appeals.html",
            context={
                "form_context": json.dumps(elems),
                "user_email": email,
                "denial_id": denial_id,
                "semi_sekret": semi_sekret,
                "current_step": 7,
                "back_url": build_back_url(
                    "find_next_steps", denial_id, email, semi_sekret
                ),
                "back_label": "Back to questions",
            },
        )

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
                "current_step": 7,
                "back_url": build_back_url(
                    "find_next_steps",
                    form.cleaned_data["denial_id"],
                    form.cleaned_data["email"],
                    form.cleaned_data["semi_sekret"],
                ),
                "back_label": "Back to questions",
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
        ocr_result = self.get_ocr_result() or ""
        context["upload_more"] = True

        # Capture microsite parameters from URL for display
        default_procedure = self.request.GET.get("default_procedure", "")
        default_condition = self.request.GET.get("default_condition", "")
        microsite_slug = self.request.GET.get("microsite_slug", "")
        microsite_title = self.request.GET.get("microsite_title", "")

        if default_procedure or default_condition:
            context["default_procedure"] = default_procedure
            context["default_condition"] = default_condition
            context["microsite_slug"] = microsite_slug
            context["microsite_title"] = microsite_title

            # If no OCR result yet, provide default denial text for users
            # coming from microsites who may not have a denial letter
            if not ocr_result:
                if default_procedure and default_condition:
                    ocr_result = f"The patient with {default_condition} was denied {default_procedure}."
                elif default_procedure:
                    ocr_result = f"The patient was denied {default_procedure}."
                elif default_condition:
                    ocr_result = f"The patient with {default_condition} was denied treatment."

        context["ocr_result"] = ocr_result

        return context

    def get_success_url(self):
        pass

    def form_valid(self, form):
        # Legacy doesn't have denial id
        cleaned_data = form.cleaned_data
        if "denial_id" in cleaned_data:
            del cleaned_data["denial_id"]

        # Handle mailing list subscription
        if cleaned_data.get("subscribe"):
            email = cleaned_data.get("email")
            # Get name from the POST data (it's not stored in cleaned_data for privacy)
            fname = self.request.POST.get("fname", "")
            lname = self.request.POST.get("lname", "")
            name = f"{fname} {lname}".strip()
            defaults = {"comments": "From appeal flow"}
            if len(name) > 2:
                defaults["name"] = name
            # Use get_or_create to avoid duplicate subscriptions
            try:
                models.MailingListSubscriber.objects.get_or_create(
                    email=email,
                    defaults=defaults,
                )
            except Exception as e:
                logger.debug(f"Error subscribing {email} to mailing list: {e}")
                try:
                    models.MailingListSubscriber.objects.filter(email=email).update(
                        **defaults
                    )
                except Exception as e2:
                    logger.warning(f"Error updating subscriber? {email}!?!")

        # Get microsite slug from request if available and validate it
        microsite_slug = self.request.POST.get(
            "microsite_slug"
        ) or self.request.GET.get("microsite_slug", "")
        if microsite_slug:
            from fighthealthinsurance.microsites import get_microsite
            if get_microsite(microsite_slug):
                cleaned_data["microsite_slug"] = microsite_slug
            else:
                logger.warning(f"Invalid microsite_slug received: {microsite_slug}")

        denial_response = common_view_logic.DenialCreatorHelper.create_or_update_denial(
            **cleaned_data,
        )

        # Store the denial ID in the session to maintain state across the multi-step form process
        # This allows the SessionRequiredMixin to verify the user is working with a valid denial
        self.request.session["denial_uuid"] = str(denial_response.uuid)
        self.request.session["denial_id"] = denial_response.denial_id

        # Store microsite data in session for prefilling later in the flow
        default_procedure = self.request.POST.get(
            "default_procedure"
        ) or self.request.GET.get("default_procedure", "")
        default_condition = self.request.POST.get(
            "default_condition"
        ) or self.request.GET.get("default_condition", "")
        microsite_slug = self.request.POST.get(
            "microsite_slug"
        ) or self.request.GET.get("microsite_slug", "")
        microsite_title = self.request.POST.get(
            "microsite_title"
        ) or self.request.GET.get("microsite_title", "")

        if default_procedure or default_condition:
            self.request.session["default_procedure"] = default_procedure
            self.request.session["default_condition"] = default_condition
            self.request.session["microsite_slug"] = microsite_slug
            self.request.session["microsite_title"] = microsite_title

        form = core_forms.HealthHistory(
            initial={
                "denial_id": denial_response.denial_id,
                "email": cleaned_data["email"],
                "semi_sekret": denial_response.semi_sekret,
            }
        )

        return render(
            self.request,
            "health_history.html",
            context={
                "form": form,
                "next": reverse("hh"),
                "current_step": 2,
                "back_url": reverse("scan"),
            },
        )


def build_back_url(url_name: str, denial_id, email: str, semi_sekret: str) -> str:
    """
    Build a back URL with denial ref parameters encoded.
    This allows the user to navigate back and still have the form fields populated.
    """
    from urllib.parse import urlencode

    base_url = reverse(url_name)
    params = urlencode(
        {
            "denial_id": denial_id,
            "email": email,
            "semi_sekret": semi_sekret,
        }
    )
    return f"{base_url}?{params}"


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

    def get_denial_ref_from_request(self) -> dict:
        """
        Get denial ref fields from GET params (for back navigation) or POST.
        Also validates that the session matches if we have a session.
        Returns dict with denial_id, email, and semi_sekret.
        """
        # Try GET params first (back navigation), then POST
        if self.request.method == "GET":
            denial_id = self.request.GET.get("denial_id")
            email = self.request.GET.get("email")
            semi_sekret = self.request.GET.get("semi_sekret")
        else:
            denial_id = self.request.POST.get("denial_id")
            email = self.request.POST.get("email")
            semi_sekret = self.request.POST.get("semi_sekret")

        if denial_id and email and semi_sekret:
            # Validate the denial exists and semi_sekret matches
            try:
                denial = models.Denial.objects.get(
                    denial_id=denial_id,
                    semi_sekret=semi_sekret,
                )
                # Check session matches if we have one
                session_denial_id = self.request.session.get("denial_id")
                if session_denial_id and str(session_denial_id) != str(denial_id):
                    logger.warning(
                        f"Session denial_id {session_denial_id} doesn't match "
                        f"request denial_id {denial_id}"
                    )
                    # Still allow it - the semi_sekret validates ownership
                return {
                    "denial_id": denial.denial_id,
                    "email": email,
                    "semi_sekret": semi_sekret,
                }
            except models.Denial.DoesNotExist:
                logger.warning(f"Invalid denial lookup: {denial_id}")

        return {}

    def get_back_url(self, url_name: str, denial_ref: dict) -> str:
        """Build a back URL with denial ref params."""
        if denial_ref:
            return build_back_url(
                url_name,
                denial_ref.get("denial_id", ""),
                denial_ref.get("email", ""),
                denial_ref.get("semi_sekret", ""),
            )
        return reverse(url_name)


class EntityExtractView(SessionRequiredMixin, generic.FormView):
    form_class = core_forms.EntityExtractForm
    template_name = "entity_extract.html"

    def get_initial(self):
        """Populate form with denial ref data from URL params for back navigation."""
        initial = super().get_initial()
        initial.update(self.get_denial_ref_from_request())
        return initial

    def get_context_data(self, **kwargs):
        """Add context needed for the template."""
        context = super().get_context_data(**kwargs)
        denial_ref = self.get_denial_ref_from_request()
        context["next"] = reverse("eev")
        context["current_step"] = 4
        context["back_url"] = self.get_back_url("dvc", denial_ref)
        context["back_label"] = "Back to plan documents"
        # form_context needed for entity fetcher JS
        context["form_context"] = denial_ref
        return context

    def form_valid(self, form):
        denial_response = common_view_logic.DenialCreatorHelper.update_denial(
            **form.cleaned_data,
        )

        email = form.cleaned_data["email"]

        # Check if we have a default procedure from microsite that should be used
        procedure = denial_response.procedure
        default_procedure = self.request.session.get("default_procedure", "")
        microsite_title = self.request.session.get("microsite_title", "")
        used_default_procedure = False

        # If no procedure was extracted and we have a default from microsite, use it
        if not procedure and default_procedure:
            procedure = default_procedure
            used_default_procedure = True

        new_form = core_forms.PostInferedForm(
            initial={
                "denial_type": denial_response.selected_denial_type,
                "denial_id": denial_response.denial_id,
                "email": email,
                "your_state": denial_response.your_state,
                "procedure": procedure,
                "diagnosis": denial_response.diagnosis,
                "semi_sekret": denial_response.semi_sekret,
                "insurance_company": denial_response.insurance_company,
                "plan_id": denial_response.plan_id,
                "claim_id": denial_response.claim_id,
                "date_of_service": denial_response.date_of_service,
            }
        )

        context = {
            "post_infered_form": new_form,
            "upload_more": True,
            "current_step": 5,
            "back_url": build_back_url(
                "dvc",
                denial_response.denial_id,
                email,
                denial_response.semi_sekret,
            ),
            "back_label": "Back to plan documents",
        }

        # Add microsite prefill note if applicable
        if used_default_procedure and microsite_title:
            context["microsite_prefill_note"] = True
            context["microsite_title"] = microsite_title
            context["default_procedure"] = default_procedure

        return render(
            self.request,
            "categorize.html",
            context=context,
        )


class PlanDocumentsView(SessionRequiredMixin, generic.FormView):
    form_class = core_forms.HealthHistory
    template_name = "health_history.html"

    def get_initial(self):
        """Populate form with denial ref data from URL params for back navigation."""
        initial = super().get_initial()
        initial.update(self.get_denial_ref_from_request())
        return initial

    def get_context_data(self, **kwargs):
        """Add context needed for the template."""
        context = super().get_context_data(**kwargs)
        context["next"] = reverse("hh")  # Form posts to itself
        context["current_step"] = 2
        context["back_url"] = reverse("scan")  # Scan doesn't need denial ref
        return context

    def form_valid(self, form):
        denial_response = common_view_logic.DenialCreatorHelper.update_denial(
            **form.cleaned_data,
        )

        email = form.cleaned_data["email"]
        new_form = core_forms.PlanDocumentsForm(
            initial={
                "denial_id": denial_response.denial_id,
                "email": email,
                "semi_sekret": denial_response.semi_sekret,
            }
        )

        return render(
            self.request,
            "plan_documents.html",
            context={
                "form": new_form,
                "next": reverse("dvc"),
                "current_step": 3,
                "back_url": build_back_url(
                    "hh",
                    denial_response.denial_id,
                    email,
                    denial_response.semi_sekret,
                ),
            },
        )


class DenialCollectedView(SessionRequiredMixin, generic.FormView):
    form_class = core_forms.PlanDocumentsForm
    template_name = "plan_documents.html"

    def get_initial(self):
        """Populate form with denial ref data from URL params for back navigation."""
        initial = super().get_initial()
        initial.update(self.get_denial_ref_from_request())
        return initial

    def get_context_data(self, **kwargs):
        """Add context needed for the template."""
        context = super().get_context_data(**kwargs)
        denial_ref = self.get_denial_ref_from_request()
        context["next"] = reverse("dvc")  # Form posts to itself
        context["current_step"] = 3
        context["back_url"] = self.get_back_url("hh", denial_ref)
        return context

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
                "current_step": 4,
                "back_url": build_back_url(
                    "hh",
                    form.cleaned_data["denial_id"],
                    form.cleaned_data["email"],
                    form.cleaned_data["semi_sekret"],
                ),
                "back_label": "Forgot some plan documents?",
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
        except stripe.SignatureVerificationError as e:  # type: ignore
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

    # Check for default_procedure and default_condition from microsite URL params
    default_procedure = request.GET.get("default_procedure", "")
    default_condition = request.GET.get("default_condition", "")
    microsite_slug = request.GET.get("microsite_slug", "")

    context = {
        "title": "Chat with FightHealthInsurance",
        "email": email,
        "default_procedure": default_procedure,
        "default_condition": default_condition,
        "microsite_slug": microsite_slug,
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

    def get_success_url(self):
        """Preserve query parameters when redirecting to chat."""
        # Get the base success URL
        url = self.success_url
        
        # Preserve microsite-related query parameters
        query_params = []
        for param in ['default_procedure', 'default_condition', 'microsite_slug', 'microsite_title']:
            value = self.request.GET.get(param) or self.request.POST.get(param)
            if value:
                from urllib.parse import urlencode
                query_params.append((param, value))
        
        if query_params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(query_params)}"
        
        return url

    def get_context_data(self, **kwargs):
        """Pass query parameters to template so they can be preserved in hidden fields."""
        context = super().get_context_data(**kwargs)
        # Add microsite params to context so they can be included as hidden fields
        context['default_procedure'] = self.request.GET.get('default_procedure', '')
        context['default_condition'] = self.request.GET.get('default_condition', '')
        context['microsite_slug'] = self.request.GET.get('microsite_slug', '')
        context['microsite_title'] = self.request.GET.get('microsite_title', '')
        return context

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


@csrf_exempt
@require_http_methods(["POST"])
def create_pwyw_checkout(request):
    """Create a Stripe checkout session for pay-what-you-want donations."""
    try:
        data = json.loads(request.body)
        amount = int(data.get("amount", 0))

        if amount <= 0:
            return HttpResponse(
                json.dumps(
                    {"success": True, "message": "Free usage - no payment needed"}
                ),
                status=200,
                content_type="application/json",
            )

        stripe.api_key = settings.STRIPE_API_SECRET_KEY

        # Create a checkout session with the specified amount
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount * 100,  # Convert dollars to cents
                        "product_data": {
                            "name": "Fight Health Insurance - Pay What You Want",
                            "description": "Support our mission to help people fight health insurance denials",
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=request.build_absolute_uri("/") + "?donation=success",
            cancel_url=request.build_absolute_uri("/"),
            metadata={
                "payment_type": "donation",
                "donation_type": "pwyw",
                "source": "checkout",
            },
        )

        return HttpResponse(
            json.dumps({"success": True, "url": checkout_session.url}),
            status=200,
            content_type="application/json",
        )
    except Exception as e:
        logger.opt(exception=e).error("Error creating PWYW checkout")
        return HttpResponse(
            json.dumps({"success": False, "error": str(e)}),
            status=500,
            content_type="application/json",
        )


class UnsubscribeView(View):
    """View for handling mailing list unsubscribe requests."""

    def get(self, request: HttpRequest, token: str) -> HttpResponseBase:
        """Handle GET request to unsubscribe a mailing list subscriber by token."""
        try:
            subscriber = models.MailingListSubscriber.objects.get(
                unsubscribe_token=token
            )
            email = subscriber.email
            subscriber.delete()
            return render(
                request,
                "unsubscribed.html",
                context={
                    "title": "Unsubscribed",
                    "email": email,
                },
            )
        except models.MailingListSubscriber.DoesNotExist:
            return render(
                request,
                "unsubscribed.html",
                context={
                    "title": "Unsubscribed",
                    "email": None,
                    "error": "This unsubscribe link is invalid or has already been used.",
                },
            )


class ChooserView(TemplateView):
    """View for the Chooser (Best-Of Selection) interface."""

    template_name = "chooser.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Help Us Improve Our Chooser"

        # Trigger async pre-fill of tasks so user doesn't have to wait
        from fighthealthinsurance.chooser_tasks import trigger_prefill_async

        try:
            trigger_prefill_async()
        except Exception as e:
            # Don't let pre-fill errors break page load
            pass

        return context


class MicrositeView(TemplateView):
    """View for microsite landing pages."""

    template_name = "microsite.html"

    def get(self, request, slug, *args, **kwargs):
        from fighthealthinsurance.microsites import get_microsite

        microsite = get_microsite(slug)
        if microsite is None:
            from django.http import Http404

            raise Http404(f"Microsite '{slug}' not found")

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.kwargs.get("slug", "")

        from fighthealthinsurance.microsites import get_microsite

        microsite = get_microsite(slug)
        if microsite:
            context["microsite"] = microsite
            context["title"] = microsite.title

        return context

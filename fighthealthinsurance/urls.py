"""fighthealthinsurance URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import os
from typing import List, Union

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.staticfiles.storage import staticfiles_storage
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.http import HttpRequest, HttpResponseBase
from django.urls import URLPattern, URLResolver, include, path, re_path
from django.views.decorators.cache import cache_control, cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic.base import RedirectView

from fighthealthinsurance import fax_views, staff_views, views
from fighthealthinsurance.sitemap import sitemap_view


def trigger_error(request: HttpRequest) -> HttpResponseBase:
    raise Exception("Test error")


def add_brand_patterns(
    patterns: List[Union[URLPattern, URLResolver]], prefix: str, name_prefix: str
) -> List[Union[URLPattern, URLResolver]]:
    """
    Create branded URL patterns by prefixing routes and names.

    Takes a list of URL patterns and creates new patterns with:
    - Route prefixed with the given prefix (e.g., "appealmyclaims/")
    - Name prefixed with the given name prefix (e.g., "amc_")

    Args:
        patterns: List of URLPattern/URLResolver objects to duplicate
        prefix: String to prepend to each route (e.g., "appealmyclaims/")
        name_prefix: String to prepend to each URL name (e.g., "amc_")

    Returns:
        List of new URL patterns with branded routes and names
    """
    branded_patterns: List[Union[URLPattern, URLResolver]] = []
    for pattern in patterns:
        if isinstance(pattern, URLPattern) and pattern.name:
            # Extract the route string from the pattern
            # pattern.pattern is a RoutePattern object with a _route attribute
            original_route = str(pattern.pattern)
            # Create new path with prefixed route and name
            new_route = f"{prefix}{original_route}"
            new_name = f"{name_prefix}{pattern.name}"
            branded_patterns.append(path(new_route, pattern.callback, name=new_name))
    return branded_patterns


urlpatterns: List[Union[URLPattern, URLResolver]] = [
    # Internal-ish-views
    path("ziggy/rest/", include("fighthealthinsurance.rest_urls")),
    path("timbit/sentry-debug/", trigger_error),
    # Add webhook handler
    path(
        "webhook/stripe/",
        csrf_exempt(views.StripeWebhookView.as_view()),
        name="stripe-webhook",
    ),
    path(
        "webhook/stripe",
        csrf_exempt(views.StripeWebhookView.as_view()),
        name="stripe-webhook-no-slash",
    ),
    path(
        "stripe/finish",
        csrf_exempt(views.CompletePaymentView.as_view()),
        name="complete_payment",
    ),
    path(
        "v0/pwyw/checkout",
        views.create_pwyw_checkout,
        name="pwyw_checkout",
    ),
    re_path("timbit/sentry-debug/(?P<path>.+)", trigger_error, name="fake_fetch_url"),
    path("timbit/charts/", include(("charts.urls", "charts"), namespace="charts")),
    path("timbit/admin/", admin.site.urls),
    path("", include("django_prometheus.urls")),
    path(
        "timbit/help/",
        staff_member_required(staff_views.StaffDashboardView.as_view()),
        name="staff_dashboard",
    ),
    path(
        "timbit/help/followup_sched",
        staff_member_required(staff_views.ScheduleFollowUps.as_view()),
    ),
    path(
        "timbit/help/followup_sender_test",
        staff_member_required(staff_views.FollowUpEmailSenderView.as_view()),
    ),
    path(
        "timbit/help/thankyou_sender_test",
        staff_member_required(staff_views.ThankyouSenderView.as_view()),
    ),
    path(
        "timbit/help/followup_fax_test",
        staff_member_required(staff_views.FollowUpFaxSenderView.as_view()),
    ),
    path(
        "timbit/help/activate_pro",
        staff_member_required(staff_views.ActivateProUserView.as_view()),
        name="activate_pro",
    ),
    path(
        "timbit/help/enable_beta",
        staff_member_required(staff_views.EnableBetaForDomainView.as_view()),
        name="enable_beta",
    ),
    path(
        "timbit/help/send_mailing_list_mail",
        staff_member_required(staff_views.SendMailingListMailView.as_view()),
        name="send_mailing_list_mail",
    ),
    # Authentication
    path("v0/auth/", include("fhi_users.urls")),
    # stripe integration (TODO webhooks go here)
    # These are links we e-mail people so might have some extra junk.
    # So if there's an extra / or . at the end we ignore it.
    path(
        "v0/followup/<uuid:uuid>/<slug:hashed_email>/<slug:follow_up_semi_sekret>",
        views.FollowUpView.as_view(),
        name="followup",
    ),
    path(
        "v0/followup/<uuid:uuid>/<slug:hashed_email>/<slug:follow_up_semi_sekret>.",
        views.FollowUpView.as_view(),
        name="followup-with-a-period",
    ),
    path(
        "v0/followup/<uuid:uuid>/<slug:hashed_email>/<slug:followup_semi_sekret>/",
        views.FollowUpView.as_view(),
        name="followup-with-trailing-slash",
    ),
    # Unsubscribe from mailing list
    path(
        "v0/unsubscribe/<slug:token>",
        views.UnsubscribeView.as_view(),
        name="unsubscribe",
    ),
    # Fax follow up
    # So if there's an extra / or . at the end we ignore it.
    path(
        "v0/faxfollowup/<uuid:uuid>/<slug:hashed_email>",
        fax_views.FaxFollowUpView.as_view(),
        name="fax-followup",
    ),
    path(
        "v0/faxfollowup/<uuid:uuid>/<slug:hashed_email>.",
        fax_views.FaxFollowUpView.as_view(),
        name="fax-followup-with-a-period",
    ),
    path(
        "v0/faxfollowup/<uuid:uuid>/<slug:hashed_email>/",
        fax_views.FaxFollowUpView.as_view(),
        name="fax-followup-with-trailing-slash",
    ),
    # Back to normal stuff
    path(
        "v0/sendfax/<uuid:uuid>/<slug:hashed_email>/",
        fax_views.SendFaxView.as_view(),
        name="sendfaxview",
    ),
    path(
        "v0/stagefax",
        fax_views.StageFaxView.as_view(),
        name="stagefaxview",
    ),
    # View an appeal
    path(
        "v0/appeal/<uuid:appeal_uuid>/appeal.pdf",
        views.AppealFileView.as_view(),
        name="appeal_file_view",
    ),
    path(
        "process",
        sensitive_post_parameters("email")(views.InitialProcessView.as_view()),
        name="process",
    ),
    path(
        "v0/combined_collected_view",
        sensitive_post_parameters("email")(views.DenialCollectedView.as_view()),
        name="dvc",
    ),
    path("v0/plan_documents", views.PlanDocumentsView.as_view(), name="hh"),
    path("v0/categorize", views.EntityExtractView.as_view(), name="eev"),
    path(
        "categorize_review",
        sensitive_post_parameters("email")(views.CategorizeReview.as_view()),
        name="categorize_review",
    ),
    path(
        "server_side_ocr",
        sensitive_post_parameters("email")(views.OCRView.as_view()),
        name="server_side_ocr",
    ),
    # FHI-specific routes (not branded)
    path(
        "how-to-help",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.HowToHelpView.as_view())
        ),
        name="how-to-help",
    ),
    path(
        "preparing-for-2026",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.Preparing2026View.as_view())
        ),
        name="preparing-2026",
    ),
    path(
        "as-seen-on-pbs",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.PBSNewsHourView.as_view())
        ),
        name="pbs-newshour",
    ),
    path(
        "bingo",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.BingoView.as_view())),
        name="bingo",
    ),
    path(
        "other-resources",
        sensitive_post_parameters("email")(views.OtherResourcesView.as_view()),
        name="other-resources",
    ),
    path(
        "blog/",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.BlogView.as_view())),
        name="blog",
    ),
    path(
        "blog/<slug:slug>/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.BlogPostView.as_view())
        ),
        name="blog-post",
    ),
    path(
        "faq/",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.FAQView.as_view())),
        name="faq",
    ),
    path(
        "faq/medicaid/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.MedicaidFAQView.as_view())
        ),
        name="medicaid-faq",
    ),
    path(
        "denial-language/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.DenialLanguageLibraryView.as_view())
        ),
        name="denial-language-library",
    ),
    path(
        "state-help/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.StateHelpIndexView.as_view())
        ),
        name="state_help_index",
    ),
    path(
        "state-help/<slug:slug>/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.StateHelpView.as_view())
        ),
        name="state_help",
    ),
    path(
        "pro_version", csrf_exempt(views.ProVersionView.as_view()), name="pro_version"
    ),
    path(
        "pro_version_thankyou",
        csrf_exempt(views.ProVersionThankYouView.as_view()),
        name="pro_version_thankyou",
    ),
    path("share_denial", views.ShareDenialView.as_view(), name="share_denial"),
    path("share_appeal", views.ShareAppealView.as_view(), name="share_appeal"),
    path("remove_data", views.RemoveDataView.as_view(), name="remove_data"),
    path(
        "mhmda",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.MHMDAView.as_view())),
        name="mhmda",
    ),
    path(
        "sitemap.xml",
        cache_control(public=True)(cache_page(60 * 60 * 24)(sitemap_view)),
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path(
        "favicon.ico",
        RedirectView.as_view(url=staticfiles_storage.url("images/favicon.ico")),
    ),
]

# Don't break people already in the flow but "drain" the people by replacing scan & index w/ BRB view.
if os.getenv("BRB") == "BRB":
    urlpatterns += [
        path(r"", views.BRB.as_view(), name="root"),
    ]

# Non-branded utility routes
urlpatterns += [
    path(
        "chat/",
        sensitive_post_parameters()(views.chat_interface_view),
        name="chat-alt",
    ),
    path(
        "chat-consent",
        sensitive_post_parameters()(views.ChatUserConsentView.as_view()),
        name="chat_consent",
    ),
    path(
        "chooser/",
        views.ChooserView.as_view(),
        name="chooser",
    ),
    path("cookies/", include("cookie_consent.urls")),
    # Microsites - condition-specific landing pages
    path(
        "microsite/<slug:slug>/",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.MicrositeView.as_view())
        ),
        name="microsite",
    ),
    # Microsite directory - lists all microsites for organic crawlers
    path(
        "treatments/",
        cache_control(public=True)(
            cache_page(60 * 60 * 24)(views.MicrositeDirectoryView.as_view())
        ),
        name="microsite_directory",
    ),
]

# Define patterns that should be available under branded paths
# These will be automatically duplicated for each brand (e.g., /appealmyclaims/)
brandable_patterns = [
    path(
        "",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.IndexView.as_view())),
        name="root",
    ),
    path(
        "scan",
        sensitive_post_parameters("email")(views.InitialProcessView.as_view()),
        name="scan",
    ),
    path(
        "chat",
        sensitive_post_parameters()(views.chat_interface_view),
        name="chat",
    ),
    path(
        "explain-denial",
        views.ExplainDenialView.as_view(),
        name="explain_denial",
    ),
    path(
        "categorize",
        views.EntityExtractView.as_view(),
        name="categorize",
    ),
    path(
        "find_next_steps",
        sensitive_post_parameters("email")(views.FindNextSteps.as_view()),
        name="find_next_steps",
    ),
    path(
        "generate_appeal",
        sensitive_post_parameters("email")(views.GenerateAppeal.as_view()),
        name="generate_appeal",
    ),
    path(
        "choose_appeal",
        sensitive_post_parameters("email")(views.ChooseAppeal.as_view()),
        name="choose_appeal",
    ),
    path(
        "about-us",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.AboutView.as_view())),
        name="about",
    ),
    path(
        "contact",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.ContactView.as_view())
        ),
        name="contact",
    ),
    path(
        "privacy_policy",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.PrivacyPolicyView.as_view())
        ),
        name="privacy_policy",
    ),
    path(
        "tos",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.TermsOfServiceView.as_view())
        ),
        name="tos",
    ),
]

# Add brandable patterns to main site (FHI) - unless BRB mode overrides root
if os.getenv("BRB") != "BRB":
    # Add all brandable patterns including root
    urlpatterns += brandable_patterns
else:
    # BRB mode is active - add all brandable patterns EXCEPT root (which is handled above)
    urlpatterns += [p for p in brandable_patterns if p.name != "root"]

# AppealMyClaims path-based brand access
# AMC uses a single-page flow with upload form on landing page (not FHI's multi-page flow)
urlpatterns += [
    path(
        "appealmyclaims/",
        sensitive_post_parameters("email")(views.AMCIndexView.as_view()),
        name="amc_root",
    )
]
# Add other AMC-branded URLs (scan, about, privacy, etc.) with /appealmyclaims/ prefix
# Note: amc_root is handled above, so filter it out to avoid duplication
urlpatterns += add_brand_patterns(
    [p for p in brandable_patterns if p.name != "root"], "appealmyclaims/", "amc_"
)

urlpatterns += staticfiles_urlpatterns()

# Serve static files in development
if settings.DEBUG:
    # Serve files from STATICFILES_DIRS in development
    if settings.STATIC_URL:
        urlpatterns += static(
            settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0]
        )
    if settings.MEDIA_URL:
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

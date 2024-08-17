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

from django.contrib import admin
from django.urls import path
from django.views.decorators.cache import cache_control, cache_page

from fighthealthinsurance import views

urlpatterns = [
    path("timbit/admin/", admin.site.urls),
    path("scan", views.ProcessView.as_view(), name="scan"),
    path("server_side_ocr", views.OCRView.as_view(), name="server_side_ocr"),
    path(
        "",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.IndexView.as_view())),
        name="root",
    ),
    path(
        "about-us",
        cache_control(public=True)(cache_page(60 * 60 * 2)(views.AboutView.as_view())),
        name="about",
    ),
    path("other-resources", views.OtherResourcesView.as_view(), name="other-resources"),
    path("process", views.ProcessView.as_view(), name="process"),
    path("privacy_policy", views.PrivacyPolicyView.as_view(), name="privacy_policy"),
    path("share_denial", views.ShareDenialView.as_view(), name="share_denial"),
    path("share_appeal", views.ShareAppealView.as_view(), name="share_appeal"),
    path("remove_data", views.RemoveDataView.as_view(), name="remove_data"),
    path(
        "tos",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.TermsOfServiceView.as_view())
        ),
        name="tos",
    ),
    path("find_next_steps", views.FindNextSteps.as_view(), name="find_next_steps"),
    path("generate_appeal", views.GenerateAppeal.as_view(), name="generate_appeal"),
    path(
        "appeals_json_backend",
        views.AppealsBackend.as_view(),
        name="appeals_json_backend",
    ),
    path("choose_appeal", views.ChooseAppeal.as_view(), name="choose_appeal"),
    path(
        "contact",
        cache_control(public=True)(
            cache_page(60 * 60 * 2)(views.ContactView.as_view())
        ),
        name="contact",
    ),
]

import typing

from django.urls import include
from django.urls import path
from django.conf import settings

from api import views
from api.views import AppealViewSet, MailingListSubscriberViewSet

from rest_framework import routers

if settings.DEBUG:
    RouterClass: typing.Type[routers.BaseRouter] = routers.DefaultRouter
else:
    RouterClass = routers.SimpleRouter

router = RouterClass()

router.register(r"denials", views.DenialViewSet, basename="denials")
router.register(r"next_steps", views.NextStepsViewSet, basename="nextsteps")
router.register(r"follow_up", views.FollowUpViewSet, basename="followups")
router.register(r"qaresponse", views.QAResponseViewSet, basename="qacontext")
router.register(
    r"healthhistory", views.HealthHistoryViewSet, basename="healthhistory"
)


router.register(
    r"data_removal",
    views.DataRemovalViewSet,
    basename="dataremoval",
)

router.register(r"appeals", AppealViewSet, basename="appeals")
router.register(
    r"appeal_attachments",
    views.AppealAttachmentViewSet,
    basename="appeal_attachments",
)
router.register(
    r"mailinglist_subscribe", MailingListSubscriberViewSet, basename="subscribe"
)

urlpatterns = [
    # Non-viewset but still rest API endpoints.
    path("ping", views.Ping.as_view(), name="ping"),
    path("check_storage", views.CheckStorage.as_view(), name="check_storage"),
    path(
        "check_ml_backend", views.CheckMlBackend.as_view(), name="check_ml_backend"
    ),
    # Router
    path("", include(router.urls)),
]

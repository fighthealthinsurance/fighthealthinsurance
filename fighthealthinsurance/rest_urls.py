import typing

from django.urls import include
from django.urls import path
from django.conf import settings

from fighthealthinsurance import rest_views

from rest_framework import routers

if settings.DEBUG:
    RouterClass: typing.Type[routers.BaseRouter] = routers.DefaultRouter
else:
    RouterClass = routers.SimpleRouter

router = RouterClass()

router.register(r"denials", rest_views.DenialViewSet, basename="denials")
router.register(r"next_steps", rest_views.NextStepsViewSet, basename="nextsteps")
router.register(r"follow_up", rest_views.FollowUpViewSet, basename="followups")
router.register(r"qaresponse", rest_views.QAResponseViewSet, basename="qacontext")
router.register(
    r"healthhistory", rest_views.HealthHistoryViewSet, basename="healthhistory"
)

router.register(r"chats", rest_views.ChatViewSet, basename="chats")


router.register(
    r"data_removal",
    rest_views.DataRemovalViewSet,
    basename="dataremoval",
)

router.register(r"appeals", rest_views.AppealViewSet, basename="appeals")
router.register(
    r"appeal_attachments",
    rest_views.AppealAttachmentViewSet,
    basename="appeal_attachments",
)
router.register(
    r"mailinglist_subscribe",
    rest_views.MailingListSubscriberViewSet,
    basename="subscribe",
)
router.register(r"prior-auth", rest_views.PriorAuthViewSet, basename="prior-auth")
router.register(
    r"prior-auth/(?P<prior_auth_id>[^/.]+)/proposals",
    rest_views.ProposedPriorAuthViewSet,
    basename="prior-auth-proposals",
)

urlpatterns = [
    # Non-viewset but still rest API endpoints.
    path("ping", rest_views.Ping.as_view(), name="ping"),
    path("check_storage", rest_views.CheckStorage.as_view(), name="check_storage"),
    path(
        "check_ml_backend", rest_views.CheckMlBackend.as_view(), name="check_ml_backend"
    ),
    # Router
    path("", include(router.urls)),
]

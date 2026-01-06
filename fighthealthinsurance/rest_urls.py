import typing

from django.conf import settings
from django.urls import include, path

from rest_framework import routers

from fighthealthinsurance import chat_lead_views, rest_views

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
router.register(r"chat-leads", chat_lead_views.ChatLeadsViewSet, basename="chat-leads")


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
router.register(
    r"demo_request",
    rest_views.DemoRequestsViewSet,
    basename="demorequest",
)
router.register(r"prior-auth", rest_views.PriorAuthViewSet, basename="prior-auth")
router.register(
    r"prior-auth/(?P<prior_auth_id>[^/.]+)/proposals",
    rest_views.ProposedPriorAuthViewSet,
    basename="prior-auth-proposals",
)
router.register(r"chooser", rest_views.ChooserViewSet, basename="chooser")

urlpatterns = [
    # Non-viewset but still rest API endpoints.
    path("ping", rest_views.Ping.as_view(), name="ping"),
    path("check_storage", rest_views.CheckStorage.as_view(), name="check_storage"),
    path(
        "check_ml_backend", rest_views.CheckMlBackend.as_view(), name="check_ml_backend"
    ),
    path(
        "live_models_status",
        rest_views.LiveModelsStatus.as_view(),
        name="live_models_status",
    ),
    path(
        "actor_health_status",
        rest_views.ActorHealthStatus.as_view(),
        name="actor_health_status",
    ),
    # Router
    path("", include(router.urls)),
]

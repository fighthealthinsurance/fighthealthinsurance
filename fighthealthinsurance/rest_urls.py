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

router.register(
    r"data_removal",
    rest_views.DataRemovalViewSet,
    basename="dataremoval",
)

urlpatterns = [
    path("ping", rest_views.Ping.as_view(), name="ping"),
    path("check_storage", rest_views.CheckStorage.as_view(), name="check_storage"),
    path(
        "check_ml_backend", rest_views.CheckMlBackend.as_view(), name="check_ml_backend"
    ),
    path(
        "appeals_json_backend",
        rest_views.AppealsBackend.as_view(),
        name="api_appeals_json_backend",
    ),
    path(
        "v0/api_streamingentity_json_backend",
        rest_views.StreamingEntityBackend.as_view(),
        name="api_streamingentity_json_backend",
    ),
    path("", include(router.urls)),
]

from django.urls import path

from fighthealthinsurance import rest_views

rest_urls = [
    path("ping", rest_views.Ping.as_view(), name="ping"),
    path("check_storage", rest_views.CheckStorage.as_view(), name="check_storage"),
    path(
        "check_ml_backend", rest_views.CheckMlBackend.as_view(), name="check_ml_backend"
    ),
    path("removedata", rest_views.RemoveData.as_view(), name="api_delete"),
    path("findnextsteps", rest_views.FindNextSteps.as_view(), name="api_findnextsteps"),
    path("followup", rest_views.FollowUpAPI.as_view(), name="api_followup"),
    path("denialcreator", rest_views.DenialCreator.as_view(), name="api_denialcreator"),
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
]

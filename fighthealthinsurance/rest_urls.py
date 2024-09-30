from django.urls import path

from fighthealthinsurance import rest_views

rest_urls = [
    path("ping", rest_views.Ping.as_view(), name="ping"),
    path("removedata", rest_views.RemoveData.as_view(), name="api_delete"),
    path("findnextsteps", rest_views.FindNextSteps.as_view(), name="api_findnextsteps"),
    path("denialcreator", rest_views.DenialCreator.as_view(), name="api_denialcreator"),
    path(
        "appeals_json_backend",
        rest_views.AppealsBackend.as_view(),
        name="api_appeals_json_backend",
    ),
]

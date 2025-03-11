from django.urls import path

from .views import (
    signups_by_day,
    users_by_day,
    sf_signups,
    pro_signups_csv,
    OlderThanTwoWeeksEmailsCSV,
    LastTwoWeeksEmailsCSV,
)

urlpatterns = [
    path("signups_by_day", signups_by_day, name="signups_by_day"),
    path("users_by_day", users_by_day, name="users_by_day"),
    path("sf_signups", sf_signups, name="sf_signups"),
    path("pro_signups_csv", pro_signups_csv, name="pro_signups_csv"),
    path(
        "emails_older_than_two_weeks",
        OlderThanTwoWeeksEmailsCSV.as_view,
        name="emails_older_than_two_weeks",
    ),
    path(
        "emails_last_two_weeks",
        LastTwoWeeksEmailsCSV.as_view,
        name="emails_last_two_weeks",
    ),
]

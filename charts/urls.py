from django.urls import path

from django.contrib.admin.views.decorators import staff_member_required

from .views import (
    signups_by_day,
    users_by_day,
    sf_signups,
    pro_signups_csv,
    pro_signups_csv_single_lines,
    OlderThanTwoWeeksEmailsCSV,
    LastTwoWeeksEmailsCSV,
    de_identified_export,
    incomplete_signups_csv,
    procedures_denied_chart,
)

urlpatterns = [
    path("signups_by_day", signups_by_day, name="signups_by_day"),
    path("users_by_day", users_by_day, name="users_by_day"),
    path("sf_signups", sf_signups, name="sf_signups"),
    path("de_identified", de_identified_export, name="de_identified_export"),
    path("pro_signups_csv", pro_signups_csv, name="pro_signups_csv"),
    path(
        "pro_signups_csv_single_lines",
        pro_signups_csv_single_lines,
        name="pro_signups_csv_single_lines",
    ),
    path(
        "emails_older_than_two_weeks",
        staff_member_required(OlderThanTwoWeeksEmailsCSV.as_view()),
        name="emails_older_than_two_weeks",
    ),
    path(
        "emails_last_two_weeks",
        staff_member_required(LastTwoWeeksEmailsCSV.as_view()),
        name="emails_last_two_weeks",
    ),
    path(
        "incomplete_signups_csv", incomplete_signups_csv, name="incomplete_signups_csv"
    ),
    path(
        "procedures_denied_chart",
        procedures_denied_chart,
        name="procedures_denied_chart",
    ),
]

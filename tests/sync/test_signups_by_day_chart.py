"""The signup-per-day charts must render on the real database backend.

Both views built their query as ``.distinct("email") ... .annotate(...)`` with
a fallback for the ``NotSupportedError`` SQLite raises for ``distinct(*fields)``.
Postgres supports ``distinct(*fields)`` and so never took that fallback -- it
raised ``NotImplementedError("annotate() + distinct(fields) is not
implemented.")`` from the SQL compiler instead, which nothing caught. The
staff charts 500'd on every load in production (PYTHON-DJANGO-00-J1) while
passing in a SQLite test run.

These tests exercise the shared query helper directly as well as the views, so
the assertion holds whichever backend the suite runs against.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from datetime import timedelta

from django.utils import timezone

from charts.views import _unique_signups_per_day_df
from fighthealthinsurance.models import InterestedProfessional

User = get_user_model()


class SignupsPerDayQueryTest(TestCase):
    """The query itself, independent of the rendering around it."""

    def test_counts_each_email_once_on_its_first_signup_day(self):
        """A professional who submits the form twice, on different days, is
        one signup on the first of them -- not one per day (review)."""
        monday = timezone.now().date() - timedelta(days=2)
        wednesday = timezone.now().date()
        # signup_date is auto_now_add, so create() ignores a supplied value;
        # update() is the one write that lands the day we mean.
        for day in (monday, monday, wednesday):
            row = InterestedProfessional.objects.create(
                email="one@example.com", clicked_for_paid=False
            )
            InterestedProfessional.objects.filter(pk=row.pk).update(signup_date=day)
        InterestedProfessional.objects.create(
            email="two@example.com", clicked_for_paid=True
        )

        df = _unique_signups_per_day_df()

        self.assertEqual(int(df["count"].sum()), 2)
        by_day = {
            (row.signup_date, row.clicked_for_paid): row.count
            for row in df.itertuples()
        }
        self.assertEqual(by_day, {(monday, False): 1, (wednesday, True): 1})

    def test_a_professional_who_later_clicks_paid_counts_as_paid(self):
        """Unpaid Monday, paid Wednesday: one professional, on Monday, Paid.
        Taking the first row's flag hid every conversion (review)."""
        monday = timezone.now().date() - timedelta(days=2)
        wednesday = timezone.now().date()
        for day, paid in ((monday, False), (wednesday, True)):
            row = InterestedProfessional.objects.create(
                email="converts@example.com", clicked_for_paid=paid
            )
            InterestedProfessional.objects.filter(pk=row.pk).update(signup_date=day)

        df = _unique_signups_per_day_df()

        by_day = {
            (row.signup_date, row.clicked_for_paid): row.count
            for row in df.itertuples()
        }
        self.assertEqual(by_day, {(monday, True): 1})

    def test_no_signups_yields_an_empty_frame_rather_than_raising(self):
        self.assertTrue(_unique_signups_per_day_df().empty)


class SignupsPerDayViewTest(TestCase):
    """The views that used to 500: they must answer 200 with real rows."""

    URL_NAMES = ("signups_by_day", "pro_signups_cumulative")

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="chartstaff", password="testpass123", is_staff=True
        )
        self.client.login(username="chartstaff", password="testpass123")
        InterestedProfessional.objects.create(
            email="pro@example.com",
            signup_date=timezone.now(),
            clicked_for_paid=True,
        )

    def test_charts_render_with_signup_data(self):
        for url_name in self.URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(f"charts:{url_name}"))
                self.assertEqual(response.status_code, 200)

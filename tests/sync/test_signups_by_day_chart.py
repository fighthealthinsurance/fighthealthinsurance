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
from django.utils import timezone

from charts.views import _unique_signups_per_day_df
from fighthealthinsurance.models import InterestedProfessional

User = get_user_model()


class SignupsPerDayQueryTest(TestCase):
    """The query itself, independent of the rendering around it."""

    def test_counts_each_email_once_per_day_and_paid_status(self):
        day = timezone.now()
        InterestedProfessional.objects.create(
            email="one@example.com", signup_date=day, clicked_for_paid=False
        )
        InterestedProfessional.objects.create(
            email="one@example.com", signup_date=day, clicked_for_paid=False
        )
        InterestedProfessional.objects.create(
            email="two@example.com", signup_date=day, clicked_for_paid=False
        )

        df = _unique_signups_per_day_df()

        self.assertFalse(df.empty)
        self.assertEqual(int(df["count"].sum()), 2)

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

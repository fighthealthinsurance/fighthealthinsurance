"""Tests for InterestedProfessional chart views."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.models import InterestedProfessional

User = get_user_model()


class ProSignupChartViewTestBase(TestCase):
    """Base for pro-signup chart view tests with authenticated staff user."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffuser", password="testpass123", is_staff=True
        )
        self.client.login(username="staffuser", password="testpass123")

    def _create_pro(self, email, **kwargs):
        return InterestedProfessional.objects.create(email=email, **kwargs)


class ProSignupChartStaffOnlyTest(TestCase):
    """Non-staff users should not be able to view pro-signup charts."""

    URL_NAMES = [
        "pro_signups_cumulative",
        "pro_signups_by_provider_type",
        "pro_signups_by_denial_type",
        "pro_signups_dashboard",
    ]

    def test_anonymous_blocked(self):
        for name in self.URL_NAMES:
            response = self.client.get(reverse(f"charts:{name}"))
            self.assertIn(
                response.status_code,
                [302, 403],
                f"Expected redirect/403 for anonymous user at {name}",
            )

    def test_non_staff_blocked(self):
        User.objects.create_user(
            username="regular", password="testpass123", is_staff=False
        )
        self.client.login(username="regular", password="testpass123")
        for name in self.URL_NAMES:
            response = self.client.get(reverse(f"charts:{name}"))
            self.assertIn(
                response.status_code,
                [302, 403],
                f"Expected redirect/403 for non-staff user at {name}",
            )


class ProSignupCumulativeChartTest(ProSignupChartViewTestBase):
    def test_empty_returns_no_data_message(self):
        response = self.client.get(reverse("charts:pro_signups_cumulative"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn(b"No signup data available", response.content)

    def test_renders_chart_with_data(self):
        self._create_pro("a@example.com", clicked_for_paid=True)
        self._create_pro("b@example.com", clicked_for_paid=False)
        self._create_pro("c@example.com", clicked_for_paid=True)

        response = self.client.get(reverse("charts:pro_signups_cumulative"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cumulative Pro Signups", response.content)
        self.assertIn(b"bokeh", response.content.lower())

    def test_excludes_test_emails(self):
        self._create_pro("farts@farts.com")
        self._create_pro("holden@pigscanfly.ca")
        response = self.client.get(reverse("charts:pro_signups_cumulative"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No signup data available", response.content)


class ProSignupsByProviderTypeChartTest(ProSignupChartViewTestBase):
    def test_empty_returns_no_data_message(self):
        response = self.client.get(reverse("charts:pro_signups_by_provider_type"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No pro signup data available", response.content)

    def test_groups_normalized_provider_types(self):
        self._create_pro("a@example.com", job_title_or_provider_type="Physician")
        self._create_pro("b@example.com", job_title_or_provider_type=" physician ")
        self._create_pro("c@example.com", job_title_or_provider_type="PHYSICIAN")
        self._create_pro("d@example.com", job_title_or_provider_type="Nurse")

        response = self.client.get(reverse("charts:pro_signups_by_provider_type"))
        self.assertEqual(response.status_code, 200)
        # All three "physician" entries should be merged into one row
        content = response.content.decode()
        self.assertIn("physician", content)
        self.assertIn("nurse", content)
        # 4 unique signups total
        self.assertIn("Total unique pro signups: 4", content)
        # 2 distinct provider types after normalization
        self.assertIn("Distinct provider type values: 2", content)

    def test_blank_provider_type_bucketed_as_not_specified(self):
        self._create_pro("a@example.com", job_title_or_provider_type="")
        self._create_pro("b@example.com", job_title_or_provider_type="Nurse")

        response = self.client.get(reverse("charts:pro_signups_by_provider_type"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"(not specified)", response.content)

    def test_dedupes_by_email(self):
        self._create_pro("dup@example.com", job_title_or_provider_type="Nurse")
        self._create_pro("dup@example.com", job_title_or_provider_type="Nurse")

        response = self.client.get(reverse("charts:pro_signups_by_provider_type"))
        self.assertIn(b"Total unique pro signups: 1", response.content)


class ProSignupsByDenialTypeChartTest(ProSignupChartViewTestBase):
    def test_empty_returns_no_data_message(self):
        response = self.client.get(reverse("charts:pro_signups_by_denial_type"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No pro signup data available", response.content)

    def test_groups_normalized_denials(self):
        self._create_pro("a@example.com", most_common_denial="Prior Auth")
        self._create_pro("b@example.com", most_common_denial="prior auth")
        self._create_pro("c@example.com", most_common_denial="Medical Necessity")

        response = self.client.get(reverse("charts:pro_signups_by_denial_type"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Total unique pro signups: 3", content)
        self.assertIn("Distinct most common denial values: 2", content)


class ProSignupsDashboardTest(ProSignupChartViewTestBase):
    def test_dashboard_renders_with_summary(self):
        self._create_pro("a@example.com", clicked_for_paid=True, paid=True)
        self._create_pro("b@example.com", clicked_for_paid=True, paid=False)
        self._create_pro("c@example.com", clicked_for_paid=False, paid=False)
        # Test email - should be excluded
        self._create_pro("farts@farts.com", paid=True)

        response = self.client.get(reverse("charts:pro_signups_dashboard"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Pro Signups Dashboard", content)
        # 3 signups (test email excluded)
        self.assertIn("3", content)
        # Links to all chart views
        self.assertIn(reverse("charts:signups_by_day"), content)
        self.assertIn(reverse("charts:pro_signups_cumulative"), content)
        self.assertIn(reverse("charts:pro_signups_by_provider_type"), content)
        self.assertIn(reverse("charts:pro_signups_by_denial_type"), content)
        self.assertIn(reverse("charts:pro_signups_csv"), content)

    def test_dashboard_handles_empty(self):
        response = self.client.get(reverse("charts:pro_signups_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pro Signups Dashboard", response.content)

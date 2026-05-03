"""Tests for the signup_abandonment_chart staff view."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fhi_users.models import (
    ProfessionalDomainRelation,
    ProfessionalUser,
    UserDomain,
)

User = get_user_model()


def _make_pro_with_domain(
    *,
    username,
    email,
    state,
    city,
    visible_phone_number,
    professional_active=False,
    domain_active=False,
    pending_domain_relation=True,
):
    """Create a ProfessionalUser + UserDomain + ProfessionalDomainRelation tuple."""
    auth_user = User.objects.create_user(
        username=username, password="testpass123", email=email
    )
    professional = ProfessionalUser.objects.create(
        user=auth_user, active=professional_active
    )
    domain = UserDomain.objects.create(
        name=username + "_domain",
        visible_phone_number=visible_phone_number,
        active=domain_active,
        display_name=f"{username} Practice",
        country="USA",
        state=state,
        city=city,
        address1="123 Test St",
        zipcode="12345",
    )
    ProfessionalDomainRelation.objects.create(
        professional=professional,
        domain=domain,
        pending_domain_relation=pending_domain_relation,
    )
    return professional, domain


class SignupAbandonmentChartStaffOnlyTest(TestCase):
    def test_requires_staff(self):
        regular = User.objects.create_user(
            username="regular", password="testpass123", is_staff=False
        )
        self.client.login(username="regular", password="testpass123")
        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertIn(response.status_code, [302, 403])

    def test_anonymous_redirected(self):
        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertIn(response.status_code, [302, 403])


class SignupAbandonmentChartContentTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="testpass123", is_staff=True
        )
        self.client.login(username="staff", password="testpass123")

    def test_empty_returns_no_data_message(self):
        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No abandoned signup data available", response.content)

    def test_renders_chart_with_abandoned_signups(self):
        _make_pro_with_domain(
            username="aban1",
            email="a@example.com",
            state="CA",
            city="Oakland",
            visible_phone_number="1110000001",
        )
        _make_pro_with_domain(
            username="aban2",
            email="b@example.com",
            state="CA",
            city="Oakland",
            visible_phone_number="1110000002",
        )
        _make_pro_with_domain(
            username="aban3",
            email="c@example.com",
            state="NY",
            city="Brooklyn",
            visible_phone_number="1110000003",
        )

        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        # Total + per-state counts should appear in the rendered output.
        self.assertIn("Total abandoned signups: 3", body)
        self.assertIn("Unique states: 2", body)
        self.assertIn("CA", body)
        self.assertIn("NY", body)
        self.assertIn("Oakland", body)
        self.assertIn("Brooklyn", body)
        # Bokeh script tag should be rendered.
        self.assertIn("Signup Abandonment Analysis", body)

    def test_excludes_active_signups(self):
        # Active professional + active domain - should be excluded.
        _make_pro_with_domain(
            username="active1",
            email="active@example.com",
            state="CA",
            city="San Francisco",
            visible_phone_number="2220000001",
            professional_active=True,
            domain_active=True,
            pending_domain_relation=False,
        )
        # Active professional but inactive domain - excluded (only both inactive count).
        _make_pro_with_domain(
            username="active2",
            email="halfactive@example.com",
            state="CA",
            city="San Jose",
            visible_phone_number="2220000002",
            professional_active=True,
            domain_active=False,
        )
        # Truly abandoned.
        _make_pro_with_domain(
            username="aban_only",
            email="aban@example.com",
            state="TX",
            city="Austin",
            visible_phone_number="2220000003",
        )

        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Total abandoned signups: 1", body)
        self.assertIn("Austin", body)
        self.assertNotIn("San Francisco", body)
        self.assertNotIn("San Jose", body)

    def test_excludes_test_emails(self):
        _make_pro_with_domain(
            username="testuser1",
            email="farts@farts.com",
            state="CA",
            city="Berkeley",
            visible_phone_number="3330000001",
        )
        _make_pro_with_domain(
            username="testuser2",
            email="holden@pigscanfly.ca",
            state="CA",
            city="Berkeley",
            visible_phone_number="3330000002",
        )

        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertEqual(response.status_code, 200)
        # All abandoned signups are test emails - should report empty.
        self.assertIn(b"No abandoned signup data available", response.content)

    def test_handles_missing_state_and_city(self):
        auth_user = User.objects.create_user(
            username="nocoords", password="testpass123", email="d@example.com"
        )
        professional = ProfessionalUser.objects.create(user=auth_user, active=False)
        domain = UserDomain.objects.create(
            name="nocoords_domain",
            visible_phone_number="4440000001",
            active=False,
            display_name="No Coords Practice",
            country="USA",
            zipcode="00000",
        )
        ProfessionalDomainRelation.objects.create(
            professional=professional,
            domain=domain,
            pending_domain_relation=True,
        )

        response = self.client.get(reverse("charts:signup_abandonment_chart"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Total abandoned signups: 1", body)
        self.assertIn("Unknown", body)

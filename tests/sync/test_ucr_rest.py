"""Tests for the UCR REST endpoints (UCR plan §7).

- POST /denial/{id}/set_billing_info/ updates Denial billing fields and
  returns a pending status.
- GET /denial/{id}/ucr_context/ returns pending until enrichment runs, then
  returns the comparison.
- POST /denial/{id}/refresh_ucr/ marks ucr_refreshed_at NULL and dispatches.
- GET /ucr/lookup/ returns rates from cache or DB; works unauthenticated.
"""

import datetime
import typing
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    Denial,
    ExtraUserProperties,
    ProfessionalUser,
    UCRGeographicArea,
    UCRRate,
    UserDomain,
)
from fighthealthinsurance.ucr_constants import (
    UCR_CONTEXT_STATUS_KEY,
    UCR_CONTEXT_STATUS_PENDING,
    UCR_CONTEXT_STATUS_READY,
    UCRAreaKind,
    UCRSource,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def _make_pro_user(domain: UserDomain) -> tuple["User", str, str]:
    username = f"testuser🐼{domain.id}"
    password = "testpass"
    user = User.objects.create_user(
        username=username, password=password, email="pro@example.com"
    )
    user.is_active = True
    user.save()
    ProfessionalUser.objects.create(user=user, active=True, npi_number="1234567890")
    ExtraUserProperties.objects.create(user=user, email_verified=True)
    return user, username, password


class _UCRRestBase(APITestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="ucrdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="UCR Test Domain",
            business_name="UCR Biz",
            country="USA",
            state="CA",
            city="SF",
            address1="123 Test St",
            zipcode="94110",
        )
        self.user, self.username, self.password = _make_pro_user(self.domain)
        self.client.login(username=self.username, password=self.password)

        professional = ProfessionalUser.objects.get(user=self.user)
        self.denial = Denial.objects.create(
            hashed_email="hash:ucrtest",
            creating_professional=professional,
            primary_professional=professional,
        )

        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        for percentile, amount in [(50, 14763), (80, 19684), (90, 24605)]:
            UCRRate.objects.create(
                procedure_code="99213",
                geographic_area=self.area,
                percentile=percentile,
                amount_cents=amount,
                source=UCRSource.MEDICARE_PFS,
                effective_date=datetime.date(2026, 1, 1),
            )


class SetBillingInfoTests(_UCRRestBase):
    def test_writes_fields_and_dispatches(self):
        with patch("fighthealthinsurance.rest_views._dispatch_ucr_refresh") as dispatch:
            response = self.client.post(
                reverse("denials-set-billing-info"),
                data={
                    "denial_id": self.denial.denial_id,
                    "service_zip": "94110",
                    "procedure_code": "99213",
                    "billed_amount_cents": 25000,
                    "allowed_amount_cents": 8000,
                    "paid_amount_cents": 6400,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            response.data[UCR_CONTEXT_STATUS_KEY], UCR_CONTEXT_STATUS_PENDING
        )
        dispatch.assert_called_once_with(self.denial.pk)

        self.denial.refresh_from_db()
        self.assertEqual(self.denial.service_zip, "94110")
        self.assertEqual(self.denial.procedure_code, "99213")
        self.assertEqual(self.denial.get_billed_cents(), 25000)
        self.assertEqual(self.denial.get_allowed_cents(), 8000)
        self.assertEqual(self.denial.get_paid_cents(), 6400)
        self.assertIsNone(self.denial.ucr_refreshed_at)

    def test_clears_fields_when_empty_or_null_provided(self):
        """Locks in the key-presence semantics: explicit "" / null clears."""
        # First fill in everything.
        self.denial.service_zip = "94110"
        self.denial.procedure_code = "99213"
        self.denial.set_billed_cents(25000)
        self.denial.set_allowed_cents(8000)
        self.denial.save()

        with patch("fighthealthinsurance.rest_views._dispatch_ucr_refresh"):
            response = self.client.post(
                reverse("denials-set-billing-info"),
                data={
                    "denial_id": self.denial.denial_id,
                    "service_zip": "",
                    "allowed_amount_cents": None,
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        self.denial.refresh_from_db()
        self.assertEqual(self.denial.service_zip, "")
        self.assertIsNone(self.denial.get_allowed_cents())
        # Fields not present in the body retain their prior values.
        self.assertEqual(self.denial.procedure_code, "99213")
        self.assertEqual(self.denial.get_billed_cents(), 25000)

    def test_rejects_unowned_denial(self):
        other_user = User.objects.create_user(
            username="other_pro🐼", password="x", email="other@example.com"
        )
        other_user.is_active = True
        other_user.save()
        ProfessionalUser.objects.create(
            user=other_user, active=True, npi_number="9999999999"
        )
        ExtraUserProperties.objects.create(user=other_user, email_verified=True)
        self.client.logout()
        self.client.login(username="other_pro🐼", password="x")

        response = self.client.post(
            reverse("denials-set-billing-info"),
            data={
                "denial_id": self.denial.denial_id,
                "billed_amount_cents": 25000,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class UCRContextTests(_UCRRestBase):
    def test_pending_when_never_enriched(self):
        response = self.client.get(
            reverse("denials-ucr-context", args=[self.denial.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data[UCR_CONTEXT_STATUS_KEY], UCR_CONTEXT_STATUS_PENDING
        )

    def test_returns_comparison_when_ready(self):
        from fighthealthinsurance.ucr_helper import UCREnrichmentHelper

        self.denial.service_zip = "94110"
        self.denial.procedure_code = "99213"
        self.denial.set_billed_cents(25000)
        self.denial.set_allowed_cents(8000)
        self.denial.set_paid_cents(6400)
        self.denial.save()
        UCREnrichmentHelper.maybe_enrich(self.denial)

        response = self.client.get(
            reverse("denials-ucr-context", args=[self.denial.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data[UCR_CONTEXT_STATUS_KEY], UCR_CONTEXT_STATUS_READY
        )
        self.assertEqual(response.data["procedure_code"], "99213")
        self.assertEqual(response.data["allowed_cents"], 8000)
        self.assertIsNotNone(response.data["refreshed_at"])


class RefreshUCRTests(_UCRRestBase):
    def test_dispatches_and_marks_stale(self):
        from fighthealthinsurance.ucr_helper import UCREnrichmentHelper

        self.denial.service_zip = "94110"
        self.denial.procedure_code = "99213"
        self.denial.set_allowed_cents(8000)
        self.denial.save()
        UCREnrichmentHelper.maybe_enrich(self.denial)
        self.denial.refresh_from_db()
        self.assertIsNotNone(self.denial.ucr_refreshed_at)

        with patch("fighthealthinsurance.rest_views._dispatch_ucr_refresh") as dispatch:
            response = self.client.post(
                reverse("denials-refresh-ucr"),
                data={"denial_id": self.denial.denial_id},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        dispatch.assert_called_once_with(self.denial.pk)

        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.ucr_refreshed_at)


class UCRPublicLookupTests(APITestCase):
    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        for percentile, amount in [(50, 14763), (80, 19684), (90, 24605)]:
            UCRRate.objects.create(
                procedure_code="99213",
                geographic_area=self.area,
                percentile=percentile,
                amount_cents=amount,
                source=UCRSource.MEDICARE_PFS,
                effective_date=datetime.date(2026, 1, 1),
            )

    def test_unauthenticated_lookup_returns_rates(self):
        url = reverse("ucr_public_lookup")
        response = self.client.get(url, {"cpt": "99213", "zip": "94110"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["cpt"], "99213")
        self.assertEqual(response.data["area_kind"], UCRAreaKind.ZIP3)
        self.assertEqual(response.data["area_code"], "941")
        amounts = sorted(r["amount_cents"] for r in response.data["rates"])
        self.assertEqual(amounts, [14763, 19684, 24605])

    def test_returns_empty_when_no_data_for_zip(self):
        response = self.client.get(
            reverse("ucr_public_lookup"), {"cpt": "99213", "zip": "10001"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # No ZIP3=100 area, no national area set up — empty rate list.
        self.assertEqual(response.data["rates"], [])

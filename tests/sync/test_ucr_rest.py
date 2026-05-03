"""Tests for the UCR public lookup REST endpoint.

The per-denial UCR REST endpoints (set_billing_info / ucr_context /
refresh_ucr) were dropped along with the billing-info entry UI flow; the
public CPT/ZIP lookup is the only UCR endpoint that remains.
"""

import datetime

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import UCRGeographicArea, UCRRate
from fighthealthinsurance.ucr_constants import UCRAreaKind, UCRSource


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

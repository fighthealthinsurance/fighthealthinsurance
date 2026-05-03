"""Tests for UCR data models.

- UCRRate uniqueness on the documented composite key.
- UCRGeographicArea uniqueness on (kind, code).
- latest_ucr_lookup FK behaves as a nullable SET_NULL pointer and rejects
  cross-denial assignments.
"""

import datetime

from django.db import IntegrityError, transaction
from django.test import TestCase

from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRLookup,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import UCRAreaKind, UCRSource


class UCRGeographicAreaTests(TestCase):
    def test_unique_kind_code(self):
        UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        with self.assertRaises(IntegrityError), transaction.atomic():
            UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")

    def test_same_code_across_kinds_is_allowed(self):
        UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="CA")
        UCRGeographicArea.objects.create(kind=UCRAreaKind.STATE, code="CA")


class UCRRateTests(TestCase):
    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")

    def test_unique_composite_key(self):
        UCRRate.objects.create(
            procedure_code="99213",
            modifier="",
            geographic_area=self.area,
            percentile=80,
            amount_cents=19684,
            source=UCRSource.MEDICARE_PFS,
            effective_date=datetime.date(2026, 1, 1),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            UCRRate.objects.create(
                procedure_code="99213",
                modifier="",
                geographic_area=self.area,
                percentile=80,
                amount_cents=20000,
                source=UCRSource.MEDICARE_PFS,
                effective_date=datetime.date(2026, 1, 1),
            )

    def test_different_percentile_is_distinct(self):
        UCRRate.objects.create(
            procedure_code="99213",
            geographic_area=self.area,
            percentile=80,
            amount_cents=19684,
            source=UCRSource.MEDICARE_PFS,
            effective_date=datetime.date(2026, 1, 1),
        )
        UCRRate.objects.create(
            procedure_code="99213",
            geographic_area=self.area,
            percentile=90,
            amount_cents=24605,
            source=UCRSource.MEDICARE_PFS,
            effective_date=datetime.date(2026, 1, 1),
        )
        self.assertEqual(UCRRate.objects.count(), 2)


class DenialLatestUCRLookupTests(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(hashed_email="hash:test@example.com")
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")

    def _make_lookup(self) -> UCRLookup:
        return UCRLookup.objects.create(
            denial=self.denial,
            procedure_code="99213",
            matched_area=self.area,
            rates_snapshot=[],
        )

    def test_set_null_on_lookup_delete(self):
        lookup = self._make_lookup()
        self.denial.latest_ucr_lookup = lookup
        self.denial.save(update_fields=["latest_ucr_lookup"])

        lookup.delete()
        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.latest_ucr_lookup)

    def test_lookup_cascades_when_denial_deleted(self):
        lookup = self._make_lookup()
        denial_id = self.denial.pk
        self.denial.delete()
        self.assertFalse(UCRLookup.objects.filter(pk=lookup.pk).exists())
        self.assertFalse(Denial.objects.filter(pk=denial_id).exists())

    def test_save_rejects_lookup_owned_by_other_denial(self):
        # latest_ucr_lookup must point at a snapshot whose denial FK matches.
        other_denial = Denial.objects.create(hashed_email="hash:other@example.com")
        foreign_lookup = UCRLookup.objects.create(
            denial=other_denial,
            procedure_code="99213",
            matched_area=self.area,
            rates_snapshot=[],
        )
        self.denial.latest_ucr_lookup = foreign_lookup
        with self.assertRaises(ValueError):
            self.denial.save(update_fields=["latest_ucr_lookup"])

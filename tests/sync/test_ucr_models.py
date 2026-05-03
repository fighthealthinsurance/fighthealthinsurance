"""Tests for UCR data models and Denial encryption round-trip.

Covers UCR-OON-Reimbursement-Plan.md §11 model-level cases:
- UCRRate uniqueness on the documented composite key.
- UCRGeographicArea uniqueness on (kind, code).
- Encrypted billing-amount round-trip on Denial: int in -> int out, and the
  underlying database column is not plaintext-readable.
- latest_ucr_lookup FK behaves as a nullable SET_NULL pointer.
"""

import datetime

from django.db import IntegrityError, connection, transaction
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


class DenialEncryptedBillingTests(TestCase):
    def test_round_trip_via_helpers(self):
        denial = Denial.objects.create(hashed_email="hash:test@example.com")
        denial.set_billed_cents(25000)
        denial.set_allowed_cents(8000)
        denial.set_paid_cents(6400)
        denial.save()

        denial.refresh_from_db()
        self.assertEqual(denial.get_billed_cents(), 25000)
        self.assertEqual(denial.get_allowed_cents(), 8000)
        self.assertEqual(denial.get_paid_cents(), 6400)

    def test_unset_amount_reads_as_none(self):
        denial = Denial.objects.create(hashed_email="hash:test@example.com")
        self.assertIsNone(denial.get_billed_cents())
        self.assertIsNone(denial.get_allowed_cents())
        self.assertIsNone(denial.get_paid_cents())

    def test_setters_reject_negative_cents(self):
        denial = Denial.objects.create(hashed_email="hash:test@example.com")
        with self.assertRaises(ValueError):
            denial.set_billed_cents(-1)
        with self.assertRaises(ValueError):
            denial.set_allowed_cents(-100)
        with self.assertRaises(ValueError):
            denial.set_paid_cents(-9999)
        # None still acceptable (means "unset").
        denial.set_billed_cents(None)
        denial.set_allowed_cents(None)
        denial.set_paid_cents(None)

    def test_storage_is_not_plaintext(self):
        """Defence-in-depth: the raw column must not be the literal int.

        We open a cursor and read the raw bytes to confirm the on-disk value
        is not '25000'. encrypted_model_fields stores a Fernet ciphertext.
        """
        denial = Denial.objects.create(hashed_email="hash:test@example.com")
        denial.set_billed_cents(25000)
        denial.save()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT billed_amount_cents FROM fighthealthinsurance_denial "
                "WHERE denial_id = %s",
                [denial.pk],
            )
            (raw,) = cursor.fetchone()

        # Stored value must not equal the cleartext int and must not be empty.
        self.assertNotEqual(raw, "25000")
        self.assertNotEqual(raw, 25000)
        self.assertTrue(raw)


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

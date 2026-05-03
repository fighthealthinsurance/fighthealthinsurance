"""Tests for UCREnrichmentHelper.

Covers:
- Procedure-code regex extraction.
- Geographic area resolution fallback (ZIP3 -> state -> national).
- Source-priority dedup when multiple sources match.
- Gap math at p80 and p90.
- Hash-unchanged short-circuit (no UCRLookup insert when nothing changed).
- prune_lookups preserves Denial.latest_ucr_lookup regardless of age.
"""

import datetime

from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRLookup,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import (
    UCR_CONTEXT_HASH_KEY,
    UCRAreaKind,
    UCRSource,
)
from fighthealthinsurance.ucr_helper import RateRow, UCREnrichmentHelper


def _make_rate(area, *, percentile, amount_cents, source=UCRSource.MEDICARE_PFS):
    return UCRRate.objects.create(
        procedure_code="99213",
        geographic_area=area,
        percentile=percentile,
        amount_cents=amount_cents,
        source=source,
        effective_date=datetime.date(2026, 1, 1),
    )


class ProcedureCodeExtractionTests(TestCase):
    def test_explicit_procedure_code_field_wins(self):
        denial = Denial(procedure_code="99213", procedure="some text 12345")
        self.assertEqual(UCREnrichmentHelper.resolve_procedure_code(denial), "99213")

    def test_falls_back_to_freeform_procedure(self):
        denial = Denial(procedure_code="", procedure="Office visit CPT 99213")
        self.assertEqual(UCREnrichmentHelper.resolve_procedure_code(denial), "99213")

    def test_hcpcs_alpha_prefix(self):
        denial = Denial(procedure_code="", procedure="J3490 unclassified drug")
        self.assertEqual(UCREnrichmentHelper.resolve_procedure_code(denial), "J3490")

    def test_no_match_returns_empty(self):
        denial = Denial(procedure_code="", procedure="No structured code here")
        self.assertEqual(UCREnrichmentHelper.resolve_procedure_code(denial), "")


class AreaResolutionTests(TestCase):
    def test_zip3_preferred(self):
        zip3 = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        UCRGeographicArea.objects.create(kind=UCRAreaKind.STATE, code="CA")
        denial = Denial(service_zip="94110", your_state="CA")
        self.assertEqual(UCREnrichmentHelper.resolve_geographic_area(denial), zip3)

    def test_state_fallback(self):
        state = UCRGeographicArea.objects.create(kind=UCRAreaKind.STATE, code="CA")
        denial = Denial(service_zip="", your_state="ca")  # case-insensitive
        self.assertEqual(UCREnrichmentHelper.resolve_geographic_area(denial), state)

    def test_national_last_resort(self):
        natl = UCRGeographicArea.objects.create(kind=UCRAreaKind.NATIONAL, code="us")
        denial = Denial(service_zip="", your_state="")
        self.assertEqual(UCREnrichmentHelper.resolve_geographic_area(denial), natl)

    def test_no_areas_returns_none(self):
        denial = Denial(service_zip="94110", your_state="CA")
        self.assertIsNone(UCREnrichmentHelper.resolve_geographic_area(denial))


class GapMathTests(TestCase):
    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")

    def test_p80_gap(self):
        comparison = UCREnrichmentHelper.build_comparison(
            procedure_code="99213",
            area=self.area,
            billed_cents=25000,
            allowed_cents=8000,
            paid_cents=6400,
            rates=[
                RateRow(
                    percentile=80,
                    amount_cents=19684,
                    source=UCRSource.MEDICARE_PFS,
                    effective_date="2026-01-01",
                ),
                RateRow(
                    percentile=90,
                    amount_cents=24605,
                    source=UCRSource.MEDICARE_PFS,
                    effective_date="2026-01-01",
                ),
            ],
        )
        self.assertEqual(comparison.gap_p80_cents, 19684 - 8000)
        self.assertAlmostEqual(comparison.gap_p80_pct, 59.4, places=1)
        self.assertEqual(comparison.gap_p90_cents, 24605 - 8000)

    def test_no_allowed_amount_no_gap(self):
        comparison = UCREnrichmentHelper.build_comparison(
            procedure_code="99213",
            area=self.area,
            billed_cents=None,
            allowed_cents=None,
            paid_cents=None,
            rates=[
                RateRow(
                    percentile=80,
                    amount_cents=19684,
                    source=UCRSource.MEDICARE_PFS,
                    effective_date="2026-01-01",
                ),
            ],
        )
        self.assertIsNone(comparison.gap_p80_cents)
        self.assertIsNone(comparison.gap_p80_pct)


class SourcePriorityTests(TestCase):
    def test_higher_priority_source_wins_for_same_percentile(self):
        area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        _make_rate(area, percentile=80, amount_cents=19684)  # medicare_pfs
        _make_rate(
            area,
            percentile=80,
            amount_cents=21000,
            source=UCRSource.FAIR_HEALTH,
        )
        rows = UCREnrichmentHelper._lookup_rates(  # noqa: SLF001 (test access)
            "99213", area, rate_cache=None
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, UCRSource.FAIR_HEALTH)
        self.assertEqual(rows[0].amount_cents, 21000)


class MaybeEnrichEndToEndTests(TestCase):
    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        _make_rate(self.area, percentile=50, amount_cents=14763)
        _make_rate(self.area, percentile=80, amount_cents=19684)
        _make_rate(self.area, percentile=90, amount_cents=24605)
        self.denial = Denial.objects.create(
            hashed_email="hash:test",
            procedure_code="99213",
            service_zip="94110",
            your_state="CA",
        )
        self.denial.set_billed_cents(25000)
        self.denial.set_allowed_cents(8000)
        self.denial.set_paid_cents(6400)
        self.denial.save()

    def test_first_enrichment_writes_lookup_and_context(self):
        result = UCREnrichmentHelper.maybe_enrich(self.denial)
        self.assertIsNotNone(result)
        self.denial.refresh_from_db()
        self.assertEqual(UCRLookup.objects.filter(denial=self.denial).count(), 1)
        self.assertIsNotNone(self.denial.latest_ucr_lookup)
        self.assertIsNotNone(self.denial.ucr_refreshed_at)
        self.assertIn(UCR_CONTEXT_HASH_KEY, self.denial.ucr_context)

    def test_hash_unchanged_short_circuits(self):
        UCREnrichmentHelper.maybe_enrich(self.denial)
        self.denial.refresh_from_db()
        first_refreshed = self.denial.ucr_refreshed_at
        first_lookup_id = self.denial.latest_ucr_lookup_id

        # Re-run: nothing changed, so no new UCRLookup row, no context rewrite.
        UCREnrichmentHelper.maybe_enrich(self.denial)
        self.denial.refresh_from_db()
        self.assertEqual(UCRLookup.objects.filter(denial=self.denial).count(), 1)
        self.assertEqual(self.denial.latest_ucr_lookup_id, first_lookup_id)
        # ucr_refreshed_at gets bumped on every poll cycle.
        self.assertGreaterEqual(self.denial.ucr_refreshed_at, first_refreshed)

    def test_force_creates_new_lookup_even_when_hash_matches(self):
        UCREnrichmentHelper.maybe_enrich(self.denial)
        UCREnrichmentHelper.maybe_enrich(self.denial, force=True)
        self.assertEqual(UCRLookup.objects.filter(denial=self.denial).count(), 2)

    def test_finalized_denial_is_skipped(self):
        self.denial.appeal_result = "submitted"
        self.denial.save(update_fields=["appeal_result"])
        result = UCREnrichmentHelper.maybe_enrich(self.denial)
        self.assertIsNone(result)
        self.assertEqual(UCRLookup.objects.filter(denial=self.denial).count(), 0)


class PruneLookupsTests(TestCase):
    def setUp(self):
        self.area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        self.denial = Denial.objects.create(hashed_email="hash:test")

    def _make_lookup(self) -> UCRLookup:
        return UCRLookup.objects.create(
            denial=self.denial,
            procedure_code="99213",
            matched_area=self.area,
            rates_snapshot=[],
        )

    @override_settings(UCR_LOOKUP_RETENTION_PER_DENIAL=2)
    def test_trims_oldest_beyond_cap(self):
        for _ in range(5):
            self._make_lookup()
        deleted = UCREnrichmentHelper.prune_lookups(self.denial)
        self.assertEqual(deleted, 3)
        self.assertEqual(UCRLookup.objects.filter(denial=self.denial).count(), 2)

    @override_settings(UCR_LOOKUP_RETENTION_PER_DENIAL=1)
    def test_preserves_latest_ucr_lookup_even_when_old(self):
        # Create the "active" lookup first so it ends up oldest.
        active = self._make_lookup()
        self.denial.latest_ucr_lookup = active
        self.denial.save(update_fields=["latest_ucr_lookup"])

        for _ in range(3):
            self._make_lookup()  # newer rows

        UCREnrichmentHelper.prune_lookups(self.denial)
        self.assertTrue(UCRLookup.objects.filter(pk=active.pk).exists())

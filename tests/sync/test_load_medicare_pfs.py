"""Tests for the load_medicare_pfs management command.

Idempotency, multiplier-derived percentile rows, locality auto-creation,
malformed-row tolerance.
"""

import datetime
import os
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TransactionTestCase, override_settings

from fighthealthinsurance.models import UCRGeographicArea, UCRRate
from fighthealthinsurance.ucr_constants import UCRAreaKind, UCRSource

_RVU_HEADER = "hcpcs,locality,allowed_cents\n"
_LOCALITY_HEADER = "locality,description\n"


# TransactionTestCase (not TestCase): the loader bridges async->sync via
# sync_to_async, which uses a thread that holds its own DB connection. The
# resulting writes commit instead of nesting under TestCase's atomic wrapper,
# so we let TransactionTestCase truncate tables between cases.
class LoadMedicarePFSTests(TransactionTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.rvu_path = Path(self.tmpdir) / "rvu.csv"
        self.locality_path = Path(self.tmpdir) / "loc.csv"

    def tearDown(self):
        for p in (self.rvu_path, self.locality_path):
            if p.exists():
                p.unlink()
        os.rmdir(self.tmpdir)

    def _write_inputs(self, *, rvu: str, localities: str) -> None:
        self.rvu_path.write_text(_RVU_HEADER + rvu)
        self.locality_path.write_text(_LOCALITY_HEADER + localities)

    def _call(self, **kwargs) -> str:
        out = StringIO()
        call_command(
            "load_medicare_pfs",
            "--rvu-file",
            str(self.rvu_path),
            "--locality-file",
            str(self.locality_path),
            "--effective-year",
            "2026",
            stdout=out,
            **kwargs,
        )
        return out.getvalue()

    def test_creates_localities_and_rate_rows(self):
        self._write_inputs(
            rvu="99213,5,9842\n99214,5,15000\n",
            localities="5,Locality 5 (CA)\n",
        )

        self._call()

        self.assertEqual(
            UCRGeographicArea.objects.filter(
                kind=UCRAreaKind.MEDICARE_LOCALITY
            ).count(),
            1,
        )
        # Per HCPCS row: 3 derived percentile rows (50/80/90); the raw
        # Medicare-allowed amount lives in metadata, not its own row.
        self.assertEqual(UCRRate.objects.count(), 2 * 3)

    def test_derived_percentiles_use_settings_multipliers(self):
        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )

        self._call()

        p80 = UCRRate.objects.get(procedure_code="99213", percentile=80)
        # 9842 * 2.0 = 19684
        self.assertEqual(p80.amount_cents, 19684)
        self.assertEqual(p80.metadata.get("derived_from"), "medicare_pfs")
        self.assertEqual(p80.source, UCRSource.MEDICARE_PFS)
        self.assertEqual(p80.effective_date, datetime.date(2026, 1, 1))

    def test_idempotent_no_change(self):
        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )
        self._call()
        first_count = UCRRate.objects.count()

        # Re-run with identical input.
        self._call()
        self.assertEqual(UCRRate.objects.count(), first_count)

    def test_idempotent_amount_changed(self):
        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )
        self._call()

        # Re-run with a different allowed amount; row count stable, value updated.
        self._write_inputs(
            rvu="99213,5,10000\n",
            localities="5,Locality 5 (CA)\n",
        )
        self._call()

        # Verify via the p80 derived row: 10000 * 2.0 = 20000.
        self.assertEqual(
            UCRRate.objects.filter(percentile=80, procedure_code="99213").count(),
            1,
        )
        updated = UCRRate.objects.get(percentile=80, procedure_code="99213")
        self.assertEqual(updated.amount_cents, 20000)
        self.assertEqual(updated.metadata["medicare_allowed_cents"], 10000)

    def test_skips_malformed_allowed_cents(self):
        self._write_inputs(
            rvu="99213,5,9842\n99214,5,not-a-number\n",
            localities="5,Locality 5 (CA)\n",
        )

        self._call()

        # 99213 wrote 3 derived percentile rows; 99214 was skipped entirely.
        self.assertEqual(UCRRate.objects.count(), 3)
        self.assertFalse(UCRRate.objects.filter(procedure_code="99214").exists())

    def test_skips_negative_allowed_cents(self):
        self._write_inputs(
            rvu="99213,5,9842\n99215,5,-100\n",
            localities="5,Locality 5 (CA)\n",
        )

        self._call()

        self.assertEqual(UCRRate.objects.count(), 3)
        self.assertFalse(UCRRate.objects.filter(procedure_code="99215").exists())

    def test_dry_run_writes_nothing(self):
        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )

        self._call(**{"dry_run": True})
        self.assertEqual(UCRRate.objects.count(), 0)
        self.assertEqual(UCRGeographicArea.objects.count(), 0)

    @override_settings(UCR_MEDICARE_PERCENTILE_MULTIPLIERS={50: 1.0, 80: 1.0, 90: 1.0})
    def test_multipliers_pluck_from_settings(self):
        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )
        self._call()
        p80 = UCRRate.objects.get(procedure_code="99213", percentile=80)
        self.assertEqual(p80.amount_cents, 9842)

    def test_refresh_helper_callable_from_python(self):
        """The actor calls refresh_medicare_pfs() directly — verify it works
        without going through call_command."""
        import asyncio

        from fighthealthinsurance.management.commands.load_medicare_pfs import (
            refresh_medicare_pfs,
        )

        self._write_inputs(
            rvu="99213,5,9842\n",
            localities="5,Locality 5 (CA)\n",
        )
        result = asyncio.run(
            refresh_medicare_pfs(
                rvu_file=str(self.rvu_path),
                locality_file=str(self.locality_path),
                effective_year=2026,
            )
        )
        self.assertEqual(result.localities, 1)
        self.assertEqual(result.rates, 1)
        self.assertEqual(result.written, 3)
        self.assertEqual(UCRRate.objects.count(), 3)

        # Re-running with the same inputs should be a no-op (idempotent upsert).
        rerun = asyncio.run(
            refresh_medicare_pfs(
                rvu_file=str(self.rvu_path),
                locality_file=str(self.locality_path),
                effective_year=2026,
            )
        )
        self.assertEqual(rerun.written, 0)
        self.assertEqual(rerun.skipped, 3)

    def test_refresh_helper_requires_both_sources(self):
        import asyncio

        from fighthealthinsurance.management.commands.load_medicare_pfs import (
            refresh_medicare_pfs,
        )

        with self.assertRaises(ValueError):
            asyncio.run(refresh_medicare_pfs(rvu_url="https://example.gov/rvu.csv"))

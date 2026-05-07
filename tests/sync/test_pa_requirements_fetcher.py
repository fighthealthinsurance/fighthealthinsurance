"""Tests for ``PaRequirementsFetcher``'s upsert / soft-retire contract."""

import asyncio
from datetime import date

from django.test import TestCase

from fighthealthinsurance.models import (
    InsuranceCompany,
    PayerPriorAuthRequirement,
)
from fighthealthinsurance.pa_requirements_fetcher import (
    PA_PARSERS,
    PaRequirementsFetcher,
    register_parser,
)


class _StubParser:
    """A parser whose output is overridable per test."""

    carrier_aliases = ("FixtureCo",)

    def __init__(self, rows):
        self._rows = rows

    async def parse(self):
        return self._rows


class FetcherContractTests(TestCase):
    """Verify upsert keys and soft-retire behaviour."""

    def setUp(self):
        self.company = InsuranceCompany.objects.create(
            name="FixtureCo",
            regex=r"fixture\s*co",
            negative_regex=r"$^",
        )
        # Reset registry and snapshot for each test so we never lean on
        # parsers registered by other modules at import time.
        self._saved_registry = dict(PA_PARSERS)
        PA_PARSERS.clear()

    def tearDown(self):
        PA_PARSERS.clear()
        PA_PARSERS.update(self._saved_registry)

    def _ingest(self, rows):
        register_parser("fixtureco")(lambda: _StubParser(rows))

        async def _go():
            async with PaRequirementsFetcher() as fetcher:
                return await fetcher.ingest_carrier("fixtureco")

        return asyncio.run(_go())

    def test_idempotent_upsert(self):
        rows = [
            {
                "line_of_business": "commercial",
                "cpt_hcpcs_code": "95810",
                "code_description": "Polysomnography",
                "source_document": "[ingest:fixtureco]",
            }
        ]
        self._ingest(rows)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 1)
        # Second run with identical input must not duplicate.
        self._ingest(rows)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 1)

    def test_missing_row_is_soft_retired(self):
        first = [
            {
                "line_of_business": "commercial",
                "cpt_hcpcs_code": "95810",
                "source_document": "[ingest:fixtureco]",
            },
            {
                "line_of_business": "commercial",
                "cpt_hcpcs_code": "95811",
                "source_document": "[ingest:fixtureco]",
            },
        ]
        self._ingest(first)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 2)

        # 95811 disappears from the carrier's list — should be soft-retired
        # (end_date set), not deleted, so historical denials still resolve.
        second = [first[0]]
        stats = self._ingest(second)
        retired = PayerPriorAuthRequirement.objects.filter(
            cpt_hcpcs_code="95811"
        ).get()
        self.assertEqual(retired.end_date, date.today())
        self.assertEqual(stats["retired"], 1)

    def test_manually_curated_rows_not_soft_retired(self):
        # A manually-curated row (no [ingest:...] marker in source_document)
        # must survive an ingest run that doesn't list it.
        manual = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.company,
            cpt_hcpcs_code="99999",
            source_document="hand-entered by ops",
        )
        self._ingest(
            [
                {
                    "line_of_business": "commercial",
                    "cpt_hcpcs_code": "95810",
                    "source_document": "[ingest:fixtureco]",
                }
            ]
        )
        manual.refresh_from_db()
        self.assertIsNone(manual.end_date)

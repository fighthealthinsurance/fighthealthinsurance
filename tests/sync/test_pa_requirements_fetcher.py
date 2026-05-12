"""Tests for ``PaRequirementsFetcher``'s upsert / soft-retire contract."""

import asyncio
from datetime import date
from unittest.mock import patch

from django.test import TestCase

from fighthealthinsurance.models import (
    InsuranceCompany,
    PayerPriorAuthRequirement,
)
from fighthealthinsurance.pa_requirement_parsers import ParsedPARequirement
from fighthealthinsurance.pa_requirements_fetcher import (
    AUTO_SOURCE_PREFIX,
    PaRequirementsFetcher,
)


class FetcherContractTests(TestCase):
    """Verify upsert keys + soft-retire behaviour using ``parse_company`` stubs."""

    def setUp(self):
        self.company = InsuranceCompany.objects.create(
            name="FixtureCo",
            regex=r"fixture\s*co",
            negative_regex=r"$^",
            pa_requirement_list_url="https://example.com/pa.csv",
            pa_requirement_list_url_is_parseable=True,
        )

    def _ingest(self, parsed):
        """Run one ingest cycle with ``parse_company`` stubbed to return ``parsed``."""

        async def _go():
            fetcher = PaRequirementsFetcher()

            async def fake_parse(_company):
                return parsed

            with patch.object(fetcher, "parse_company", side_effect=fake_parse):
                return await fetcher.ingest_company(self.company)

        return asyncio.run(_go())

    def test_idempotent_upsert(self):
        parsed = [
            ParsedPARequirement(
                cpt_hcpcs_code="95810",
                code_description="Polysomnography",
                line_of_business="commercial",
            )
        ]
        self._ingest(parsed)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 1)
        # Re-running with the same input must not duplicate.
        self._ingest(parsed)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 1)

    def test_missing_row_is_soft_retired(self):
        first = [
            ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="commercial"),
            ParsedPARequirement(cpt_hcpcs_code="95811", line_of_business="commercial"),
        ]
        self._ingest(first)
        self.assertEqual(PayerPriorAuthRequirement.objects.count(), 2)

        # 95811 disappears from the carrier's list — soft-retire it.
        second = [first[0]]
        stats = self._ingest(second)
        retired = PayerPriorAuthRequirement.objects.filter(cpt_hcpcs_code="95811").get()
        self.assertEqual(retired.end_date, date.today())
        self.assertEqual(stats["retired"], 1)

    def test_manually_curated_rows_not_soft_retired(self):
        # A manually-curated row (no ``auto:`` prefix on source_document)
        # must survive an ingest run that doesn't list it.
        manual = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.company,
            cpt_hcpcs_code="99999",
            source_document="hand-entered by ops",
        )
        self._ingest(
            [ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="commercial")]
        )
        manual.refresh_from_db()
        self.assertIsNone(manual.end_date)

    def test_invalid_lob_falls_back_to_all(self):
        # The parsers normalise LOB to lowercase strings; an unknown value
        # must fall back to ``LineOfBusiness.ALL`` rather than failing
        # ``full_clean()``.
        self._ingest(
            [
                ParsedPARequirement(
                    cpt_hcpcs_code="95810", line_of_business="bogus_value"
                )
            ]
        )
        row = PayerPriorAuthRequirement.objects.get(cpt_hcpcs_code="95810")
        self.assertEqual(row.line_of_business, "all")

    def test_manual_row_protects_same_key_from_overwrite(self):
        # A manual / fixture-seeded row that shares the upsert key with a
        # carrier's parsed row must NOT be overwritten by auto-ingest.
        manual = PayerPriorAuthRequirement.objects.create(
            insurance_company=self.company,
            cpt_hcpcs_code="95810",
            line_of_business="commercial",
            code_description="Manual override description",
            submission_channel="Manual channel",
            source_document="curated by ops",
        )
        self._ingest(
            [
                ParsedPARequirement(
                    cpt_hcpcs_code="95810",
                    code_description="Carrier-published description",
                    submission_channel="Carrier channel",
                    line_of_business="commercial",
                )
            ]
        )
        manual.refresh_from_db()
        self.assertEqual(manual.code_description, "Manual override description")
        self.assertEqual(manual.submission_channel, "Manual channel")
        self.assertEqual(manual.source_document, "curated by ops")
        # And no parallel auto-ingest row was created for the same key.
        auto_rows = PayerPriorAuthRequirement.objects.filter(
            cpt_hcpcs_code="95810", source_document__startswith="auto:"
        )
        self.assertEqual(auto_rows.count(), 0)

    def test_re_ingest_clears_end_date(self):
        # A row that was soft-retired in a previous run becomes current
        # again when the parser produces it on a fresh fetch.
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.company,
            cpt_hcpcs_code="95810",
            line_of_business="commercial",
            source_document=f"{AUTO_SOURCE_PREFIX}https://example.com/pa.csv",
            end_date=date.today(),
        )
        self._ingest(
            [ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="commercial")]
        )
        row = PayerPriorAuthRequirement.objects.get(cpt_hcpcs_code="95810")
        self.assertIsNone(row.end_date)


class IngestAllSelectionTests(TestCase):
    """Only companies flagged ``pa_requirement_list_url_is_parseable`` are fetched."""

    def setUp(self):
        InsuranceCompany.objects.create(
            name="Parseable Co",
            regex=r"parseable",
            negative_regex=r"$^",
            pa_requirement_list_url="https://example.com/p.csv",
            pa_requirement_list_url_is_parseable=True,
        )
        InsuranceCompany.objects.create(
            name="Unparseable Co",
            regex=r"unparseable",
            negative_regex=r"$^",
            pa_requirement_list_url="https://example.com/u.html",
            pa_requirement_list_url_is_parseable=False,
        )
        InsuranceCompany.objects.create(
            name="No URL Co",
            regex=r"nourl",
            negative_regex=r"$^",
            pa_requirement_list_url="",
            pa_requirement_list_url_is_parseable=True,
        )

    def test_only_parseable_companies_fetched(self):
        async def _go():
            fetcher = PaRequirementsFetcher()
            visited = []

            async def fake_ingest_company(company):
                visited.append(company.name)
                return {"fetched": 1, "failed": 0, "entries": 0, "retired": 0}

            with patch.object(
                fetcher, "ingest_company", side_effect=fake_ingest_company
            ):
                await fetcher.ingest_all()
            return visited

        visited = asyncio.run(_go())
        self.assertEqual(visited, ["Parseable Co"])

    def test_per_company_exception_does_not_abort_run(self):
        # A second parseable company is enough to verify the loop continues
        # past a failure.
        InsuranceCompany.objects.create(
            name="Second Parseable",
            regex=r"second",
            negative_regex=r"$^",
            pa_requirement_list_url="https://example.com/s.csv",
            pa_requirement_list_url_is_parseable=True,
        )

        async def _go():
            fetcher = PaRequirementsFetcher()
            calls = []

            async def boom_first(company):
                calls.append(company.name)
                if company.name == "Parseable Co":
                    raise RuntimeError("simulated parser crash")
                return {"fetched": 1, "failed": 0, "entries": 0, "retired": 0}

            with patch.object(fetcher, "ingest_company", side_effect=boom_first):
                stats = await fetcher.ingest_all()
            return stats, calls

        stats, calls = asyncio.run(_go())
        # Both companies were attempted.
        self.assertEqual(sorted(calls), ["Parseable Co", "Second Parseable"])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["fetched"], 1)

"""
Tests for pa_requirement_fetcher — the async layer that fetches, parses, and
persists PayerPriorAuthRequirement rows from payer-published PA lists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from django.test import TestCase

from fighthealthinsurance.models import InsuranceCompany, PayerPriorAuthRequirement
from fighthealthinsurance.pa_requirement_fetcher import (
    AUTO_SOURCE_PREFIX,
    PARequirementFetcher,
)
from fighthealthinsurance.pa_requirement_parsers import ParsedPARequirement


# ---------------------------------------------------------------------------
# Unit tests (Django TestCase for DB access)
# ---------------------------------------------------------------------------


class PARequirementFetcherReplaceTests(TestCase):
    """Test the _replace_requirements static method via direct DB manipulation."""

    def setUp(self):
        self.company = InsuranceCompany.objects.create(
            name="Test Payer Replace",
            pa_requirement_list_url="https://www.uhcprovider.com/test.html",
            pa_requirement_list_url_is_parseable=True,
        )

    def _parsed(self, code: str, **kwargs) -> ParsedPARequirement:
        defaults = dict(
            code_description="Test",
            requires_pa=True,
            notification_only=False,
            pa_category="",
            criteria_reference="",
            submission_channel="",
            line_of_business="all",
            state="",
            notes="",
            source_document="",
        )
        defaults.update(kwargs)
        return ParsedPARequirement(cpt_hcpcs_code=code, **defaults)

    def _source(self) -> str:
        return f"{AUTO_SOURCE_PREFIX}https://www.uhcprovider.com/test.html"

    def test_creates_new_rows(self):
        from asgiref.sync import async_to_sync

        parsed = [self._parsed("95810"), self._parsed("J0490")]
        count = async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, parsed, self._source()
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=self.company
            ).count(),
            2,
        )

    def test_replaces_auto_rows_on_rerun(self):
        from asgiref.sync import async_to_sync

        # First ingest
        async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, [self._parsed("95810")], self._source()
        )
        # Second ingest with different codes
        count = async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, [self._parsed("J0490"), self._parsed("27447")], self._source()
        )
        self.assertEqual(count, 2)
        codes = set(
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=self.company
            ).values_list("cpt_hcpcs_code", flat=True)
        )
        self.assertNotIn("95810", codes)
        self.assertIn("J0490", codes)
        self.assertIn("27447", codes)

    def test_manual_rows_preserved(self):
        """Rows without AUTO_SOURCE_PREFIX must not be touched on re-ingest."""
        from asgiref.sync import async_to_sync

        # Seed a manual row
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.company,
            cpt_hcpcs_code="99213",
            requires_pa=False,
            line_of_business="all",
            source_document="seed:manual",
        )

        async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, [self._parsed("95810")], self._source()
        )

        all_codes = set(
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=self.company
            ).values_list("cpt_hcpcs_code", flat=True)
        )
        self.assertIn("99213", all_codes)
        self.assertIn("95810", all_codes)

    def test_range_code_stored_as_range(self):
        from asgiref.sync import async_to_sync

        parsed = [
            ParsedPARequirement(
                code_range_start="99201",
                code_range_end="99215",
                code_description="E&M visits",
            )
        ]
        async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, parsed, self._source()
        )
        row = PayerPriorAuthRequirement.objects.filter(
            insurance_company=self.company
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.cpt_hcpcs_code, "")
        self.assertEqual(row.code_range_start, "99201")
        self.assertEqual(row.code_range_end, "99215")

    def test_invalid_lob_falls_back_to_all(self):
        from asgiref.sync import async_to_sync

        parsed = [self._parsed("95810", line_of_business="garbage_lob")]
        async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, parsed, self._source()
        )
        row = PayerPriorAuthRequirement.objects.filter(
            insurance_company=self.company
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.line_of_business, "all")

    def test_empty_code_and_no_range_skipped(self):
        from asgiref.sync import async_to_sync

        parsed = [self._parsed("")]  # empty code, no range
        count = async_to_sync(PARequirementFetcher._replace_requirements)(
            self.company, parsed, self._source()
        )
        self.assertEqual(count, 0)


class PARequirementFetcherIngestCompanyTests(TestCase):
    """Test ingest_company with mocked HTTP and parser."""

    def setUp(self):
        self.company = InsuranceCompany.objects.create(
            name="Mock Payer",
            pa_requirement_list_url="https://www.uhcprovider.com/mock-pa.html",
            pa_requirement_list_url_is_parseable=True,
        )

    def test_ingest_company_writes_requirements(self):
        """ingest_company should fetch content, call the parser, and persist rows."""
        from asgiref.sync import async_to_sync

        html_content = """
        <table>
          <thead><tr><th>CPT/HCPCS</th><th>Description</th><th>PA Required</th></tr></thead>
          <tbody>
            <tr><td>95810</td><td>Sleep study</td><td>Yes</td></tr>
            <tr><td>J0490</td><td>Belimumab</td><td>Yes</td></tr>
          </tbody>
        </table>
        """

        async def run():
            async with PARequirementFetcher() as fetcher:
                with patch.object(fetcher, "_get_content", new=AsyncMock(
                    return_value=("text/html", html_content)
                )):
                    return await fetcher.ingest_company(self.company)

        count = async_to_sync(run)()
        self.assertEqual(count, 2)
        codes = set(
            PayerPriorAuthRequirement.objects.filter(
                insurance_company=self.company
            ).values_list("cpt_hcpcs_code", flat=True)
        )
        self.assertIn("95810", codes)
        self.assertIn("J0490", codes)

    def test_ingest_company_no_url_returns_zero(self):
        from asgiref.sync import async_to_sync

        self.company.pa_requirement_list_url = ""
        self.company.save()

        async def run():
            async with PARequirementFetcher() as fetcher:
                return await fetcher.ingest_company(self.company)

        count = async_to_sync(run)()
        self.assertEqual(count, 0)

    def test_ingest_company_unknown_content_type_raises(self):
        """A Content-Type that has no registered parser must raise LookupError."""
        from asgiref.sync import async_to_sync

        async def run():
            async with PARequirementFetcher() as fetcher:
                with patch.object(fetcher, "_get_content", new=AsyncMock(
                    return_value=("application/octet-stream", b"\x00\x01")
                )):
                    return await fetcher.ingest_company(self.company)

        with self.assertRaises(LookupError):
            async_to_sync(run)()

    def test_ingest_company_pdf_content_type(self):
        """A PDF Content-Type dispatches to the generic PDF parser."""
        from asgiref.sync import async_to_sync

        self.company.pa_requirement_list_url = "https://example.com/list.pdf"
        self.company.save()

        with patch(
            "fighthealthinsurance.pa_requirement_fetcher.PARSERS_BY_CONTENT_TYPE",
            {
                "application/pdf": (
                    lambda data, name: [ParsedPARequirement(cpt_hcpcs_code="95810")],
                    True,
                )
            },
        ):
            async def run():
                async with PARequirementFetcher() as fetcher:
                    with patch.object(fetcher, "_get_content", new=AsyncMock(
                        return_value=("application/pdf", b"%PDF-1.4")
                    )):
                        return await fetcher.ingest_company(self.company)

            count = async_to_sync(run)()
            self.assertEqual(count, 1)

    def test_host_enrichment_applied(self):
        """Records returned by parse_company should pick up host-specific defaults."""
        from asgiref.sync import async_to_sync

        html = (
            "<table><thead><tr><th>CPT</th><th>PA Required</th></tr></thead>"
            "<tbody><tr><td>95810</td><td>Yes</td></tr></tbody></table>"
        )

        async def run():
            async with PARequirementFetcher() as fetcher:
                with patch.object(fetcher, "_get_content", new=AsyncMock(
                    return_value=("text/html", html)
                )):
                    return await fetcher.parse_company(self.company)

        reqs = async_to_sync(run)()
        self.assertEqual(len(reqs), 1)
        self.assertIn("UHCprovider", reqs[0].submission_channel)
        self.assertEqual(reqs[0].line_of_business, "commercial")


class PARequirementFetcherIngestAllTests(TestCase):
    """Test ingest_all: skip companies without parseable flag, count results."""

    def test_skips_unparseable_companies(self):
        from asgiref.sync import async_to_sync

        InsuranceCompany.objects.create(
            name="Unparseable Payer",
            pa_requirement_list_url="https://example.com/pa.html",
            pa_requirement_list_url_is_parseable=False,
        )

        async def run():
            async with PARequirementFetcher() as fetcher:
                return await fetcher.ingest_all()

        stats = async_to_sync(run)()
        self.assertEqual(stats["fetched"], 0)

    def test_returns_summary_stats(self):
        from asgiref.sync import async_to_sync

        InsuranceCompany.objects.create(
            name="Parseable Payer",
            pa_requirement_list_url="https://www.uhcprovider.com/mock.html",
            pa_requirement_list_url_is_parseable=True,
        )

        html = """
        <table>
          <thead><tr><th>CPT</th><th>PA Required</th></tr></thead>
          <tbody><tr><td>95810</td><td>Yes</td></tr></tbody>
        </table>
        """

        async def run():
            async with PARequirementFetcher() as fetcher:
                with patch.object(fetcher, "ingest_company", new=AsyncMock(return_value=1)):
                    return await fetcher.ingest_all()

        stats = async_to_sync(run)()
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_failed_company_counted_not_raised(self):
        from asgiref.sync import async_to_sync

        InsuranceCompany.objects.create(
            name="Failing Payer",
            pa_requirement_list_url="https://www.uhcprovider.com/failing.html",
            pa_requirement_list_url_is_parseable=True,
        )

        async def run():
            async with PARequirementFetcher() as fetcher:
                with patch.object(
                    fetcher,
                    "ingest_company",
                    new=AsyncMock(side_effect=Exception("network error")),
                ):
                    return await fetcher.ingest_all()

        stats = async_to_sync(run)()
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["fetched"], 0)

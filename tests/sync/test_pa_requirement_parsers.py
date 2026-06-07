"""
Tests for pa_requirement_parsers — the HTML/PDF/CSV/Excel parsing layer that
converts payer-published prior-authorization lists into ParsedPARequirement
records.

These tests do not touch the database; ``SimpleTestCase`` is used for test
discovery compatibility with no DB setup overhead.
"""

from django.test import SimpleTestCase as TestCase

from fighthealthinsurance.pa_requirement_parsers import (
    ParsedPARequirement,
    _bool_cell,
    _dedupe,
    _map_columns,
    _rows_to_requirements,
    _scan_text_for_codes,
    apply_enrichment,
    enrichment_for_host,
    parse_csv_pa_list,
    parse_html_pa_table,
)


class BoolCellTests(TestCase):
    def test_truthy_variants(self):
        for val in ("Yes", "YES", "yes", "Y", "y", "true", "1", "required", "x", "✓"):
            with self.subTest(val=val):
                self.assertTrue(_bool_cell(val))

    def test_falsy_variants(self):
        for val in (
            "No",
            "NO",
            "no",
            "N",
            "n",
            "false",
            "0",
            "not required",
            "excluded",
        ):
            with self.subTest(val=val):
                self.assertFalse(_bool_cell(val))

    def test_ambiguous_returns_none(self):
        self.assertIsNone(_bool_cell("maybe"))
        self.assertIsNone(_bool_cell(""))
        self.assertIsNone(_bool_cell("pending"))


class ColumnMappingTests(TestCase):
    def test_recognises_code_column(self):
        mapping = _map_columns(["CPT/HCPCS", "Description", "PA Required"])
        self.assertIn(0, mapping)
        self.assertEqual(mapping[0], "code")

    def test_recognises_all_canonical_fields(self):
        headers = [
            "Procedure Code",
            "Procedure Description",
            "Prior Authorization Required",
            "Advance Notification Required",
            "Category",
            "Criteria Reference",
            "Submission Channel",
        ]
        mapping = _map_columns(headers)
        self.assertEqual(mapping[0], "code")
        self.assertEqual(mapping[1], "description")
        self.assertEqual(mapping[2], "requires_pa")
        self.assertEqual(mapping[3], "notification_only")
        self.assertEqual(mapping[4], "category")
        self.assertEqual(mapping[5], "criteria")
        self.assertEqual(mapping[6], "submission")

    def test_case_insensitive(self):
        mapping = _map_columns(["CPT CODE", "PRIOR AUTHORIZATION REQUIRED"])
        self.assertEqual(mapping.get(0), "code")
        self.assertEqual(mapping.get(1), "requires_pa")

    def test_unrecognised_columns_not_included(self):
        mapping = _map_columns(["Claim Number", "Member ID", "Date of Service"])
        self.assertEqual(mapping, {})

    def test_collapses_multiple_whitespace_runs(self):
        # PDF-extracted headers often carry runs of 3+ spaces or interleaved
        # tabs / newlines. The normaliser must collapse them all to a single
        # space so the alias map still matches.
        mapping = _map_columns(
            [
                "Procedure   Code",  # 3-space run
                "Procedure\tDescription",  # tab
                "Prior  Authorization\n  Required",  # mixed run
            ]
        )
        self.assertEqual(mapping.get(0), "code")
        self.assertEqual(mapping.get(1), "description")
        self.assertEqual(mapping.get(2), "requires_pa")


class RowsToRequirementsTests(TestCase):
    def _make_col_map(self):
        return {
            0: "code",
            1: "description",
            2: "requires_pa",
            3: "notification_only",
            4: "category",
            5: "criteria",
            6: "submission",
        }

    def test_basic_row(self):
        col_map = self._make_col_map()
        rows = [
            [
                "95810",
                "Polysomnography",
                "Yes",
                "No",
                "Sleep Medicine",
                "CDG SL-001",
                "UHCprovider.com",
            ]
        ]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)
        req = reqs[0]
        self.assertEqual(req.cpt_hcpcs_code, "95810")
        self.assertEqual(req.code_description, "Polysomnography")
        self.assertTrue(req.requires_pa)
        self.assertFalse(req.notification_only)
        self.assertEqual(req.pa_category, "Sleep Medicine")
        self.assertEqual(req.criteria_reference, "CDG SL-001")
        self.assertEqual(req.submission_channel, "UHCprovider.com")

    def test_hcpcs_code(self):
        col_map = {0: "code", 1: "description", 2: "requires_pa"}
        rows = [["J0490", "Belimumab injection", "Yes"]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "J0490")

    def test_no_required_pa(self):
        col_map = {0: "code", 1: "description", 2: "requires_pa"}
        rows = [["99213", "Office visit E&M Level 3", "No"]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)
        self.assertFalse(reqs[0].requires_pa)

    def test_notification_only(self):
        col_map = {0: "code", 2: "requires_pa", 3: "notification_only"}
        rows = [["27447", "TKA", "Yes", "Yes"]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)
        self.assertTrue(reqs[0].notification_only)

    def test_empty_rows_skipped(self):
        col_map = {0: "code", 1: "description"}
        rows = [["", ""], ["", "   "], ["95810", "Sleep study"]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)

    def test_range_code(self):
        col_map = {0: "code", 1: "description"}
        rows = [["99201-99215", "E&M office visits", ""]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "")
        self.assertEqual(reqs[0].code_range_start, "99201")
        self.assertEqual(reqs[0].code_range_end, "99215")

    def test_no_code_column_returns_empty(self):
        col_map = {0: "description", 1: "requires_pa"}
        rows = [["Polysomnography", "Yes"]]
        reqs = _rows_to_requirements(col_map, rows)
        self.assertEqual(reqs, [])


class DedupeTests(TestCase):
    def test_removes_duplicate_code_and_lob(self):
        reqs = [
            ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="all"),
            ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="all"),
            ParsedPARequirement(cpt_hcpcs_code="95810", line_of_business="commercial"),
        ]
        deduped = _dedupe(reqs)
        self.assertEqual(len(deduped), 2)

    def test_keeps_different_states(self):
        reqs = [
            ParsedPARequirement(cpt_hcpcs_code="95810", state="CA"),
            ParsedPARequirement(cpt_hcpcs_code="95810", state="TX"),
        ]
        deduped = _dedupe(reqs)
        self.assertEqual(len(deduped), 2)


class HTMLParserTests(TestCase):
    _TABLE_HTML = """
    <html><body>
    <table>
      <thead><tr>
        <th>CPT/HCPCS</th>
        <th>Description</th>
        <th>PA Required</th>
        <th>Category</th>
      </tr></thead>
      <tbody>
        <tr><td>95810</td><td>Polysomnography</td><td>Yes</td><td>Sleep Medicine</td></tr>
        <tr><td>J0490</td><td>Belimumab injection</td><td>Yes</td><td>Specialty Drug</td></tr>
        <tr><td>99213</td><td>Office visit E/M</td><td>No</td><td>E&amp;M</td></tr>
      </tbody>
    </table>
    </body></html>
    """

    def test_extracts_rows_from_html_table(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        codes = {r.cpt_hcpcs_code for r in reqs}
        self.assertIn("95810", codes)
        self.assertIn("J0490", codes)
        self.assertIn("99213", codes)

    def test_pa_required_flag(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        by_code = {r.cpt_hcpcs_code: r for r in reqs}
        self.assertTrue(by_code["95810"].requires_pa)
        self.assertFalse(by_code["99213"].requires_pa)

    def test_empty_table_returns_empty(self):
        html = "<html><body><table></table></body></html>"
        reqs = parse_html_pa_table(html)
        self.assertEqual(reqs, [])

    def test_no_table_returns_empty(self):
        html = "<html><body><p>No tables here.</p></body></html>"
        reqs = parse_html_pa_table(html)
        self.assertEqual(reqs, [])

    def test_uhc_enrichment_adds_submission_channel(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        apply_enrichment(reqs, enrichment_for_host("www.uhcprovider.com"))
        for req in reqs:
            self.assertIn("UHCprovider", req.submission_channel)
            self.assertEqual(req.line_of_business, "commercial")

    def test_aetna_enrichment(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        apply_enrichment(reqs, enrichment_for_host("www.aetna.com"))
        for req in reqs:
            self.assertIn("Aetna", req.submission_channel)

    def test_cigna_enrichment(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        apply_enrichment(reqs, enrichment_for_host("www.cigna.com"))
        for req in reqs:
            self.assertIn("eviCore", req.submission_channel)

    def test_unknown_host_is_noop(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        before = [(r.submission_channel, r.line_of_business) for r in reqs]
        apply_enrichment(reqs, enrichment_for_host("example.com"))
        after = [(r.submission_channel, r.line_of_business) for r in reqs]
        self.assertEqual(before, after)

    def test_enrichment_does_not_overwrite_existing_channel(self):
        reqs = parse_html_pa_table(self._TABLE_HTML)
        reqs[0].submission_channel = "Custom channel"
        apply_enrichment(reqs, enrichment_for_host("www.uhcprovider.com"))
        self.assertEqual(reqs[0].submission_channel, "Custom channel")


class CSVParserTests(TestCase):
    def _csv(self, content: str) -> bytes:
        return content.encode("utf-8")

    def test_basic_csv(self):
        csv_data = self._csv(
            "Procedure Code,Description,PA Required\n"
            "95810,Polysomnography,Yes\n"
            "99213,Office E/M,No\n"
        )
        reqs = parse_csv_pa_list(csv_data)
        self.assertEqual(len(reqs), 2)
        codes = {r.cpt_hcpcs_code for r in reqs}
        self.assertIn("95810", codes)
        self.assertIn("99213", codes)

    def test_csv_no_code_column_returns_empty(self):
        csv_data = self._csv("Name,Date\nFoo,2025-01-01\n")
        reqs = parse_csv_pa_list(csv_data)
        self.assertEqual(reqs, [])

    def test_csv_bom_handling(self):
        # BOM (\xef\xbb\xbf) is present in some Windows-exported CSVs.
        csv_data = (
            b"\xef\xbb\xbfCPT,Description,PA Required\r\n95810,Sleep study,Yes\r\n"
        )
        reqs = parse_csv_pa_list(csv_data)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "95810")

    def test_csv_deduplicates(self):
        csv_data = self._csv(
            "CPT,Description,PA Required\n"
            "95810,Polysomnography,Yes\n"
            "95810,Polysomnography,Yes\n"
        )
        reqs = parse_csv_pa_list(csv_data)
        self.assertEqual(len(reqs), 1)


class ScanTextForCodesTests(TestCase):
    """Free-text fallback scanning (used for PDF pages with no detectable table).

    The scanner must only emit requirements when surrounding text carries a
    positive PA/CPT cue; otherwise incidental 5-digit numbers (ZIPs, dollar
    amounts, member/group IDs) would become false "requires prior auth" rows.
    """

    def test_bare_non_code_numbers_without_pa_context_yield_nothing(self):
        # Member/group IDs and a ZIP code all match the 5-digit _INLINE_CODE
        # regex but appear with no PA/CPT context — none should be emitted.
        text = "Member ID: 12345\n" "Group Number: 67890\n" "ZIP: 90210"
        reqs = _scan_text_for_codes(text)
        self.assertEqual(reqs, [])

    def test_dollar_amount_without_pa_context_yields_nothing(self):
        # "$10,000" extracts a bare "10000"; without PA context it must be
        # ignored rather than treated as a procedure code.
        text = "Estimated out-of-pocket maximum is $10,000 per plan year."
        reqs = _scan_text_for_codes(text)
        self.assertEqual(reqs, [])

    def test_code_with_pa_context_is_extracted(self):
        text = (
            "The following CPT codes require prior authorization:\n"
            "72148 MRI lumbar spine"
        )
        reqs = _scan_text_for_codes(text)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "72148")
        self.assertTrue(reqs[0].requires_pa)

    def test_negative_phrase_with_context_sets_requires_pa_false(self):
        text = "CPT 72148 does not require prior authorization."
        reqs = _scan_text_for_codes(text)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "72148")
        self.assertFalse(reqs[0].requires_pa)

    def test_notification_only_context_sets_flag(self):
        # Advance-notification language carries a "procedure code" cue but no
        # "authorization", so the record is flagged notification-only.
        text = (
            "Advance notification is required for the following procedure codes:\n"
            "27447 Total knee arthroplasty"
        )
        reqs = _scan_text_for_codes(text)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "27447")
        self.assertTrue(reqs[0].notification_only)
        self.assertTrue(reqs[0].requires_pa)

    def test_hcpcs_code_with_pa_context_is_extracted(self):
        text = (
            "HCPCS codes requiring prior authorization:\n" "J0490 Belimumab injection"
        )
        reqs = _scan_text_for_codes(text)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].cpt_hcpcs_code, "J0490")
        self.assertTrue(reqs[0].requires_pa)

    def test_source_document_is_propagated(self):
        text = (
            "Prior authorization is required for procedure code 72148.\n"
            "72148 MRI lumbar spine"
        )
        reqs = _scan_text_for_codes(text, source_document="payer.pdf")
        self.assertTrue(reqs)
        self.assertTrue(all(r.source_document == "payer.pdf" for r in reqs))

"""Tests for the USPSTF Prevention TaskForce API client."""

from unittest.mock import patch

from django.test import TestCase

from fighthealthinsurance.models import USPSTFRecommendation
from fighthealthinsurance.uspstf_api import (
    FALLBACK_RECOMMENDATIONS,
    _coerce_record,
    _extract_records,
    _normalize_grade,
    find_recommendations_for_codes,
    format_recommendation,
    get_uspstf_info,
    search_recommendations,
)


class NormalizeGradeTests(TestCase):
    def test_returns_empty_for_none(self):
        self.assertEqual(_normalize_grade(None), "")

    def test_returns_empty_for_unrecognized(self):
        self.assertEqual(_normalize_grade("zzz"), "")

    def test_strips_grade_prefix(self):
        self.assertEqual(_normalize_grade("Grade A"), "A")
        self.assertEqual(_normalize_grade("grade B"), "B")

    def test_handles_insufficient_evidence(self):
        self.assertEqual(_normalize_grade("I (Insufficient)"), "I")

    def test_passes_through_letter(self):
        for letter in ("A", "B", "C", "D", "I"):
            self.assertEqual(_normalize_grade(letter), letter)
            self.assertEqual(_normalize_grade(letter.lower()), letter)


class CoerceRecordTests(TestCase):
    def test_coerces_short_form(self):
        raw = {
            "id": "x1",
            "title": "Some Screening",
            "grade": "A",
            "shortDesc": "Short text.",
        }
        record = _coerce_record(raw)
        self.assertEqual(record["id"], "x1")
        self.assertEqual(record["title"], "Some Screening")
        self.assertEqual(record["grade"], "A")
        self.assertEqual(record["short_description"], "Short text.")

    def test_derives_id_from_title_when_missing(self):
        raw = {"title": "Lung Cancer: Screening", "grade": "B"}
        record = _coerce_record(raw)
        self.assertTrue(record["id"])
        self.assertNotIn(":", record["id"])
        self.assertEqual(record["grade"], "B")

    def test_handles_missing_fields(self):
        record = _coerce_record({})
        self.assertEqual(record["title"], "")
        self.assertEqual(record["grade"], "")
        self.assertEqual(record["status"], "current")


class ExtractRecordsTests(TestCase):
    def test_extracts_from_top_level_list(self):
        payload = [{"id": "a", "title": "A", "grade": "A"}]
        records = _extract_records(payload)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "a")

    def test_extracts_from_specifications_key(self):
        payload = {"specifications": [{"id": "b", "title": "B", "grade": "B"}]}
        records = _extract_records(payload)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["grade"], "B")

    def test_extracts_from_recommendations_key(self):
        payload = {"recommendations": [{"id": "c", "title": "C", "grade": "C"}]}
        records = _extract_records(payload)
        self.assertEqual(records[0]["grade"], "C")

    def test_returns_empty_for_unknown_shape(self):
        self.assertEqual(_extract_records("not a payload"), [])
        self.assertEqual(_extract_records({"unrelated": 1}), [])


class SearchRecommendationsTests(TestCase):
    """The cache should be auto-seeded from the bundled fallback dataset."""

    def test_grade_a_filter_returns_only_a(self):
        results = search_recommendations(grade="A", limit=10)
        self.assertGreater(len(results), 0)
        for rec in results:
            self.assertEqual(rec["grade"], "A")

    def test_query_filters_results(self):
        results = search_recommendations(query="colorectal", limit=5)
        self.assertGreater(len(results), 0)
        joined = " ".join(r["title"].lower() for r in results)
        self.assertIn("colorectal", joined)

    def test_no_matches_returns_empty(self):
        results = search_recommendations(query="xyz_no_such_topic_12345", limit=5)
        self.assertEqual(results, [])

    def test_limit_capped_to_25(self):
        results = search_recommendations(query="", limit=999)
        self.assertLessEqual(len(results), 25)

    def test_results_sorted_a_before_b(self):
        results = search_recommendations(query="", limit=20)
        grade_indices = {"A": 0, "B": 1, "C": 2, "D": 3, "I": 4, "": 5}
        last_index = -1
        for rec in results:
            current = grade_indices.get(rec["grade"], 99)
            self.assertGreaterEqual(current, last_index)
            last_index = current


class FormatRecommendationTests(TestCase):
    def test_includes_title_and_grade(self):
        rec = {"title": "Foo", "grade": "A"}
        formatted = format_recommendation(rec)
        self.assertIn("Foo", formatted)
        self.assertIn("Grade A", formatted)

    def test_includes_url_when_present(self):
        rec = {"title": "Foo", "grade": "B", "url": "https://example.com/x"}
        formatted = format_recommendation(rec)
        self.assertIn("https://example.com/x", formatted)


class GetUSPSTFInfoTests(TestCase):
    def test_returns_intro_with_results(self):
        result = get_uspstf_info({"query": "colorectal", "limit": 1})
        self.assertIn("USPSTF", result)
        self.assertIn("ACA", result)
        self.assertIn("Colorectal", result)

    def test_no_results_message(self):
        result = get_uspstf_info({"query": "xyz_no_match_zzzz", "limit": 1})
        self.assertIn("No USPSTF recommendations found", result)


class FindRecommendationsForCodesTests(TestCase):
    def test_colorectal_screening_code(self):
        recs = find_recommendations_for_codes(["Z12.11"], limit=2)
        self.assertGreater(len(recs), 0)
        self.assertTrue(any("colorectal" in (r["title"] or "").lower() for r in recs))

    def test_dotless_icd10_code_matches(self):
        """ICD-10 codes can appear without dots (e.g. 'Z1211'); they must match
        the same recommendations as the dotted form."""
        with_dot = find_recommendations_for_codes(["Z12.11"], limit=2)
        without_dot = find_recommendations_for_codes(["Z1211"], limit=2)
        self.assertEqual(
            [r["id"] for r in with_dot],
            [r["id"] for r in without_dot],
        )
        self.assertGreater(len(without_dot), 0)

    def test_z12_2_maps_to_lung_not_colorectal(self):
        """ICD-10 Z12.2 is screening for respiratory organs (lung)."""
        recs = find_recommendations_for_codes(["Z12.2"], limit=3)
        self.assertGreater(len(recs), 0)
        titles = " ".join((r["title"] or "").lower() for r in recs)
        self.assertIn("lung", titles)
        self.assertNotIn("colorectal", titles)

    def test_breast_cancer_code(self):
        recs = find_recommendations_for_codes(["77067"], limit=2)
        self.assertGreater(len(recs), 0)
        self.assertTrue(any("breast" in (r["title"] or "").lower() for r in recs))

    def test_unknown_code_returns_empty(self):
        recs = find_recommendations_for_codes(["xyzzyzzzz"], limit=2)
        self.assertEqual(recs, [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(find_recommendations_for_codes([], limit=2), [])


class CacheSeedingTests(TestCase):
    def test_seed_populates_cache_from_fallback(self):
        USPSTFRecommendation.objects.all().delete()
        results = search_recommendations(grade="A", limit=10)
        self.assertGreater(len(results), 0)
        self.assertGreater(USPSTFRecommendation.objects.count(), 0)
        # All bundled records should be present.
        self.assertEqual(
            USPSTFRecommendation.objects.count(),
            len(FALLBACK_RECOMMENDATIONS),
        )


class SyncRecommendationsTests(TestCase):
    """USPSTFClient.sync_recommendations should fall back when API returns empty."""

    def test_sync_uses_fallback_when_api_empty(self):
        from fighthealthinsurance.uspstf_api import USPSTFClient

        async def empty_fetch(self):
            return []

        with patch.object(USPSTFClient, "fetch_all_recommendations", empty_fetch):
            import asyncio

            client = USPSTFClient(api_url="http://invalid.local/api/json")
            count = asyncio.run(client.sync_recommendations())

        self.assertEqual(count, len(FALLBACK_RECOMMENDATIONS))
        self.assertGreater(USPSTFRecommendation.objects.count(), 0)

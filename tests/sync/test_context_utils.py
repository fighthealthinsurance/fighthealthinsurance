"""Tests for fighthealthinsurance.context_utils.

Covers boundary-aware truncation, deduplication, block merging, and the
supplemental-citation attach pattern used in the appeal/denial flow.
"""

import unittest

from fighthealthinsurance.context_utils import (
    attach_supplemental_to_citations,
    dedupe_blocks,
    flatten_citation_context,
    merge_context_blocks,
    truncate_at_boundary,
)


class TestTruncateAtBoundary(unittest.TestCase):
    def test_returns_empty_for_none_or_empty(self):
        self.assertEqual(truncate_at_boundary(None, 100), "")
        self.assertEqual(truncate_at_boundary("", 100), "")

    def test_returns_empty_for_nonpositive_budget(self):
        self.assertEqual(truncate_at_boundary("hello", 0), "")
        self.assertEqual(truncate_at_boundary("hello", -5), "")

    def test_returns_input_when_within_budget(self):
        text = "Short enough text."
        self.assertEqual(truncate_at_boundary(text, 100), text)

    def test_prefers_paragraph_boundary(self):
        text = (
            "First paragraph with some sentences. Second one too.\n\n"
            "Second paragraph keeps going here and is long enough."
        )
        result = truncate_at_boundary(text, 70)
        self.assertTrue(result.endswith("..."))
        # Cut should be at the paragraph break, not mid-sentence.
        self.assertNotIn("Second paragraph", result)
        self.assertIn("Second one too.", result)

    def test_prefers_sentence_boundary_when_no_paragraph(self):
        text = (
            "First sentence here. Second sentence follows. "
            "Third sentence keeps going and going."
        )
        result = truncate_at_boundary(text, 45)
        self.assertTrue(result.endswith("..."))
        # Should not end mid-word.
        body = result.rstrip(".").rstrip()
        self.assertFalse(body.endswith("Thir"))
        # First sentence should be present.
        self.assertIn("First sentence here.", result)

    def test_falls_back_to_word_boundary(self):
        text = "alpha beta gamma delta epsilon zeta eta theta iota"
        result = truncate_at_boundary(text, 20)
        self.assertTrue(result.endswith("..."))
        # No partial word: the part before the ellipsis must be a prefix
        # of the source text.
        body = result[: -len("...")].rstrip()
        self.assertTrue(text.startswith(body))

    def test_hard_cut_when_no_boundary_in_window(self):
        text = "x" * 100
        result = truncate_at_boundary(text, 20)
        self.assertEqual(len(result), 20)
        self.assertTrue(result.endswith("..."))

    def test_custom_ellipsis(self):
        text = "First sentence. Second sentence. Third sentence."
        result = truncate_at_boundary(text, 25, ellipsis=" [snip]")
        self.assertTrue(result.endswith("[snip]"))

    def test_empty_ellipsis_allows_full_budget(self):
        text = "x" * 100
        result = truncate_at_boundary(text, 50, ellipsis="")
        self.assertEqual(len(result), 50)

    def test_empty_ellipsis_no_trailing_space_on_sentence_cut(self):
        # Regression: trailing " " was previously appended on the
        # sentence/word boundary paths even when ellipsis="", giving the
        # RAG query a stray trailing space.
        text = "First sentence. Second sentence. Third sentence keeps going."
        result = truncate_at_boundary(text, 40, ellipsis="")
        self.assertFalse(result.endswith(" "))

    def test_output_never_exceeds_max_chars(self):
        # Regression: previous implementation could exceed max_chars by 1
        # because " " + ellipsis was appended after only len(ellipsis)
        # was reserved in the budget.
        cases = [
            ("First sentence. Second sentence. Third sentence.", 25, "..."),
            ("alpha beta gamma delta epsilon zeta eta theta", 20, "..."),
            ("paragraph one ends.\n\nparagraph two continues here.", 25, "..."),
            ("x" * 200, 50, "..."),
            ("First sentence. Second.", 22, " [more]"),
            ("words " * 50, 40, ""),
        ]
        for text, max_chars, ellipsis in cases:
            with self.subTest(text=text[:20], max_chars=max_chars):
                result = truncate_at_boundary(text, max_chars, ellipsis=ellipsis)
                self.assertLessEqual(
                    len(result),
                    max_chars,
                    f"Output {len(result)} > max_chars {max_chars}: {result!r}",
                )

    def test_max_chars_smaller_than_ellipsis_falls_back_to_hard_cut(self):
        # Regression: when max_chars < len(ellipsis) the previous code
        # still appended the ellipsis, exceeding the budget. Now the
        # function hard-cuts without an ellipsis.
        text = "hello world"
        for max_chars in (1, 2, 3):
            with self.subTest(max_chars=max_chars):
                result = truncate_at_boundary(text, max_chars, ellipsis="...")
                self.assertEqual(result, text[:max_chars])
                self.assertEqual(len(result), max_chars)


class TestDedupeBlocks(unittest.TestCase):
    def test_skips_none_and_empty(self):
        self.assertEqual(dedupe_blocks([None, "", "  ", "kept"]), ["kept"])

    def test_dedupe_is_case_and_whitespace_insensitive(self):
        result = dedupe_blocks(["Hello World", "  hello  WORLD  ", "Other"])
        self.assertEqual(result, ["Hello World", "Other"])

    def test_preserves_order_of_first_occurrence(self):
        result = dedupe_blocks(["b", "a", "B", "c", "A"])
        self.assertEqual(result, ["b", "a", "c"])

    def test_min_chars_filter(self):
        result = dedupe_blocks(["ok", "x", "longer text"], min_chars=3)
        self.assertEqual(result, ["longer text"])


class TestMergeContextBlocks(unittest.TestCase):
    def test_empty_input_returns_empty_string(self):
        self.assertEqual(merge_context_blocks([]), "")
        self.assertEqual(merge_context_blocks([None, "", "  "]), "")

    def test_joins_with_default_separator(self):
        result = merge_context_blocks(["one", "two", "three"])
        self.assertEqual(result, "one\n\ntwo\n\nthree")

    def test_dedupes_by_default(self):
        result = merge_context_blocks(["one", "ONE", "two"])
        self.assertEqual(result, "one\n\ntwo")

    def test_dedupe_can_be_disabled(self):
        result = merge_context_blocks(["one", "ONE"], dedupe=False)
        self.assertEqual(result, "one\n\nONE")

    def test_respects_max_chars_budget(self):
        result = merge_context_blocks(
            ["aaaa", "bbbb", "cccc"], separator="|", max_chars=9
        )
        # First two cost 4 + 1 + 4 = 9; third would exceed.
        self.assertEqual(result, "aaaa|bbbb")

    def test_max_chars_drops_oversized_first_block(self):
        result = merge_context_blocks(["a" * 100], max_chars=10)
        self.assertEqual(result, "")


class TestFlattenCitationContext(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(flatten_citation_context(None), "")

    def test_string_returned_stripped(self):
        self.assertEqual(flatten_citation_context("  hello  "), "hello")

    def test_list_joined_with_newlines(self):
        result = flatten_citation_context(["a", "b", "c"])
        self.assertEqual(result, "a\nb\nc")

    def test_list_skips_falsy_entries(self):
        result = flatten_citation_context(["a", "", None, "b"])
        self.assertEqual(result, "a\nb")

    def test_list_skips_whitespace_only_entries(self):
        # Regression: whitespace-only entries are truthy but strip to
        # empty, previously rendering as blank lines.
        result = flatten_citation_context(["a", "   ", "\t\n", "b"])
        self.assertEqual(result, "a\nb")


class TestAttachSupplementalToCitations(unittest.TestCase):
    def test_no_supplemental_returns_inputs_unchanged(self):
        ml, pm = attach_supplemental_to_citations("ml ctx", "pm ctx", None)
        self.assertEqual(ml, "ml ctx")
        self.assertEqual(pm, "pm ctx")

        ml, pm = attach_supplemental_to_citations(None, None, "  ")
        self.assertIsNone(ml)
        self.assertIsNone(pm)

    def test_appends_to_ml_citation_when_present(self):
        ml, pm = attach_supplemental_to_citations("existing", "pm", "new evidence")
        self.assertEqual(ml, "existing\n\nnew evidence")
        self.assertEqual(pm, "pm")

    def test_flattens_list_ml_citation_before_appending(self):
        ml, pm = attach_supplemental_to_citations(
            ["citation A", "citation B"], None, "extra"
        )
        self.assertEqual(ml, "citation A\ncitation B\n\nextra")
        self.assertIsNone(pm)

    def test_appends_to_pubmed_when_ml_empty(self):
        ml, pm = attach_supplemental_to_citations(None, "pubmed", "extra")
        self.assertIsNone(ml)
        self.assertEqual(pm, "pubmed\n\nextra")

    def test_uses_supplemental_standalone_when_both_empty(self):
        ml, pm = attach_supplemental_to_citations(None, None, "lonely citation")
        self.assertEqual(ml, "lonely citation")
        self.assertIsNone(pm)

    def test_empty_string_citation_treated_as_empty(self):
        ml, pm = attach_supplemental_to_citations("", "", "extra")
        self.assertEqual(ml, "extra")
        self.assertEqual(pm, "")

    def test_dedupes_when_supplemental_already_present_in_ml(self):
        # Realistic shape: existing already has the supplemental block
        # appended via \n\n from a prior call.
        microsite_block = (
            "## Additional Medical Evidence\n\n"
            "Microsite link: https://example.com/x"
        )
        existing = f"original ml citations\n\n{microsite_block}"
        ml, pm = attach_supplemental_to_citations(existing, None, microsite_block)
        # Already contained — must not re-append.
        self.assertEqual(ml, existing)
        self.assertIsNone(pm)

    def test_dedupes_when_supplemental_already_present_in_pubmed(self):
        imr_block = "Prior IMR decision: case 12345 reversed."
        existing = f"PubMed reference list ...\n\n{imr_block}"
        ml, pm = attach_supplemental_to_citations(None, existing, imr_block)
        self.assertIsNone(ml)
        self.assertEqual(pm, existing)

    def test_dedupe_does_not_match_substring_inside_unrelated_block(self):
        # Regression: previous implementation used substring containment
        # on normalized text, so a short supplemental could be silently
        # dropped if it happened to appear inside an unrelated block.
        existing = "Lorem ipsum dolor sit amet. Background context here."
        supp = "ipsum"
        ml, pm = attach_supplemental_to_citations(existing, None, supp)
        self.assertEqual(ml, f"{existing}\n\n{supp}")
        self.assertIsNone(pm)

    def test_dedupes_multi_paragraph_supplemental_appearing_in_middle(self):
        # The supplemental can be multi-paragraph and may be sandwiched
        # between other context. Block-sequence match should detect it.
        supp = "## Heading\n\nFirst para of supp.\n\nSecond para of supp."
        existing = f"head context\n\n{supp}\n\ntail context"
        ml, pm = attach_supplemental_to_citations(existing, None, supp)
        self.assertEqual(ml, existing)
        self.assertIsNone(pm)

    def test_dedupe_preserves_list_input_type_when_already_present(self):
        # Regression: when ml_citation_context is a list whose flattened
        # form already contains the supplemental (e.g. a single entry
        # that the previous call appended into), the function must
        # return the original list rather than silently flattening it
        # to a string. Downstream consumers in common_view_logic.py
        # branch on isinstance(..., list) and produce different output
        # for the two shapes.
        supp = "Prior IMR decision: case 12345 reversed."
        # flatten_citation_context joins list entries with "\n", so a
        # single entry containing "...\n\nsupp" splits into two blocks
        # on "\n\n" and the dedup matcher can detect it.
        existing_list = [f"original context\n\n{supp}"]
        ml, pm = attach_supplemental_to_citations(existing_list, None, supp)
        self.assertIs(ml, existing_list)
        self.assertIsNone(pm)


if __name__ == "__main__":
    unittest.main()

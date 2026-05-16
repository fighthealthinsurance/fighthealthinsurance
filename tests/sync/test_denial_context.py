"""Tests for ``fighthealthinsurance.denial_context`` merge helpers."""

import json
import unittest
from types import SimpleNamespace

from fighthealthinsurance.denial_context import (
    load_qa,
    merge_plan_context,
    merge_qa,
)


def _fake_denial(qa_context=None, plan_context=None, denial_id=1):
    return SimpleNamespace(
        denial_id=denial_id,
        qa_context=qa_context,
        plan_context=plan_context,
    )


class TestLoadQA(unittest.TestCase):
    def test_none_returns_empty_dict(self):
        self.assertEqual(load_qa(_fake_denial()), {})

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(load_qa(_fake_denial(qa_context="")), {})

    def test_valid_json_dict_loads(self):
        d = _fake_denial(qa_context=json.dumps({"a": "1", "b": "2"}))
        self.assertEqual(load_qa(d), {"a": "1", "b": "2"})

    def test_corrupted_json_falls_back_to_misc(self):
        d = _fake_denial(qa_context="not-a-json {")
        self.assertEqual(load_qa(d), {"misc": "not-a-json {"})

    def test_list_payload_falls_back_to_misc(self):
        d = _fake_denial(qa_context=json.dumps(["a", "b"]))
        self.assertEqual(load_qa(d), {"misc": '["a", "b"]'})


class TestMergeQA(unittest.TestCase):
    def test_merges_into_empty(self):
        d = _fake_denial()
        merge_qa(d, {"q1": "answer1"}, source="test")
        self.assertEqual(json.loads(d.qa_context), {"q1": "answer1"})

    def test_preserves_existing_keys_when_updates_disjoint(self):
        d = _fake_denial(qa_context=json.dumps({"medical_context": "diabetes"}))
        merge_qa(d, {"q1": "answer1"}, source="test")
        result = json.loads(d.qa_context)
        self.assertEqual(result["medical_context"], "diabetes")
        self.assertEqual(result["q1"], "answer1")

    def test_overwrites_when_new_value_truthy_and_different(self):
        d = _fake_denial(qa_context=json.dumps({"q1": "old"}))
        merge_qa(d, {"q1": "new"}, source="test")
        self.assertEqual(json.loads(d.qa_context)["q1"], "new")

    def test_ignores_empty_value(self):
        d = _fake_denial(qa_context=json.dumps({"q1": "keep"}))
        merge_qa(d, {"q1": ""}, source="test")
        self.assertEqual(json.loads(d.qa_context)["q1"], "keep")

    def test_ignores_unknown_sentinel(self):
        d = _fake_denial(qa_context=json.dumps({"q1": "keep"}))
        merge_qa(d, {"q1": "UNKNOWN", "q2": "UNKNOWN"}, source="test")
        result = json.loads(d.qa_context)
        self.assertEqual(result["q1"], "keep")
        self.assertNotIn("q2", result)

    def test_ignores_none_value(self):
        d = _fake_denial(qa_context=json.dumps({"q1": "keep"}))
        merge_qa(d, {"q1": None, "q2": None}, source="test")
        result = json.loads(d.qa_context)
        self.assertEqual(result, {"q1": "keep"})

    def test_ignores_empty_key(self):
        d = _fake_denial()
        merge_qa(d, {"": "x", "  ": "y", "real": "z"}, source="test")
        result = json.loads(d.qa_context)
        # Whitespace-only keys are kept (preserves any legacy form keys)
        # but truly empty string keys are dropped.
        self.assertIn("real", result)
        self.assertNotIn("", result)

    def test_strips_whitespace_from_values(self):
        d = _fake_denial()
        merge_qa(d, {"q1": "  trimmed  "}, source="test")
        self.assertEqual(json.loads(d.qa_context)["q1"], "trimmed")

    def test_no_change_when_all_updates_redundant(self):
        d = _fake_denial(qa_context=json.dumps({"q1": "same"}))
        original = d.qa_context
        merge_qa(d, {"q1": "same"}, source="test")
        self.assertEqual(d.qa_context, original)

    def test_corrupted_existing_qa_preserved_under_misc(self):
        d = _fake_denial(qa_context="raw notes here")
        merge_qa(d, {"q1": "answer"}, source="test")
        result = json.loads(d.qa_context)
        self.assertEqual(result["misc"], "raw notes here")
        self.assertEqual(result["q1"], "answer")

    def test_medical_context_survives_disjoint_qa_merge(self):
        """The original clobber bug: REST viewset overwrote medical_context."""
        d = _fake_denial(qa_context=json.dumps({"medical_context": "patient has X"}))
        # Simulate the REST viewset rebuilding from DenialQA rows
        merge_qa(d, {"What is your diagnosis?": "X"}, source="rest_qa_response")
        result = json.loads(d.qa_context)
        self.assertEqual(result["medical_context"], "patient has X")
        self.assertEqual(result["What is your diagnosis?"], "X")


class TestMergePlanContext(unittest.TestCase):
    def test_sets_when_empty(self):
        d = _fake_denial()
        merge_plan_context(d, ["plan A details"])
        self.assertEqual(d.plan_context, "plan A details")

    def test_appends_distinct_fragment(self):
        d = _fake_denial(plan_context="plan A details")
        merge_plan_context(d, ["plan B details"])
        self.assertIn("plan A details", d.plan_context)
        self.assertIn("plan B details", d.plan_context)

    def test_deduplicates_repeat_fragment(self):
        d = _fake_denial(plan_context="plan A details")
        merge_plan_context(d, ["plan A details"])
        self.assertEqual(d.plan_context.count("plan A details"), 1)

    def test_drops_empty_fragments(self):
        d = _fake_denial(plan_context="plan A")
        merge_plan_context(d, ["", None, "  ", "plan B"])
        self.assertIn("plan A", d.plan_context)
        self.assertIn("plan B", d.plan_context)

    def test_no_change_when_all_fragments_empty(self):
        d = _fake_denial(plan_context="plan A")
        merge_plan_context(d, ["", None, "  "])
        self.assertEqual(d.plan_context, "plan A")

    def test_returns_none_when_nothing_present(self):
        d = _fake_denial()
        result = merge_plan_context(d, [])
        self.assertIsNone(result)

    def test_growth_across_multiple_calls(self):
        d = _fake_denial()
        merge_plan_context(d, ["fragment 1"])
        merge_plan_context(d, ["fragment 2"])
        merge_plan_context(d, ["fragment 3"])
        self.assertIn("fragment 1", d.plan_context)
        self.assertIn("fragment 2", d.plan_context)
        self.assertIn("fragment 3", d.plan_context)


if __name__ == "__main__":
    unittest.main()

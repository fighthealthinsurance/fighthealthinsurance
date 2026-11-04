"""LLM procedure categories: the two-pass backfill and the staff chart's
second (by-category) view.

The model is never called: ``infer_with_fallback`` is patched in the module
under test, with replies keyed off the prompt's pass.
"""

import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from fighthealthinsurance import procedure_categories as pc
from fighthealthinsurance.models import Denial, ProcedureCategory

User = get_user_model()

INFER = "fighthealthinsurance.procedure_categories.infer_with_fallback"


def _make_denials(*procedures):
    for i, procedure in enumerate(procedures):
        Denial.objects.create(
            semi_sekret="s", hashed_email=f"person-{i}", procedure=procedure
        )


def _fake_model(categories, assign):
    """An infer_with_fallback stand-in: pass 1 answers ``categories``, pass 2
    answers ``assign(procedure)`` for each numbered procedure."""

    async def infer(system_prompts, prompt, **kwargs):
        if system_prompts == [pc._DISCOVER_SYSTEM]:
            return "Sure! " + json.dumps(categories)
        lines = prompt.split("Procedures:\n", 1)[1].split("\n\n", 1)[0]
        answer = {}
        for line in lines.splitlines():
            number, procedure = line.split(". ", 1)
            category = assign(procedure)
            if category is not None:
                answer[number] = category
        return "```json\n" + json.dumps(answer) + "\n```"

    return AsyncMock(side_effect=infer)


class ParsingTest(SimpleTestCase):
    def test_off_list_category_becomes_other(self):
        result = pc.parse_assignments(
            '{"1": "imaging", "2": "Astrology"}', ["mri", "tarot"], ["Imaging", "Other"]
        )
        self.assertEqual(result, {"mri": "Imaging", "tarot": "Other"})

    def test_out_of_range_numbers_are_ignored(self):
        result = pc.parse_assignments('{"7": "Imaging"}', ["mri"], ["Imaging"])
        self.assertEqual(result, {})

    def test_merge_keeps_existing_spelling_and_puts_other_last(self):
        merged = pc.merge_categories(
            ["Imaging"], ["imaging", "Surgery", "Other", "Uncategorized"]
        )
        self.assertEqual(merged, ["Imaging", "Surgery", "Other"])

    def test_category_list_accepts_wrapped_object(self):
        self.assertEqual(
            pc.parse_category_list('{"categories": [" Imaging. ", 3]}'), ["Imaging"]
        )


class CategorizeProceduresTest(TestCase):
    def test_two_passes_store_one_row_per_normalized_procedure(self):
        _make_denials("MRI", "  mri ", "Wegovy", "Physical Therapy")
        fake = _fake_model(
            ["Imaging", "Drugs"],
            lambda p: {"mri": "Imaging", "wegovy": "Drugs"}.get(p, "Rehab"),
        )
        with patch(INFER, fake):
            async_to_sync(pc.categorize_procedures)()
        self.assertEqual(
            pc.stored_categories(),
            {"mri": "Imaging", "wegovy": "Drugs", "physical therapy": "Other"},
        )

    def test_pass_one_categories_are_fed_into_pass_two(self):
        _make_denials("MRI")
        fake = _fake_model(["Imaging"], lambda p: "Imaging")
        with patch(INFER, fake):
            async_to_sync(pc.categorize_procedures)()
        assign_prompt = fake.await_args_list[1].kwargs["prompt"]
        self.assertIn("- Imaging\n- Other", assign_prompt)

    def test_rerun_feeds_back_stored_categories_and_skips_done_rows(self):
        _make_denials("MRI", "Wegovy")
        ProcedureCategory.objects.create(procedure="mri", category="Radiology")
        fake = _fake_model(["Drugs"], lambda p: "Drugs")
        with patch(INFER, fake):
            stats = async_to_sync(pc.categorize_procedures)()
        discover_prompt = fake.await_args_list[0].kwargs["prompt"]
        self.assertEqual(
            (stats["categorized"], "- Radiology" in discover_prompt),
            (1, True),
        )

    def test_skipped_procedure_stays_uncategorized(self):
        _make_denials("MRI", "Wegovy")
        fake = _fake_model(
            ["Imaging"], lambda p: "Imaging" if p == "mri" else None
        )
        with patch(INFER, fake):
            async_to_sync(pc.categorize_procedures)()
        self.assertEqual(pc.stored_categories(), {"mri": "Imaging"})

    def test_no_taxonomy_writes_nothing(self):
        _make_denials("MRI")
        with patch(INFER, AsyncMock(return_value=None)):
            async_to_sync(pc.categorize_procedures)()
        self.assertFalse(ProcedureCategory.objects.exists())

    def test_dry_run_writes_nothing(self):
        _make_denials("MRI")
        fake = _fake_model(["Imaging"], lambda p: "Imaging")
        with patch(INFER, fake):
            stats = async_to_sync(pc.categorize_procedures)(dry_run=True)
        self.assertEqual(
            (stats["assignments"], ProcedureCategory.objects.exists()),
            ({"mri": "Imaging"}, False),
        )


class ProceduresDeniedChartTest(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.client.login(username="staff", password="pw")

    def test_category_view_counts_denials_and_uncategorized(self):
        _make_denials("MRI", "mri", "CT scan", "Wegovy")
        ProcedureCategory.objects.create(procedure="mri", category="Imaging")
        ProcedureCategory.objects.create(procedure="ct scan", category="Imaging")
        response = self.client.get(reverse("charts:procedures_denied_chart"))
        self.assertEqual(
            pc.category_counts(pc.denied_procedure_counts(), pc.stored_categories()),
            {"Imaging": 3, "Uncategorized": 1},
        )
        self.assertContains(response, "Denied Procedures by Category")

    def test_chart_page_renders_without_any_categories(self):
        _make_denials("MRI")
        response = self.client.get(reverse("charts:procedures_denied_chart"))
        self.assertContains(response, "Unique procedures not yet categorized: 1")

    def test_chart_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse("charts:procedures_denied_chart"))
        self.assertIn(response.status_code, [302, 403])

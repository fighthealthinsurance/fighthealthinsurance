"""Tests for the phone call script generator (issue #568)."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import django

django.setup()

from fighthealthinsurance.call_script_helper import CallScriptHelper
from fighthealthinsurance.call_script_prompts import (
    PROMPT_VERSION,
    build_generation_prompt,
    get_system_prompts,
)


class TestPrompts(unittest.TestCase):
    def test_info_gathering_prompts_mention_ai_policy(self):
        prompts = get_system_prompts("info_gathering")
        self.assertEqual(len(prompts), 1)
        body = prompts[0].lower()
        self.assertIn("medical policy", body)
        self.assertIn("ai", body)

    def test_escalation_prompts_request_supervisor(self):
        prompts = get_system_prompts("escalation")
        self.assertEqual(len(prompts), 1)
        self.assertIn("supervisor", prompts[0].lower())

    def test_unknown_goal_falls_back_to_info_gathering(self):
        # Defensive default: callers should validate goal before this, but a
        # typo shouldn't crash prompt construction.
        self.assertEqual(
            get_system_prompts("nonsense"), get_system_prompts("info_gathering")
        )

    def test_build_generation_prompt_uses_only_cache_key_fields(self):
        prompt = build_generation_prompt(
            insurer="Aetna", denial_reason="medical necessity"
        )
        # Cache key is (insurer, denial_reason, goal, prompt_version). The
        # prompt must not include any input that isn't in the key, or a
        # single cached row would serve callers with different inputs.
        self.assertIn("Aetna", prompt)
        self.assertIn("medical necessity", prompt)
        for leaked in ("MRI", "lumbar", "back pain", "ssn", "date of birth"):
            self.assertNotIn(leaked.lower(), prompt.lower())
        # Placeholders must survive so the model wraps them into the script.
        self.assertIn("[PATIENT NAME]", prompt)
        self.assertIn("[MEMBER ID]", prompt)


class TestResolveHelpers(unittest.TestCase):
    def _denial(self, **kwargs):
        d = MagicMock()
        d.insurance_company = kwargs.get("insurance_company")
        d.insurance_company_obj = kwargs.get("insurance_company_obj")
        d.denial_type_text = kwargs.get("denial_type_text")
        d.denial_text = kwargs.get("denial_text", "")
        d.claim_id = kwargs.get("claim_id")
        d.date_of_service = kwargs.get("date_of_service")
        d.date_of_service_text = kwargs.get("date_of_service_text")
        d.procedure = kwargs.get("procedure")
        d.diagnosis = kwargs.get("diagnosis")
        return d

    def test_insurer_prefers_explicit_override(self):
        d = self._denial(
            insurance_company="Aetna",
            insurance_company_obj=MagicMock(name="Cigna"),
        )
        d.insurance_company_obj.name = "Cigna"
        self.assertEqual(
            CallScriptHelper._resolve_insurer(d, "UnitedHealthcare"),
            "UnitedHealthcare",
        )

    def test_insurer_prefers_fk_over_text(self):
        obj = MagicMock()
        obj.name = "Cigna"
        d = self._denial(insurance_company="Aetna", insurance_company_obj=obj)
        self.assertEqual(CallScriptHelper._resolve_insurer(d, None), "Cigna")

    def test_insurer_falls_back_to_text(self):
        d = self._denial(insurance_company="Aetna", insurance_company_obj=None)
        self.assertEqual(CallScriptHelper._resolve_insurer(d, None), "Aetna")

    def test_insurer_default_when_nothing_set(self):
        d = self._denial(insurance_company=None, insurance_company_obj=None)
        self.assertEqual(
            CallScriptHelper._resolve_insurer(d, None), "the insurance company"
        )

    def test_denial_reason_prefers_typed_label(self):
        d = self._denial(
            denial_type_text="Not medically necessary",
            denial_text="A long letter explaining the denial...",
        )
        self.assertEqual(
            CallScriptHelper._resolve_denial_reason(d, None),
            "Not medically necessary",
        )

    def test_denial_reason_never_falls_back_to_raw_denial_text(self):
        # Raw denial_text can contain patient names / claim IDs, so it MUST
        # NOT leak into the non-PHI GenericCallScript cache. Without a typed
        # label, callers get "unspecified" and the LLM has to ask better
        # questions on the call itself.
        d = self._denial(
            denial_type_text=None, denial_text="Patient John Doe, claim 99..."
        )
        self.assertEqual(
            CallScriptHelper._resolve_denial_reason(d, None), "unspecified"
        )

    def test_normalize_folds_case_and_whitespace(self):
        self.assertEqual(
            CallScriptHelper._normalize("  Anthem  Blue   CROSS  "),
            "anthem blue cross",
        )


class TestPlaceholderSubstitution(unittest.TestCase):
    def test_fills_known_placeholders(self):
        d = MagicMock()
        d.claim_id = "ABC-123"
        d.date_of_service = "2026-01-15"
        d.date_of_service_text = None
        script = "Please pull up claim [CLAIM ID] for date [DATE OF SERVICE]."
        result = CallScriptHelper._substitute_placeholders(script, d)
        self.assertIn("ABC-123", result)
        self.assertIn("2026-01-15", result)
        self.assertNotIn("[CLAIM ID]", result)

    def test_leaves_placeholders_when_missing(self):
        d = MagicMock()
        d.claim_id = None
        d.date_of_service = None
        d.date_of_service_text = None
        script = "Please pull up claim [CLAIM ID] for date [DATE OF SERVICE]."
        # When unknown, placeholders stay so the caller fills them in by mouth.
        result = CallScriptHelper._substitute_placeholders(script, d)
        self.assertIn("[CLAIM ID]", result)
        self.assertIn("[DATE OF SERVICE]", result)

    def test_member_id_placeholder_left_alone(self):
        # Member ID is never sourced from the denial, so it must always stay
        # as a placeholder for the caller to read aloud.
        d = MagicMock()
        d.claim_id = None
        d.date_of_service = None
        d.date_of_service_text = None
        result = CallScriptHelper._substitute_placeholders(
            "My member ID is [MEMBER ID].", d
        )
        self.assertIn("[MEMBER ID]", result)


class TestPrintableHtml(unittest.TestCase):
    def test_html_contains_script_and_insurer(self):
        d = MagicMock()
        d.claim_id = "C1"
        d.date_of_service = "2026-01-01"
        d.procedure = "MRI"
        d.diagnosis = "Back pain"
        html = CallScriptHelper.render_printable_html(
            script_text="1. Hello (pause)",
            denial=d,
            goal="info_gathering",
            insurer_name="Aetna",
        )
        self.assertIn("<html", html.lower())
        self.assertIn("Aetna", html)
        self.assertIn("1. Hello", html)
        self.assertIn("Information Gathering", html)


class TestGenerateCallScript(unittest.TestCase):
    def test_invalid_goal_raises(self):
        d = MagicMock()
        with self.assertRaises(ValueError):
            asyncio.run(CallScriptHelper.generate_call_script(denial=d, goal="bogus"))

    @patch("fighthealthinsurance.call_script_helper.CallScript.objects")
    @patch("fighthealthinsurance.call_script_helper.GenericCallScript.objects")
    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_cache_hit_skips_llm(
        self, mock_infer, mock_generic_objs, mock_callscript_objs
    ):
        cached = MagicMock(script_text="cached script")
        mock_generic_objs.filter.return_value.afirst = AsyncMock(return_value=cached)
        mock_callscript_objs.acreate = AsyncMock(return_value=MagicMock(id="uuid-1"))

        d = MagicMock()
        d.insurance_company = "Aetna"
        d.insurance_company_obj = None
        d.denial_type_text = "medical necessity"
        d.denial_text = ""
        d.claim_id = None
        d.date_of_service = None
        d.date_of_service_text = None
        d.procedure = None
        d.diagnosis = None

        result = asyncio.run(
            CallScriptHelper.generate_call_script(denial=d, goal="info_gathering")
        )

        mock_infer.assert_not_awaited()
        self.assertIsNotNone(result)
        self.assertEqual(result.script_text, "cached script")

    @patch("fighthealthinsurance.call_script_helper.CallScript.objects")
    @patch("fighthealthinsurance.call_script_helper.GenericCallScript.objects")
    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_cache_miss_calls_llm_and_persists(
        self, mock_infer, mock_generic_objs, mock_callscript_objs
    ):
        mock_generic_objs.filter.return_value.afirst = AsyncMock(return_value=None)
        new_generic = MagicMock(script_text="fresh script")
        mock_generic_objs.aget_or_create = AsyncMock(return_value=(new_generic, True))
        mock_callscript_objs.acreate = AsyncMock(return_value=MagicMock(id="uuid-2"))
        mock_infer.return_value = "fresh script that is long enough to pass min_length"

        d = MagicMock()
        d.insurance_company = "Aetna"
        d.insurance_company_obj = None
        d.denial_type_text = "medical necessity"
        d.denial_text = ""
        d.claim_id = "X1"
        d.date_of_service = None
        d.date_of_service_text = None
        d.procedure = None
        d.diagnosis = None

        result = asyncio.run(
            CallScriptHelper.generate_call_script(denial=d, goal="escalation")
        )

        mock_infer.assert_awaited_once()
        mock_generic_objs.aget_or_create.assert_awaited_once()
        # Cache key must include the current prompt version so prompt bumps
        # invalidate stale rows.
        kwargs = mock_generic_objs.aget_or_create.call_args.kwargs
        self.assertEqual(kwargs["prompt_version"], PROMPT_VERSION)
        self.assertEqual(kwargs["goal"], "escalation")
        self.assertIsNotNone(result)

    @patch("fighthealthinsurance.call_script_helper.GenericCallScript.objects")
    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_all_models_fail_returns_none(self, mock_infer, mock_generic_objs):
        mock_generic_objs.filter.return_value.afirst = AsyncMock(return_value=None)
        mock_infer.return_value = None

        d = MagicMock()
        d.insurance_company = "Aetna"
        d.insurance_company_obj = None
        d.denial_type_text = "medical necessity"
        d.denial_text = ""
        d.claim_id = None
        d.date_of_service = None
        d.date_of_service_text = None
        d.procedure = None
        d.diagnosis = None

        result = asyncio.run(
            CallScriptHelper.generate_call_script(denial=d, goal="info_gathering")
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

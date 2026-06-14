"""Tests for the curated regulatory-citation hooks, the make_open_prompt
injection, and the defensive _collect_regulatory_context wiring.

The selector only emits content for states we have a verified hook for, frames
every hook conservatively (cite only where applicable), and adjusts the ERISA
caveat by plan type without ever suppressing a hook.
"""

import datetime
import unittest
from types import SimpleNamespace

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.regulatory_citations import (
    get_regulatory_citation_context,
)


class TestGetRegulatoryCitationContext(unittest.TestCase):
    def test_returns_none_for_missing_state(self):
        self.assertIsNone(get_regulatory_citation_context(None))
        self.assertIsNone(get_regulatory_citation_context(""))

    def test_returns_none_for_state_without_hooks(self):
        # Nevada has no curated hook today, so we inject nothing.
        self.assertIsNone(get_regulatory_citation_context("NV"))

    def test_massachusetts_block_includes_state_and_federal_hooks(self):
        block = get_regulatory_citation_context("MA")
        self.assertIsNotNone(block)
        assert block is not None  # for type-checkers
        self.assertIn("REGULATORY CONTEXT", block)
        self.assertIn("Massachusetts", block)
        # Federal hooks are surfaced alongside the state hook.
        self.assertIn("CMS-0057-F", block)

    def test_full_state_name_is_normalized(self):
        block = get_regulatory_citation_context("Massachusetts")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Massachusetts", block)

    def test_ai_oversight_state_hook(self):
        block = get_regulatory_citation_context("CA")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("California", block)
        self.assertIn("sole basis", block)

    def test_washington_explicit_hook_supersedes_generic(self):
        # Washington has a specific, individually-verified SB 5395 hook, so the
        # block carries its real effective date and credential-disclosure demand
        # (which the generic AI-oversight framing lacked) and lists WA only once.
        block = get_regulatory_citation_context("WA")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Washington", block)
        self.assertIn("June 11, 2026", block)
        self.assertIn("credentials", block)
        # AI-as-sole-basis framing is preserved.
        self.assertIn("sole basis", block)
        # Federal hooks still ride along with the state hook.
        self.assertIn("CMS-0057-F", block)
        # No generic + explicit duplication: Washington appears exactly once.
        self.assertEqual(block.count("Washington"), 1)

    def test_maryland_explicit_hook_supersedes_generic(self):
        # Maryland HB 1563 was upgraded from the generic AI-oversight list to an
        # explicit hook: licensed-professional adverse determinations + quarterly
        # reporting to the Insurance Commissioner of whether AI was used.
        block = get_regulatory_citation_context("MD")
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Maryland", block)
        self.assertIn("licensed", block)
        self.assertIn("Commissioner", block)
        self.assertIn("quarter", block)
        # Listed exactly once (no generic + explicit duplication).
        self.assertEqual(block.count("Maryland"), 1)

    def test_indiana_downcoding_hook_gated_by_effective_date(self):
        # Indiana HB 1271 (downcoding-specific) takes effect 2026-07-01; before
        # then it is IN's only hook, so the whole block is suppressed rather
        # than cite a not-yet-in-force law.
        self.assertIsNone(
            get_regulatory_citation_context("IN", as_of=datetime.date(2026, 6, 30))
        )
        in_force = get_regulatory_citation_context(
            "IN", as_of=datetime.date(2026, 7, 1)
        )
        self.assertIsNotNone(in_force)
        assert in_force is not None
        self.assertIn("Indiana", in_force)
        self.assertIn("downcode", in_force)

    def test_georgia_hook_gated_until_2027(self):
        # Georgia SB 544 is not in force until 2027-01-01; citing it earlier
        # cuts against the module's "cite only where it applies" framing.
        self.assertIsNone(
            get_regulatory_citation_context("GA", as_of=datetime.date(2026, 12, 31))
        )
        in_force = get_regulatory_citation_context(
            "GA", as_of=datetime.date(2027, 1, 1)
        )
        self.assertIsNotNone(in_force)
        assert in_force is not None
        self.assertIn("Georgia", in_force)
        # Summary flags that it may bind only newly issued/renewed plans.
        self.assertIn("renewed", in_force)

    def test_self_insured_caveat_wording(self):
        self_insured = get_regulatory_citation_context("MA", self_insured=True)
        assert self_insured is not None
        self.assertIn("self-insured (ERISA)", self_insured)

        neutral = get_regulatory_citation_context("MA", self_insured=None)
        assert neutral is not None
        self.assertIn("fully-insured", neutral)

    def test_self_insured_drops_payer_specific_federal_hooks(self):
        block = get_regulatory_citation_context("MA", self_insured=True)
        assert block is not None
        # CMS-0057-F does not reach self-funded ERISA employer plans.
        self.assertNotIn("CMS-0057-F", block)
        # ACA internal/external review still reaches non-grandfathered
        # self-insured plans.
        self.assertIn("147.136", block)
        # State insurance mandates are dropped for self-insured plans.
        self.assertNotIn("Massachusetts", block)


class TestRegulatoryPromptInjection(unittest.TestCase):
    def setUp(self):
        self.gen = AppealGenerator()

    def test_block_is_injected_when_present(self):
        prompt = self.gen.make_open_prompt(
            denial_text="Service denied.",
            regulatory_citation_context="REG-BLOCK-SENTINEL",
        )
        assert prompt is not None
        self.assertIn("REG-BLOCK-SENTINEL", prompt)

    def test_no_block_when_absent(self):
        prompt = self.gen.make_open_prompt(denial_text="Service denied.")
        assert prompt is not None
        self.assertNotIn("REGULATORY CONTEXT", prompt)


class TestCollectRegulatoryContext(unittest.TestCase):
    def _denial(self, **overrides):
        base = dict(
            your_state=None,
            insurance_company_obj=None,
            denial_text=None,
            procedure=None,
            diagnosis=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_collects_for_state_with_hook(self):
        result = AppealGenerator._collect_regulatory_context(
            self._denial(your_state="MA")
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("Massachusetts", result)

    def test_returns_none_for_state_without_hook(self):
        self.assertIsNone(
            AppealGenerator._collect_regulatory_context(self._denial(your_state="NV"))
        )

    def test_returns_none_when_state_missing(self):
        self.assertIsNone(AppealGenerator._collect_regulatory_context(self._denial()))

    def test_tpa_carrier_selects_self_insured_caveat(self):
        denial = self._denial(
            your_state="MA",
            insurance_company_obj=SimpleNamespace(is_tpa=True),
        )
        result = AppealGenerator._collect_regulatory_context(denial)
        assert result is not None
        self.assertIn("self-insured (ERISA)", result)


if __name__ == "__main__":
    unittest.main()

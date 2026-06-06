"""Tests that the appeal-generation prompt picks up clinical_trials_context.

Parallel to ``test_uspstf_api.MakeOpenPromptIncludesUSPSTFContextTests`` and
``test_nice_context_prompt`` — the same shape, for the trial-evidence path.
"""

from django.test import TestCase


class MakeOpenPromptIncludesClinicalTrialsContextTests(TestCase):
    """Verify the appeal-generation prompt picks up clinical_trials_context."""

    def test_make_open_prompt_includes_clinical_trials_block(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text=(
                "Insurer denied pembrolizumab as experimental/investigational."
            ),
            procedure="Pembrolizumab",
            diagnosis="Melanoma",
            insurance_company="ExamplePayer",
            clinical_trials_context=(
                "CLINICAL TRIAL EVIDENCE (relevant when the denial cites "
                '"experimental" or "investigational" grounds):\n\n'
                "NCT: NCT12345678; Title: Pembrolizumab in Melanoma; "
                "Status: RECRUITING; Phase: PHASE3"
            ),
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        # The self-contained header survives unchanged into the prompt.
        self.assertIn("CLINICAL TRIAL EVIDENCE", prompt)
        self.assertIn("NCT12345678", prompt)
        # Citation instructions block kicks in because trials count as
        # citation evidence; NCT IDs must be in the no-fabricate list.
        self.assertIn("CITATION INSTRUCTIONS", prompt)
        self.assertIn("NCT IDs", prompt)

    def test_make_open_prompt_omits_clinical_trials_block_when_empty(self):
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text="Insurer denied pembrolizumab.",
            insurance_company="ExamplePayer",
            clinical_trials_context="",
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertNotIn("CLINICAL TRIAL EVIDENCE", prompt)

    def test_make_open_prompt_omits_clinical_trials_block_when_none(self):
        """Default (no clinical_trials_context kwarg) must not emit a stub
        trial section or otherwise pollute the prompt."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        prompt = generator.make_open_prompt(
            denial_text="Insurer denied pembrolizumab.",
            insurance_company="ExamplePayer",
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertNotIn("CLINICAL TRIAL EVIDENCE", prompt)

    def test_clinical_trials_context_alone_triggers_citation_instructions(self):
        """When the only evidence we have is trial data, the model must still
        get the "ONLY cite what's provided" block instead of the fallback
        "no citations available, don't invent any" block."""
        from fighthealthinsurance.generate_appeal import AppealGenerator

        generator = AppealGenerator()
        trials_only_prompt = generator.make_open_prompt(
            denial_text="Insurer denied X as experimental.",
            insurance_company="ExamplePayer",
            clinical_trials_context="CLINICAL TRIAL EVIDENCE …\n\nNCT: NCT99999999",
        )
        no_evidence_prompt = generator.make_open_prompt(
            denial_text="Insurer denied X as experimental.",
            insurance_company="ExamplePayer",
        )
        assert trials_only_prompt is not None and no_evidence_prompt is not None
        self.assertIn("CITATION INSTRUCTIONS", trials_only_prompt)
        self.assertNotIn(
            "No specific medical citations have been provided",
            trials_only_prompt,
        )
        self.assertIn(
            "No specific medical citations have been provided", no_evidence_prompt
        )

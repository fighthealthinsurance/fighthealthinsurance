"""Tests that the appeal-generation prompt picks up clinical_trials_context.

Parallel to ``test_uspstf_api.MakeOpenPromptIncludesUSPSTFContextTests`` and
``test_nice_context_prompt`` — the same shape, for the trial-evidence path.
"""

from django.test import TestCase


# A realistic rendered block (what ``get_context_for_denial`` would return):
# self-contained header + one trial row carrying an NCT id.
SAMPLE_TRIALS_CONTEXT = (
    "CLINICAL TRIAL EVIDENCE (relevant when the denial cites "
    '"experimental" or "investigational" grounds):\n\n'
    "NCT: NCT12345678; Title: Pembrolizumab in Melanoma; "
    "Status: RECRUITING; Phase: PHASE3"
)

# Minimal trials block used by the "trials are the only evidence" cases.
TRIALS_ONLY_CONTEXT = "CLINICAL TRIAL EVIDENCE …\n\nNCT: NCT99999999"

# The exact sentence make_open_prompt emits when no citation evidence at all
# was supplied; its presence/absence is how we tell the two prompt branches
# apart.
NO_CITATIONS_WARNING = "No specific medical citations have been provided"


class MakeOpenPromptIncludesClinicalTrialsContextTests(TestCase):
    """Verify the appeal-generation prompt picks up clinical_trials_context.

    Each test asserts a single behavior; shared setup (generator construction +
    make_open_prompt call with sensible defaults) lives in ``_prompt``.
    """

    def _prompt(self, **overrides) -> str:
        """Build an appeal prompt, assert it rendered, and return it.

        ``denial_text`` and ``insurance_company`` default to non-empty values
        so callers only pass the kwarg under test (e.g.
        ``clinical_trials_context``).
        """
        from fighthealthinsurance.generate_appeal import AppealGenerator

        kwargs = {
            "denial_text": "Insurer denied pembrolizumab as experimental.",
            "insurance_company": "ExamplePayer",
        }
        kwargs.update(overrides)
        prompt = AppealGenerator().make_open_prompt(**kwargs)
        self.assertIsNotNone(prompt)
        assert prompt is not None  # narrow Optional[str] for mypy
        return prompt

    def test_clinical_trials_block_included_when_provided(self):
        """The self-contained header + NCT id survive unchanged into the prompt."""
        prompt = self._prompt(clinical_trials_context=SAMPLE_TRIALS_CONTEXT)
        self.assertIn("CLINICAL TRIAL EVIDENCE", prompt)
        self.assertIn("NCT12345678", prompt)

    def test_clinical_trials_adds_nct_to_citation_no_fabricate_list(self):
        """Trials count as citation evidence: the citation-instructions block
        fires and NCT IDs join the no-fabricate list."""
        prompt = self._prompt(clinical_trials_context=SAMPLE_TRIALS_CONTEXT)
        self.assertIn("CITATION INSTRUCTIONS", prompt)
        self.assertIn("NCT IDs", prompt)

    def test_clinical_trials_block_omitted_when_empty(self):
        """An empty-string context must not emit a trial section."""
        prompt = self._prompt(clinical_trials_context="")
        self.assertNotIn("CLINICAL TRIAL EVIDENCE", prompt)

    def test_clinical_trials_block_omitted_when_none(self):
        """Default call (no clinical_trials_context kwarg) emits no trial section."""
        prompt = self._prompt()
        self.assertNotIn("CLINICAL TRIAL EVIDENCE", prompt)

    def test_trials_only_triggers_citation_instructions(self):
        """When trial data is the sole evidence, the "ONLY cite what's
        provided" block must still fire."""
        prompt = self._prompt(clinical_trials_context=TRIALS_ONLY_CONTEXT)
        self.assertIn("CITATION INSTRUCTIONS", prompt)

    def test_trials_only_suppresses_no_citations_warning(self):
        """Trial-only evidence must NOT fall through to the "no citations
        available" guard."""
        prompt = self._prompt(clinical_trials_context=TRIALS_ONLY_CONTEXT)
        self.assertNotIn(NO_CITATIONS_WARNING, prompt)

    def test_no_evidence_emits_no_citations_warning(self):
        """With no citation context of any kind, the explicit no-citations
        guard is emitted."""
        prompt = self._prompt()
        self.assertIn(NO_CITATIONS_WARNING, prompt)

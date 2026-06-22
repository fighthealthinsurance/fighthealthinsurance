"""Tests for common_view_logic.mark_proposal_chosen helper."""

from django.test import TestCase

from fighthealthinsurance.common_view_logic import mark_proposal_chosen
from fighthealthinsurance.models import Denial, ProposedAppeal


class MarkProposalChosenTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def test_exact_text_match_copies_model_name(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text-x",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(self.denial, "text-x")
        self.assertTrue(pa.chosen)
        self.assertEqual(pa.model_name, "model-x")
        # The original row is not mutated.
        original = ProposedAppeal.objects.get(
            for_denial=self.denial, appeal_text="text-x", chosen=False
        )
        self.assertEqual(original.model_name, "model-x")

    def test_synthesized_flag_copied_from_original(self):
        # Picking a synthesized draft must carry synthesized=True onto the
        # chosen row, not just model_name.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="synth-text",
            chosen=False,
            model_name="synthesized",
            synthesized=True,
        )
        pa = mark_proposal_chosen(self.denial, "synth-text")
        self.assertTrue(pa.chosen)
        self.assertTrue(pa.synthesized)

    def test_synthesized_defaults_false_without_match(self):
        pa = mark_proposal_chosen(self.denial, "no-match-text")
        self.assertFalse(pa.synthesized)

    def test_no_match_falls_back_to_none(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="original-text",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertTrue(pa.chosen)
        self.assertIsNone(pa.model_name)

    def test_editted_flag_propagated(self):
        pa = mark_proposal_chosen(self.denial, "any-text", editted=True)
        self.assertTrue(pa.editted)

    def test_picks_most_recent_original_on_ties(self):
        # Two original rows with the same appeal_text but different model_name;
        # the most recent (highest id) wins.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="dup",
            chosen=False,
            model_name="model-old",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="dup",
            chosen=False,
            model_name="model-new",
        )
        pa = mark_proposal_chosen(self.denial, "dup")
        self.assertEqual(pa.model_name, "model-new")

    def test_id_lookup_survives_substitution(self):
        # Regression: sub_in_appeals rewrites the displayed text after
        # save_appeal persists the raw template. The id-based lookup must
        # still recover model_name even when the submitted text no longer
        # matches the saved row.
        original = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="raw with {claim_id} placeholder",
            chosen=False,
            model_name="model-y",
        )
        # User submits the substituted text plus the id from the JSON frame.
        pa = mark_proposal_chosen(
            self.denial,
            "raw with ABC123 placeholder",
            proposed_appeal_id=original.id,
        )
        self.assertEqual(pa.model_name, "model-y")
        self.assertEqual(pa.appeal_text, "raw with ABC123 placeholder")

    def test_id_lookup_ignores_wrong_denial(self):
        # An id that belongs to a different denial should not leak model_name.
        other_denial = Denial.objects.create(
            hashed_email="other",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        other_pa = ProposedAppeal.objects.create(
            for_denial=other_denial,
            appeal_text="other text",
            chosen=False,
            model_name="model-other",
        )
        pa = mark_proposal_chosen(
            self.denial,
            "totally different text",
            proposed_appeal_id=other_pa.id,
        )
        self.assertIsNone(pa.model_name)

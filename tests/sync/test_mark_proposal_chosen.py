"""Tests for common_view_logic.mark_proposal_chosen helper."""

import inspect

from django.test import TestCase

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.common_view_logic import (
    ChooseAppealHelper,
    mark_proposal_chosen,
)
from fighthealthinsurance.ml.model_identity import LEGACY_UNATTRIBUTED_LABEL
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

    def test_synthesized_flag_copied_via_id_lookup(self):
        # The preferred id-based lookup path must also carry synthesized, even
        # when sub_in_appeals has rewritten the displayed text.
        original = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="raw synthesized draft {claim_id}",
            chosen=False,
            model_name="synthesized",
            synthesized=True,
        )
        pa = mark_proposal_chosen(
            self.denial,
            "raw synthesized draft ABC123",
            proposed_appeal_id=original.id,
        )
        self.assertTrue(pa.synthesized)
        self.assertEqual(pa.model_name, "synthesized")

    def test_no_match_single_model_denial_infers_that_model(self):
        # sub_in_appeals rewrote the displayed text so no exact match exists,
        # but every draft for this denial came from one model - the pick is
        # attributable to it.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="original-text",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertTrue(pa.chosen)
        self.assertEqual(pa.model_name, "model-x")

    def test_no_match_multiple_models_falls_back_to_none(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text-a",
            chosen=False,
            model_name="model-a",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text-b",
            chosen=False,
            model_name="model-b",
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertTrue(pa.chosen)
        # Ambiguous - never guess between the candidate models.
        self.assertIsNone(pa.model_name)

    def test_no_match_unnamed_draft_blocks_inference(self):
        # A NULL-model draft could equally have been the pick's source, so
        # the sole-draft inference must not fire.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="named draft",
            chosen=False,
            model_name="model-x",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="legacy draft",
            chosen=False,
            model_name=None,
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertIsNone(pa.model_name)

    def test_editted_share_flow_never_infers(self):
        # The share-appeal flow submits arbitrary user text (arbitrary_text,
        # stored editted=True); even a single-model denial must not claim it.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="only draft",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(
            self.denial, "user authored text", editted=True, arbitrary_text=True
        )
        self.assertIsNone(pa.model_name)

    def test_blank_sole_draft_model_name_not_inferred(self):
        # model_name is blank=True; a blank/whitespace sole-draft label is
        # not usable evidence and must not be stamped onto the pick.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="blank-named draft",
            chosen=False,
            model_name="   ",
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertIsNone(pa.model_name)

    def test_sole_draft_inference_carries_synthesized_flag(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="synth draft",
            chosen=False,
            model_name="synthesized",
            synthesized=True,
        )
        pa = mark_proposal_chosen(self.denial, "rewritten synth pick")
        self.assertEqual(pa.model_name, "synthesized")
        self.assertTrue(pa.synthesized)

    def test_editted_flag_propagated(self):
        pa = mark_proposal_chosen(self.denial, "any-text", editted=True)
        self.assertTrue(pa.editted)

    def test_picks_most_recent_original_on_ties(self):
        # Two original rows with the same appeal_text but different model_name;
        # the most recent (highest id) wins. Identical un-chosen texts can only
        # exist as LEGACY data now (save() fingerprints every new row and the
        # unique constraint rejects the twin), so build the tie the way legacy
        # data actually exists: bulk_create bypasses save(), leaving NULL
        # fingerprints exactly like pre-constraint rows.
        ProposedAppeal.objects.bulk_create(
            [
                ProposedAppeal(
                    for_denial=self.denial,
                    appeal_text="dup",
                    chosen=False,
                    model_name="model-old",
                ),
                ProposedAppeal(
                    for_denial=self.denial,
                    appeal_text="dup",
                    chosen=False,
                    model_name="model-new",
                ),
            ]
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

    # --- context_level provenance (shed-level tracking) --------------------

    def test_context_level_copied_from_exact_match(self):
        # The chosen row must carry the draft's shed level, else the dashboard
        # /RL export (which read only chosen rows) are blind to it.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="lvl-text",
            chosen=False,
            model_name="model-x",
            context_level="tier1_shed",
        )
        pa = mark_proposal_chosen(self.denial, "lvl-text")
        self.assertEqual(pa.context_level, "tier1_shed")

    def test_context_level_copied_via_sole_draft_inference(self):
        for text in ("d1", "d2"):
            ProposedAppeal.objects.create(
                for_denial=self.denial,
                appeal_text=text,
                chosen=False,
                model_name="model-x",
                context_level="full",
            )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertEqual(pa.model_name, "model-x")
        self.assertEqual(pa.context_level, "full")

    def test_context_level_none_when_draft_levels_differ(self):
        # Same model, different shed tiers: the model is still inferable but the
        # level is ambiguous, so it must not be guessed.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="d1",
            chosen=False,
            model_name="model-x",
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="d2",
            chosen=False,
            model_name="model-x",
            context_level="tier2_shed",
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        self.assertEqual(pa.model_name, "model-x")
        self.assertIsNone(pa.context_level)

    def test_speculative_draft_excluded_from_sole_draft_inference(self):
        # A held-back speculative row is not a presented draft, so it must not
        # count toward (or block) the sole-draft model inference.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="real",
            chosen=False,
            model_name="model-x",
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="spec",
            chosen=False,
            model_name="model-spec",
            context_level="speculative",
            speculative=True,
        )
        pa = mark_proposal_chosen(self.denial, "edited-text")
        # Only the real draft counts -> inference still resolves to model-x.
        self.assertEqual(pa.model_name, "model-x")
        # ...and to its context level. Without the speculative exclusion the
        # two rows would look like mixed levels and this would infer None.
        self.assertEqual(pa.context_level, "full")
    # --- re-picks, CRLF submissions, unsaved drafts, edited picks ----------

    def test_repick_via_the_chosen_copys_id_keeps_the_model(self):
        # The appeals page replays a user's earlier pick under the chosen
        # copy's id; re-submitting it must not degrade to "(unattributed)".
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft-a",
            chosen=False,
            model_name="model-a",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft-b",
            chosen=False,
            model_name="model-b",
        )
        first = mark_proposal_chosen(self.denial, "draft-a")
        self.assertEqual(first.model_name, "model-a")
        again = mark_proposal_chosen(
            self.denial, "draft-a, lightly edited", proposed_appeal_id=first.id
        )
        self.assertEqual(again.model_name, "model-a")

    def test_chosen_copy_without_a_model_is_not_evidence(self):
        # An earlier unattributed copy must not pin a re-pick to None when the
        # denial's drafts can still say which model it was.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="only draft",
            chosen=False,
            model_name="model-x",
        )
        copy = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="whatever",
            chosen=True,
            model_name=None,
        )
        pa = mark_proposal_chosen(self.denial, "rewritten", proposed_appeal_id=copy.id)
        self.assertEqual(pa.model_name, "model-x")

    def test_text_match_survives_crlf_line_endings(self):
        # Browsers submit textarea content with CRLF while drafts are stored
        # with LF; a byte-for-byte comparison never matched a multi-line
        # letter, so an id-less pick fell through to inference -- None here,
        # with two models in play.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Dear Reviewer,\nI appeal.",
            chosen=False,
            model_name="model-x",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Other draft",
            chosen=False,
            model_name="model-y",
        )
        pa = mark_proposal_chosen(self.denial, "Dear Reviewer,\r\nI appeal.")
        self.assertEqual(pa.model_name, "model-x")

    def test_unsaved_draft_blocks_sole_draft_inference(self):
        # The browser says the picked draft was never stored: the stored
        # drafts say nothing about which model produced it.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="stored draft",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(
            self.denial, "the unsaved draft's text", draft_unsaved=True
        )
        self.assertIsNone(pa.model_name)

    def test_presented_ids_are_kept_filtered_to_the_denials_own_drafts(self):
        # The browser reports what was on screen; a stray id from another
        # denial (or a typo) must not credit that draft with a presentation.
        shown = ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text="shown", chosen=False, model_name="m"
        )
        other_denial = Denial.objects.create(
            hashed_email="other",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        foreign = ProposedAppeal.objects.create(
            for_denial=other_denial, appeal_text="foreign", chosen=False, model_name="m"
        )
        pa = mark_proposal_chosen(
            self.denial, "shown", presented_ids=[shown.id, foreign.id, 999999]
        )
        self.assertEqual(pa.presented_ids, [shown.id])

    def test_no_report_leaves_presented_ids_null(self):
        pa = mark_proposal_chosen(self.denial, "anything")
        self.assertIsNone(pa.presented_ids)

    def test_presented_ids_keep_the_on_screen_order_without_duplicates(self):
        # The page ranks its cards, so the order says which sat on top.
        first = ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text="first", chosen=False, model_name="m"
        )
        second = ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text="second", chosen=False, model_name="m"
        )
        pa = mark_proposal_chosen(
            self.denial, "first", presented_ids=[second.id, first.id, second.id]
        )
        self.assertEqual(pa.presented_ids, [second.id, first.id])

    def test_a_legacy_placeholder_on_a_chosen_copy_is_not_evidence(self):
        # The backfill stamps picks it could not attribute; the replay serves
        # chosen copies too, so a re-submit can echo such a copy's id. Copying
        # the placeholder would file a pick made today as a pre-tracking one.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="the draft",
            chosen=False,
            model_name="model-x",
        )
        legacy = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="the draft",
            chosen=True,
            model_name=LEGACY_UNATTRIBUTED_LABEL,
        )
        pa = mark_proposal_chosen(
            self.denial, "the draft", proposed_appeal_id=legacy.id
        )
        self.assertEqual(pa.model_name, "model-x")

    def _draft_from_model_x(self, text="the draft"):
        return ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text=text, chosen=False, model_name="model-x"
        )

    def test_a_verbatim_pick_is_not_an_edit_when_the_caller_cannot_say(self):
        draft = self._draft_from_model_x()
        pa = mark_proposal_chosen(
            self.denial, "the draft", proposed_appeal_id=draft.id, editted=None
        )
        self.assertFalse(pa.editted)

    def test_a_changed_pick_is_an_edit_of_the_same_model_when_the_caller_cannot_say(
        self,
    ):
        draft = self._draft_from_model_x()
        pa = mark_proposal_chosen(
            self.denial, "the draft, edited", proposed_appeal_id=draft.id, editted=None
        )
        self.assertTrue(pa.editted)
        self.assertEqual(pa.model_name, "model-x")

    def test_a_re_pick_of_an_edited_copy_stays_an_edit(self):
        # The replay serves chosen copies too, so a re-submit can echo the
        # copy's id; unchanged text keeps the copy's own answer rather than
        # reading as verbatim against the already-edited copy.
        self._draft_from_model_x()
        copy = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="the draft, edited",
            chosen=True,
            editted=True,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(
            self.denial, "the draft, edited", proposed_appeal_id=copy.id, editted=None
        )
        self.assertTrue(pa.editted)

    def test_edited_main_flow_pick_is_recorded_and_still_inferred(self):
        # editted only records the edit now; a draft edited from the sole
        # model's output is still that model's.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="only draft",
            chosen=False,
            model_name="model-x",
        )
        pa = mark_proposal_chosen(self.denial, "only draft, edited", editted=True)
        self.assertTrue(pa.editted)
        self.assertEqual(pa.model_name, "model-x")


class ChooseAppealCarriesTheBrowserFlagsTest(TestCase):
    """The hidden inputs the appeals page fills in (the draft's id, whether it
    was ever stored, whether it was edited) reach mark_proposal_chosen."""

    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("user@example.com"),
            semi_sekret="sekret",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def test_form_fields_match_the_helper_signature(self):
        form = core_forms.ChooseAppealForm(
            {
                "denial_id": str(self.denial.denial_id),
                "email": "user@example.com",
                "semi_sekret": "sekret",
                "appeal_text": "the letter",
                "proposed_appeal_id": "",
                "draft_unsaved": "1",
                "editted": "1",
                "presented_ids": "[12, 7, \"x\", 7]",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["draft_unsaved"])
        self.assertTrue(form.cleaned_data["editted"])
        self.assertIsNone(form.cleaned_data["proposed_appeal_id"])
        # Parsed to ints; junk entries dropped, nothing else rejected.
        self.assertEqual(form.cleaned_data["presented_ids"], [12, 7, 7])
        params = inspect.signature(ChooseAppealHelper.choose_appeal).parameters
        self.assertTrue(set(form.cleaned_data) <= set(params), form.cleaned_data)

    def test_unparseable_presented_ids_are_dropped_not_fatal(self):
        base = {
            "denial_id": str(self.denial.denial_id),
            "email": "user@example.com",
            "semi_sekret": "sekret",
            "appeal_text": "the letter",
        }
        # Also a JSON number that parses to inf and nesting past the parser's
        # depth: neither may 500 the pick.
        for raw in ("not json", "{\"a\": 1}", "", "[]", "[1e999]", "[" * 100000):
            form = core_forms.ChooseAppealForm({**base, "presented_ids": raw})
            self.assertTrue(form.is_valid(), (raw, form.errors))
            self.assertIsNone(form.cleaned_data["presented_ids"], raw)

    def test_helper_stamps_the_flags_on_the_chosen_row(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="stored draft",
            chosen=False,
            model_name="model-x",
        )
        ChooseAppealHelper.choose_appeal(
            denial_id=str(self.denial.denial_id),
            appeal_text="an unsaved draft, edited",
            email="user@example.com",
            semi_sekret="sekret",
            draft_unsaved=True,
            editted=True,
        )
        pick = ProposedAppeal.objects.get(for_denial=self.denial, chosen=True)
        self.assertTrue(pick.editted)
        # Unsaved: the stored draft is not evidence, so no inference.
        self.assertIsNone(pick.model_name)

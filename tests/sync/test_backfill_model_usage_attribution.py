"""Tests for the backfill_model_usage_attribution management command."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    legacy_unresolved_label,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    Denial,
    ProposedAppeal,
)

REPR_DEEPINFRA = (
    "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7f81456da840>"
)
REPR_INTERNAL = (
    "<fighthealthinsurance.ml.ml_models.NewRemoteInternal object at 0x7d65196f8bc0>"
)


def run_command(*args):
    out = StringIO()
    call_command("backfill_model_usage_attribution", *args, stdout=out)
    return out.getvalue()


class BackfillProposedAppealTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _denial(self, suffix):
        return Denial.objects.create(
            hashed_email=f"hash-{suffix}",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def test_recovers_via_exact_text_match(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=False,
            model_name="model-x",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=True,
            model_name=None,
        )
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "model-x")

    def test_recovers_synthesized_flag_with_text_match(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="synth text",
            chosen=False,
            model_name="synthesized",
            synthesized=True,
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="synth text",
            chosen=True,
            model_name=None,
        )
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "synthesized")
        self.assertTrue(pick.synthesized)

    def test_recovers_via_sole_draft_inference(self):
        # Text was rewritten by sub_in_appeals, but every draft for the
        # denial came from one model, so the pick is attributable.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="raw draft {claim_id}",
            chosen=False,
            model_name="only-model",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="another raw draft {claim_id}",
            chosen=False,
            model_name="only-model",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="raw draft ABC123",
            chosen=True,
            model_name=None,
        )
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "only-model")

    def test_does_not_guess_across_multiple_models(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft a",
            chosen=False,
            model_name="model-a",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft b",
            chosen=False,
            model_name="model-b",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="edited beyond recognition",
            chosen=True,
            model_name=None,
        )
        run_command("--apply")
        pick.refresh_from_db()
        # Ambiguous AND post-tracking (created_at set): never guessed onto
        # model-a/model-b, and left NULL so it reports as "(unattributed)"
        # rather than being mislabeled legacy.
        self.assertIsNone(pick.model_name)

    def test_does_not_sole_draft_infer_for_editted_rows(self):
        # editted=True rows come from the share-appeal flow and may contain
        # text that never was a draft; sole-draft inference must not fire.
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=False,
            model_name="only-model",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="user-authored text",
            chosen=True,
            editted=True,
            model_name=None,
        )
        run_command("--apply")
        pick.refresh_from_db()
        # Post-tracking + unrecoverable => left NULL, not force-labeled.
        self.assertIsNone(pick.model_name)

    def test_legacy_row_without_drafts_labeled_unattributed(self):
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="ancient pick",
            chosen=True,
            model_name=None,
        )
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=None)
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, LEGACY_UNATTRIBUTED_LABEL)

    def test_drafts_are_never_labeled(self):
        # Labeling chosen=False rows would fabricate a presented denominator
        # for the legacy bucket.
        draft = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="legacy draft",
            chosen=False,
            model_name=None,
        )
        run_command("--apply")
        draft.refresh_from_db()
        self.assertIsNone(draft.model_name)

    def test_blank_draft_model_name_not_copied(self):
        # model_name is blank=True. A matching draft whose model_name is an
        # empty/whitespace string is not usable evidence: the pick must not
        # inherit it (pre-tracking pick => labeled legacy instead).
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=False,
            model_name="  ",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=True,
            model_name=None,
        )
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=None)
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, LEGACY_UNATTRIBUTED_LABEL)

    def test_valid_names_never_overwritten(self):
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text",
            chosen=True,
            model_name="model-x",
        )
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "model-x")

    def test_malformed_angle_bracket_name_not_relabeled(self):
        # The target queryset prefilters on startswith("<") to catch object
        # reprs cheaply, but a non-repr value that merely starts with "<" is
        # not an object repr and must be left exactly as-is - even on a
        # pre-tracking row, it must not be swept into legacy-unattributed.
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text",
            chosen=True,
            model_name="<not-a-repr>",
        )
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=None)
        out = run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "<not-a-repr>")
        self.assertIn("rows changed: 0", out)

    def test_dry_run_by_default(self):
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="pick",
            chosen=True,
            model_name=None,
        )
        # Pre-tracking so it is a labelable legacy row (would change).
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=None)
        out = run_command()
        pick.refresh_from_db()
        self.assertIsNone(pick.model_name)
        self.assertIn("DRY RUN", out)
        self.assertIn("rows would change: 1", out)

    def test_idempotent_second_run_changes_nothing(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=False,
            model_name="model-x",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=True,
            model_name=None,
        )
        unrecoverable_denial = self._denial("legacy")
        ancient = ProposedAppeal.objects.create(
            for_denial=unrecoverable_denial,
            appeal_text="ancient",
            chosen=True,
            model_name=None,
        )
        # Pre-tracking: labeled legacy (post-tracking NULL picks stay NULL).
        ProposedAppeal.objects.filter(pk=ancient.pk).update(created_at=None)
        first = run_command("--apply")
        self.assertIn("rows changed: 2", first)
        second = run_command("--apply")
        # Both sections (ProposedAppeal + ChooserCandidate) report zero.
        self.assertEqual(second.count("rows changed: 0"), 2, second)
        # The label survives re-runs untouched.
        self.assertEqual(
            ProposedAppeal.objects.filter(model_name=LEGACY_UNATTRIBUTED_LABEL).count(),
            1,
        )

    def test_relabeled_row_recovers_when_evidence_appears(self):
        # legacy-unattributed rows are re-checked for recovery, so a rerun
        # can only improve attribution (it never un-labels to NULL).
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=True,
            model_name=LEGACY_UNATTRIBUTED_LABEL,
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="draft text",
            chosen=False,
            model_name="model-x",
        )
        run_command("--apply")
        pick.refresh_from_db()
        self.assertEqual(pick.model_name, "model-x")


class BackfillChooserCandidateTest(TestCase):
    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="EXHAUSTED", source="synthetic"
        )

    def _candidate(self, index, model_name, kind="appeal_letter"):
        return ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=index,
            kind=kind,
            model_name=model_name,
            content="x",
        )

    def test_object_reprs_normalized_to_class_label(self):
        c1 = self._candidate(0, REPR_DEEPINFRA)
        c2 = self._candidate(1, REPR_INTERNAL)
        run_command("--apply")
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.model_name, legacy_unresolved_label("DeepInfra"))
        self.assertEqual(c2.model_name, legacy_unresolved_label("NewRemoteInternal"))

    def test_clean_names_and_synthesized_untouched(self):
        clean = self._candidate(0, "fhi-legacy")
        synth = self._candidate(1, "synthesized")
        run_command("--apply")
        clean.refresh_from_db()
        synth.refresh_from_db()
        self.assertEqual(clean.model_name, "fhi-legacy")
        self.assertEqual(synth.model_name, "synthesized")

    def test_idempotent(self):
        self._candidate(0, REPR_DEEPINFRA)
        first = run_command("--apply")
        self.assertIn("rows changed: 1", first)
        second = run_command("--apply")
        # Both sections (ProposedAppeal + ChooserCandidate) report zero.
        self.assertEqual(second.count("rows changed: 0"), 2, second)

    def test_no_address_survives_backfill(self):
        self._candidate(0, REPR_DEEPINFRA)
        self._candidate(1, REPR_INTERNAL)
        run_command("--apply")
        for value in ChooserCandidate.objects.values_list("model_name", flat=True):
            self.assertNotIn("object at 0x", value)


class BackfillAuditTest(TestCase):
    def test_audit_is_read_only_and_reports_gaps(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        pick = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="pick",
            chosen=True,
            model_name=None,
        )
        out = run_command("--audit")
        pick.refresh_from_db()
        self.assertIsNone(pick.model_name)
        self.assertIn("chosen rows missing attribution: 1", out)
        self.assertIn("read-only", out)

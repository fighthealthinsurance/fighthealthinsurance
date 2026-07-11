"""Tests for the backfill_model_metadata management command.

The command may only rewrite model_name values whose canonical registry name
is unambiguously inferable; NULLs stay NULL (the dashboard's "(unknown)"
bucket) and ambiguous/unrecognized strings are left untouched and reported.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    Denial,
    ProposedAppeal,
)


class BackfillModelMetadataTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", source="synthetic"
        )

    def _make_appeal(self, model_name):
        return ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=f"text-{model_name}",
            model_name=model_name,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_model_metadata", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_but_does_not_write(self):
        legacy = self._make_appeal("RemoteAnthropic(claude-opus-4-8)")
        output = self._run()
        legacy.refresh_from_db()
        self.assertEqual(legacy.model_name, "RemoteAnthropic(claude-opus-4-8)")
        self.assertIn("DRY RUN", output)
        self.assertIn("anthropic/claude-opus-4-8", output)

    def test_repairs_class_descriptor_to_registry_name(self):
        legacy = self._make_appeal("RemoteAnthropic(claude-opus-4-8)")
        output = self._run("--apply")
        legacy.refresh_from_db()
        self.assertEqual(legacy.model_name, "anthropic/claude-opus-4-8")
        self.assertIn("repaired=1", output)

    def test_repairs_object_repr_for_single_model_backend(self):
        # RemotePerplexity exposes exactly one catalog model ("sonar"), so an
        # old object repr is unambiguous.
        legacy = self._make_appeal(
            "<fighthealthinsurance.ml.ml_models.RemotePerplexity object at 0x7f3a2b9d>"
        )
        self._run("--apply")
        legacy.refresh_from_db()
        self.assertEqual(legacy.model_name, "sonar")

    def test_object_repr_for_multi_model_backend_is_ambiguous(self):
        # RemoteAnthropic has three catalog models — which one produced the
        # row is unknowable, so it must be left alone.
        legacy = self._make_appeal(
            "<fighthealthinsurance.ml.ml_models.RemoteAnthropic object at 0x7f11>"
        )
        output = self._run("--apply")
        legacy.refresh_from_db()
        self.assertIn("object at 0x7f11", legacy.model_name)
        self.assertIn("ambiguous", output)

    def test_bare_wire_id_repaired_only_when_unique(self):
        # claude-haiku-4-5-20251001 exists only in the direct Anthropic
        # catalog -> repaired. claude-sonnet-4-6 is both an anthropic wire id
        # and an azure-anthropic deployment name -> ambiguous, untouched.
        unique = self._make_appeal("claude-haiku-4-5-20251001")
        ambiguous = self._make_appeal("claude-sonnet-4-6")
        self._run("--apply")
        unique.refresh_from_db()
        ambiguous.refresh_from_db()
        self.assertEqual(unique.model_name, "anthropic/claude-haiku-4-5")
        self.assertEqual(ambiguous.model_name, "claude-sonnet-4-6")

    def test_null_rows_left_for_unknown_bucket(self):
        row = self._make_appeal(None)
        output = self._run("--apply")
        row.refresh_from_db()
        self.assertIsNone(row.model_name)
        self.assertIn("left_unknown(null)=1", output)

    def test_canonical_and_synthesized_names_untouched(self):
        canonical = self._make_appeal("anthropic/claude-sonnet-4-6")
        synthesized = self._make_appeal("synthesized")
        self._run("--apply")
        canonical.refresh_from_db()
        synthesized.refresh_from_db()
        self.assertEqual(canonical.model_name, "anthropic/claude-sonnet-4-6")
        self.assertEqual(synthesized.model_name, "synthesized")

    def test_unrecognized_names_reported_not_touched(self):
        weird = self._make_appeal("totally-made-up-model")
        output = self._run("--apply")
        weird.refresh_from_db()
        self.assertEqual(weird.model_name, "totally-made-up-model")
        self.assertIn("unclassifiable=1", output)

    def test_chooser_candidates_also_repaired(self):
        candidate = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="RemoteAnthropic(claude-sonnet-4-6)",
            content="draft",
        )
        self._run("--apply")
        candidate.refresh_from_db()
        self.assertEqual(candidate.model_name, "anthropic/claude-sonnet-4-6")

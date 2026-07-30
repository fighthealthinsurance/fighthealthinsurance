"""Tests for persisting per-model-call attempts against a denial.

These rows hold model response text, which is derived from the denial letter
and therefore PHI. Two properties matter more than anything else here:

1. they are deleted with the denial (FK CASCADE), including via the
   privacy-compliance path, and
2. writing them can never break appeal generation.

The rest of the suite covers the flush cursor (make_appeals flushes
synchronously mid-run, the streaming flow flushes again at the end, and
neither may duplicate or drop a record).
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.ml.model_attempt_log import (
    MAX_RESPONSE_CHARS,
    ModelAttemptRecord,
    ModelAttemptRecorder,
    summarize_model_outcomes,
)
from fighthealthinsurance.models import Denial, ModelCallAttempt


class ModelCallAttemptPersistenceTest(TestCase):
    EMAIL = "attempts@example.com"

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_text="denial letter",
            hashed_email=Denial.get_hashed_email(self.EMAIL),
        )

    def _recorder(self, **kwargs):
        return ModelAttemptRecorder(
            denial_id=self.denial.denial_id,
            generation_id="abc123def456",
            **kwargs,
        )

    def test_flush_persists_the_full_attempt_detail(self):
        recorder = self._recorder()
        recorder.record(
            ModelAttemptRecord(
                model_name="fhi-legacy",
                outcome="runt_only",
                stage="backup",
                context_level="tier1_shed",
                infer_type="full",
                backend="RemoteOpen(api_base=http://x)",
                error_detail="too short (12 chars)",
                response_text="I cannot help with that.",
                response_chars=24,
                prompt_tokens_est=5184,
                duration_ms=41231,
            )
        )
        self.assertEqual(recorder.flush(), 1)

        row = ModelCallAttempt.objects.get()
        self.assertEqual(row.for_denial_id, self.denial.denial_id)
        self.assertEqual(row.generation_id, "abc123def456")
        self.assertEqual(row.run_kind, "live")
        self.assertEqual(row.model_name, "fhi-legacy")
        self.assertEqual(row.stage, "backup")
        self.assertEqual(row.context_level, "tier1_shed")
        self.assertEqual(row.outcome, "runt_only")
        self.assertEqual(row.error_detail, "too short (12 chars)")
        # The whole point: what the model actually said is recoverable.
        self.assertEqual(row.response_text, "I cannot help with that.")
        self.assertEqual(row.prompt_tokens_est, 5184)
        self.assertEqual(row.duration_ms, 41231)

    def test_run_kind_separates_speculative_from_live(self):
        """A denial has both a background precompute and a live run; without
        run_kind their attempts are indistinguishable."""
        spec = self._recorder(run_kind="speculative")
        spec.record(ModelAttemptRecord(model_name="m", outcome="ok"))
        spec.flush()
        live = self._recorder()
        live.record(ModelAttemptRecord(model_name="m", outcome="no_output"))
        live.flush()

        self.assertEqual(
            ModelCallAttempt.objects.filter(run_kind="speculative").count(), 1
        )
        self.assertEqual(ModelCallAttempt.objects.filter(run_kind="live").count(), 1)

    def test_unnamed_model_is_recorded_as_unknown(self):
        recorder = self._recorder()
        recorder.record(ModelAttemptRecord(model_name=None, outcome="no_output"))
        recorder.flush()
        self.assertEqual(ModelCallAttempt.objects.get().model_name, "unknown")

    def test_oversized_response_is_truncated_but_true_length_kept(self):
        """A runaway model must not be able to write megabytes per attempt, but
        the row still has to say how much it produced."""
        huge = "x" * (MAX_RESPONSE_CHARS + 5000)
        recorder = self._recorder()
        recorder.record(
            ModelAttemptRecord(
                model_name="chatty",
                outcome="ok",
                response_text=huge,
                response_chars=len(huge),
            )
        )
        recorder.flush()

        row = ModelCallAttempt.objects.get()
        self.assertLess(len(row.response_text), len(huge))
        self.assertIn("truncated", row.response_text)
        self.assertEqual(row.response_chars, len(huge))

    def test_over_long_field_values_do_not_break_the_write(self):
        """Backend reprs and model names come from configuration, so an
        over-long one must clip rather than raise mid-generation."""
        recorder = self._recorder()
        recorder.record(
            ModelAttemptRecord(
                model_name="m" * 500,
                outcome="o" * 200,
                backend="b" * 900,
                stage="s" * 100,
            )
        )
        self.assertEqual(recorder.flush(), 1)
        row = ModelCallAttempt.objects.get()
        self.assertEqual(len(row.model_name), 200)
        self.assertEqual(len(row.backend), 300)


class ModelAttemptFlushCursorTest(TestCase):
    """make_appeals flushes synchronously mid-run and the streaming flow
    flushes again once every generator has drained, so the cursor has to make
    the second flush pick up exactly the new records."""

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_text="d", hashed_email=Denial.get_hashed_email("cursor@example.com")
        )
        self.recorder = ModelAttemptRecorder(
            denial_id=self.denial.denial_id, generation_id="g"
        )

    def test_second_flush_writes_nothing_when_no_new_records(self):
        self.recorder.record(ModelAttemptRecord(model_name="a", outcome="ok"))
        self.assertEqual(self.recorder.flush(), 1)
        self.assertEqual(self.recorder.flush(), 0)
        self.assertEqual(ModelCallAttempt.objects.count(), 1)

    def test_late_records_are_picked_up_by_the_second_flush(self):
        self.recorder.record(ModelAttemptRecord(model_name="a", outcome="no_output"))
        self.recorder.flush()
        # A generator that was still lazily chained when make_appeals returned.
        self.recorder.record(ModelAttemptRecord(model_name="b", outcome="ok"))
        self.assertEqual(self.recorder.flush(), 1)
        self.assertEqual(
            sorted(ModelCallAttempt.objects.values_list("model_name", flat=True)),
            ["a", "b"],
        )

    def test_flush_without_a_denial_id_is_a_noop(self):
        """The speculative path can run with no denial in hand; recording must
        stay harmless rather than writing an orphan row."""
        recorder = ModelAttemptRecorder(denial_id=None)
        recorder.record(ModelAttemptRecord(model_name="a", outcome="ok"))
        self.assertEqual(recorder.flush(), 0)
        self.assertEqual(ModelCallAttempt.objects.count(), 0)

    def test_db_failure_is_swallowed_and_does_not_retry(self):
        """Diagnostics must never break generation, and a half-written batch
        must not be re-attempted on the next flush."""
        self.recorder.record(ModelAttemptRecord(model_name="a", outcome="ok"))
        with patch(
            "fighthealthinsurance.models.ModelCallAttempt.objects.bulk_create",
            side_effect=RuntimeError("db exploded"),
        ):
            self.assertEqual(self.recorder.flush(), 0)
        # Consumed, not retried.
        self.assertEqual(self.recorder.flush(), 0)
        self.assertEqual(ModelCallAttempt.objects.count(), 0)

    def test_models_tried_summary_covers_everything_recorded(self):
        self.recorder.record(ModelAttemptRecord(model_name="a", outcome="no_output"))
        self.recorder.record(ModelAttemptRecord(model_name="b", outcome="ok"))
        self.assertEqual(self.recorder.models_tried_summary(), "a:no_output,b:ok")


class ModelCallAttemptDeletionTest(TestCase):
    """The rows carry PHI-derived model output, so their lifetime must be the
    denial's. These are the tests that make holding that data acceptable."""

    EMAIL = "deleteme@example.com"

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_text="d", hashed_email=Denial.get_hashed_email(self.EMAIL)
        )
        self.other = Denial.objects.create(
            denial_text="other", hashed_email=Denial.get_hashed_email("keep@x.com")
        )
        for denial in (self.denial, self.other):
            ModelCallAttempt.objects.create(
                for_denial=denial,
                outcome="no_output",
                response_text="PHI-derived model output",
            )

    def test_deleting_the_denial_deletes_its_attempts(self):
        self.denial.delete()
        self.assertFalse(
            ModelCallAttempt.objects.filter(
                for_denial_id=self.denial.denial_id
            ).exists()
        )

    def test_deleting_a_denial_queryset_deletes_its_attempts(self):
        """RemoveDataHelper deletes through a queryset, which is a different
        code path in Django than instance .delete()."""
        Denial.objects.filter(denial_id=self.denial.denial_id).delete()
        self.assertEqual(ModelCallAttempt.objects.count(), 1)

    def test_remove_data_for_email_deletes_attempts(self):
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertFalse(
            ModelCallAttempt.objects.filter(
                for_denial_id=self.denial.denial_id
            ).exists()
        )

    def test_unrelated_denials_keep_their_attempts(self):
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertTrue(
            ModelCallAttempt.objects.filter(for_denial_id=self.other.denial_id).exists()
        )


class MakeAppealsPersistsAttemptsTest(TestCase):
    """End-to-end: a real make_appeals run that produces nothing must leave a
    complete record behind, because that is the run nobody can debug from the
    logs. The zero-appeal ladder drains every stage, so the synchronous flush
    inside make_appeals is enough on its own -- no streaming flow required."""

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_text="denial letter text",
            hashed_email=Denial.get_hashed_email("e2e@example.com"),
            procedure="px",
            diagnosis="dx",
        )

    def _run(self, models_by_name, names):
        from fighthealthinsurance.generate_appeal import (
            AppealGenerator,
            AppealTemplateGenerator,
        )

        sink: dict = {}
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=lambda use_external=False: names,
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new=models_by_name,
        ), patch(
            # The failure ladder sleeps a second between primary+backup and the
            # shed retries.
            "fighthealthinsurance.generate_appeal.time.sleep"
        ):
            list(
                AppealGenerator().make_appeals(
                    self.denial,
                    AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"]),
                    medical_reasons=[],
                    non_ai_appeals=[],
                    generation_id="deadbeef1234",
                    diagnostics_sink=sink,
                )
            )
        return sink

    def test_unregistered_model_is_persisted_with_the_reason(self):
        self._run(models_by_name={}, names=["ghost-model"])

        rows = ModelCallAttempt.objects.filter(for_denial=self.denial)
        self.assertTrue(rows.exists())
        row = rows.filter(model_name="ghost-model").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.outcome, "not_registered")
        self.assertEqual(row.generation_id, "deadbeef1234")
        self.assertIn("models_by_name", row.error_detail)

    def test_undeliverable_response_text_is_persisted(self):
        """The runt a model returned is what tells you whether it refused, got
        cut off, or answered in the wrong language -- and it is dropped by the
        pipeline, so the row is the only copy."""
        backend = MagicMock()
        backend.__str__ = lambda self: "FakeBackend(api_base=http://fake)"
        backend.infer.return_value = [("full", "no.")]

        self._run(models_by_name={"runty": [backend]}, names=["runty"])

        rows = list(ModelCallAttempt.objects.filter(for_denial=self.denial))
        self.assertTrue(rows)
        # The item that made each stage fall through, recorded by the peek --
        # the row that would otherwise be missing, since peeking pulls one item
        # without exhausting the generator that produced it.
        peek_rows = [r for r in rows if r.outcome == "rejected_at_peek"]
        self.assertTrue(peek_rows, [r.outcome for r in rows])
        self.assertEqual(peek_rows[0].response_text, "no.")
        self.assertEqual(peek_rows[0].model_name, "runty")
        # Every stage of the ladder is represented, so it can be replayed: the
        # same model is retried at backup and at both shed tiers when nothing
        # deliverable comes back.
        self.assertEqual(
            {r.stage for r in peek_rows},
            {"primary", "backup", "retry_tier_1", "retry_tier_2"},
        )
        # And the drained generators record the call-level view, with timing.
        runt_rows = [r for r in rows if r.outcome == "runt_only"]
        self.assertTrue(runt_rows, [r.outcome for r in rows])
        self.assertTrue(all(r.duration_ms is not None for r in runt_rows))
        self.assertTrue(all(r.started_at is not None for r in runt_rows))

    def test_backend_exception_is_persisted_as_error_with_detail(self):
        backend = MagicMock()
        backend.infer.side_effect = TimeoutError()

        self._run(models_by_name={"flaky": [backend]}, names=["flaky"])

        rows = ModelCallAttempt.objects.filter(for_denial=self.denial, outcome="error")
        self.assertTrue(rows.exists())
        self.assertIn("timed out", rows.first().error_detail)

    def test_persisted_attempts_are_deleted_with_the_denial(self):
        """The property that makes storing response text acceptable, checked
        against rows the real pipeline wrote rather than hand-built ones."""
        self._run(models_by_name={}, names=["ghost-model"])
        self.assertTrue(ModelCallAttempt.objects.exists())

        self.denial.delete()
        self.assertEqual(ModelCallAttempt.objects.count(), 0)


class SummarizeModelOutcomesLocationTest(TestCase):
    """The summary helper moved next to the recorder that owns the records, but
    generate_appeal is where callers and tests have always imported it from."""

    def test_generate_appeal_still_exports_it(self):
        from fighthealthinsurance.generate_appeal import _summarize_model_outcomes

        self.assertIs(_summarize_model_outcomes, summarize_model_outcomes)

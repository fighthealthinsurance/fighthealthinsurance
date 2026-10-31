import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.ml.ml_metrics import ML_CALL_PURPOSE
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.ml.ml_router import MLRouter


class TestBestInternalModel(unittest.TestCase):
    """Tests for MLRouter.best_internal_model method."""

    def setUp(self):
        self.router = MLRouter()

    def test_returns_none_when_no_internal_models(self):
        self.router.internal_models_by_cost = []
        result = self.router.best_internal_model()
        self.assertIsNone(result)

    def test_returns_highest_quality_model(self):
        low_quality = MagicMock(spec=RemoteModelLike)
        low_quality.quality.return_value = 100

        mid_quality = MagicMock(spec=RemoteModelLike)
        mid_quality.quality.return_value = 200

        high_quality = MagicMock(spec=RemoteModelLike)
        high_quality.quality.return_value = 210

        # internal_models_by_cost is sorted by cost, not quality
        self.router.internal_models_by_cost = [low_quality, mid_quality, high_quality]
        result = self.router.best_internal_model()
        self.assertIs(result, high_quality)

    def test_works_with_single_model(self):
        single = MagicMock(spec=RemoteModelLike)
        single.quality.return_value = 150
        self.router.internal_models_by_cost = [single]
        result = self.router.best_internal_model()
        self.assertIs(result, single)


class TestSynthesizeAppeals(unittest.TestCase):
    """Tests for AppealGenerator.synthesize_appeals method."""

    def setUp(self):
        self.generator = AppealGenerator()
        self.sample_appeals = [
            "Dear Insurance Company, I am writing to appeal the denial of coverage for knee replacement surgery...",
            "To Whom It May Concern, This letter serves as a formal appeal regarding the denied claim for total knee arthroplasty...",
        ]

    async def _run_synthesize(self, **kwargs):
        return await self.generator.synthesize_appeals(**kwargs)

    def _purpose_seen_by_synthesis(self, mock_router, mock_best_within, **kwargs):
        seen = []

        async def infer(**_kwargs):
            seen.append(ML_CALL_PURPOSE.get())
            return "A" * 100

        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = infer
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        async def call_first_task(tasks, **_kwargs):
            return await tasks[0]

        mock_best_within.side_effect = call_first_task
        asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals, **kwargs))
        return seen

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_synthesis_calls_are_appeal_traffic_by_default(
        self, mock_router, mock_best_within
    ):
        seen = self._purpose_seen_by_synthesis(mock_router, mock_best_within)
        self.assertEqual(seen, ["appeal"])

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_the_chooser_keeps_its_synthesis_out_of_the_appeal_series(
        self, mock_router, mock_best_within
    ):
        seen = self._purpose_seen_by_synthesis(
            mock_router, mock_best_within, purpose="other"
        )
        self.assertEqual(seen, ["other"])

    def test_returns_none_with_empty_list(self):
        result = asyncio.run(self._run_synthesize(appeal_texts=[]))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_when_no_models_available(self, mock_router):
        mock_router.synthesis_backends.return_value = ([], [])
        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_synthesized_text_on_success(self, mock_router, mock_best_within):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        synthesized_text = "This is a well-crafted synthesized appeal letter that combines all the best arguments from the drafts provided."
        mock_best_within.return_value = (synthesized_text, mock_model)

        result = asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text="Your claim has been denied.",
                procedure="Knee replacement",
                diagnosis="Osteoarthritis",
            )
        )
        self.assertEqual(result, synthesized_text)
        mock_best_within.assert_called_once()
        # Verify timeout=60
        call_kwargs = mock_best_within.call_args.kwargs
        self.assertEqual(call_kwargs["timeout"], 60)
        self.assertEqual(AppealGenerator.SYNTHESIS_TIMEOUT_SECONDS, 60)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_when_result_too_short(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="Too short")
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_on_all_models_failing(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(
            side_effect=Exception("Model exploded")
        )
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_tries_all_internal_models(self, mock_router, mock_best_within):
        """Verify that all internal models are attempted, not just the best one."""
        model_a = MagicMock(spec=RemoteModelLike)
        model_b = MagicMock(spec=RemoteModelLike)
        model_c = MagicMock(spec=RemoteModelLike)
        mock_router.synthesis_backends.return_value = (
            [model_a, model_b, model_c],
            [],
        )

        mock_best_within.return_value = ("A" * 100, model_a)

        asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))

        # best_within_timelimit should receive 3 tasks (one per model)
        tasks_arg = mock_best_within.call_args[0][0]
        self.assertEqual(len(tasks_arg), 3)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_prompt_includes_all_drafts_and_context(
        self, mock_router, mock_best_within
    ):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        denial_text = "Your claim for knee replacement has been denied."
        procedure = "Total knee arthroplasty"
        diagnosis = "Severe osteoarthritis"

        # Make best_within_timelimit call through to the actual coroutine
        # so we can verify the model's _infer_no_context was called with the right prompt
        async def call_first_task(tasks, **_kwargs):
            result = await tasks[0]
            return result

        mock_best_within.side_effect = call_first_task

        asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text=denial_text,
                procedure=procedure,
                diagnosis=diagnosis,
            )
        )

        # Verify the model call was made
        mock_model._infer_no_context.assert_called_once()
        call_kwargs = mock_model._infer_no_context.call_args.kwargs

        # Check system prompt
        self.assertEqual(len(call_kwargs["system_prompts"]), 1)
        self.assertIn("synthesize", call_kwargs["system_prompts"][0].lower())

        # Check prompt includes all drafts
        prompt = call_kwargs["prompt"]
        for appeal in self.sample_appeals:
            self.assertIn(appeal, prompt)

        # Check prompt includes context
        self.assertIn(denial_text, prompt)
        self.assertIn(procedure, prompt)
        self.assertIn(diagnosis, prompt)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_uses_low_temperature(self, mock_router, mock_best_within):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        async def call_first_task(tasks, **_kwargs):
            return await tasks[0]

        mock_best_within.side_effect = call_first_task

        asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))

        call_kwargs = mock_model._infer_no_context.call_args.kwargs
        self.assertEqual(call_kwargs["temperature"], 0.3)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_denial_text_truncated_at_3000_chars(self, mock_router, mock_best_within):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        async def call_first_task(tasks, **_kwargs):
            return await tasks[0]

        mock_best_within.side_effect = call_first_task

        long_denial = "X" * 5000

        asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text=long_denial,
            )
        )

        prompt = mock_model._infer_no_context.call_args.kwargs["prompt"]
        # Boundary-aware truncation keeps total length <= 3000 and marks
        # truncation with an ellipsis. For an input with no sentence/word
        # boundaries (all X's), the helper hard-cuts and appends "...",
        # so the prompt contains 2997 X's followed by "..." rather than
        # the full 5000-char input.
        self.assertIn("X" * 2997 + "...", prompt)
        self.assertNotIn("X" * 3001, prompt)

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_selects_top_drafts_when_many_provided(self, mock_router, mock_best_within):
        """When more than MAX_SYNTHESIS_DRAFTS are provided, only the top-scored are used."""
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.synthesis_backends.return_value = ([mock_model], [])

        async def call_first_task(tasks, **_kwargs):
            return await tasks[0]

        mock_best_within.side_effect = call_first_task

        # Create 5 drafts with varying quality — high-quality ones have keywords
        good_1 = (
            "This appeal presents strong evidence of medical necessity and clinical support for the policy. "
            * 10
        )
        good_2 = (
            "We appeal this denial based on clinical evidence and established medical policy guidelines. "
            * 10
        )
        good_3 = (
            "The evidence clearly shows this is medically necessary per the clinical policy. "
            * 10
        )
        weak_1 = "Hello world this is a short filler text without relevant terms. " * 3
        weak_2 = "Another filler draft that says nothing useful at all. " * 3

        all_drafts = [weak_1, good_1, weak_2, good_2, good_3]

        asyncio.run(
            self._run_synthesize(
                appeal_texts=all_drafts,
            )
        )

        prompt = mock_model._infer_no_context.call_args.kwargs["prompt"]
        # Should have exactly 3 drafts in the prompt
        self.assertEqual(prompt.count("--- DRAFT"), 3)
        # The good drafts should be included, the weak ones excluded
        self.assertIn(good_1, prompt)
        self.assertIn(good_2, prompt)
        self.assertIn(good_3, prompt)
        self.assertNotIn(weak_1, prompt)
        self.assertNotIn(weak_2, prompt)

    # --- the hosted backup -------------------------------------------------
    # The 2026-10-31 log: fhi-legacy (appeal-only) answered the synthesis
    # prompt with "\n\n0.000", the newer internal model was skipped as not
    # served here, and with no other model asked the user got no merged
    # letter. Now the appeal-only fine-tune is not asked at all (router
    # tests), and with the user's consent the hosted models stand by.

    @staticmethod
    def _backend(reply, *, quality=200, name="model"):
        model = MagicMock(spec=RemoteModelLike, name=name)
        model.quality.return_value = quality
        model._infer_no_context = AsyncMock(return_value=reply)
        return model

    LETTER = (
        "Dear Appeals Department, I am writing to appeal the denial of my "
        "transesophageal echocardiogram; the evidence of medical necessity "
        "follows. " * 3
    )

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_hosted_model_merges_when_ours_answer_with_a_stray_number(
        self, mock_router
    ):
        ours = self._backend("\n\n0.000", quality=101, name="fhi-legacy")
        hosted = self._backend(self.LETTER, quality=210, name="hosted")
        mock_router.synthesis_backends.return_value = ([ours], [hosted])
        provenance: dict = {}

        result = asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                provenance=provenance,
                use_external=True,
            )
        )

        self.assertEqual(result, self.LETTER)
        self.assertIs(provenance["model"], hosted)
        ours._infer_no_context.assert_awaited_once()
        hosted._infer_no_context.assert_awaited_once()

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_hosted_models_never_start_when_one_of_ours_answers(self, mock_router):
        """A hosted call is a paid call over every draft: not made when our
        own model has already merged them."""
        ours = self._backend(self.LETTER, name="ours")
        hosted = self._backend("A hosted letter. " * 20, quality=210, name="hosted")
        mock_router.synthesis_backends.return_value = ([ours], [hosted])

        result = asyncio.run(
            self._run_synthesize(appeal_texts=self.sample_appeals, use_external=True)
        )

        self.assertEqual(result, self.LETTER)
        hosted._infer_no_context.assert_not_awaited()

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_hosted_tasks_are_held_back_behind_ours(
        self, mock_router, mock_best_within
    ):
        ours = self._backend(self.LETTER, name="ours")
        hosted = [
            self._backend(self.LETTER, quality=210, name="hosted-1"),
            self._backend(self.LETTER, quality=205, name="hosted-2"),
        ]
        mock_router.synthesis_backends.return_value = ([ours], hosted)
        seen = {}

        async def record(tasks, **kwargs):
            seen["tasks"] = list(tasks)
            seen.update(kwargs)
            for task in tasks:
                task.close()
            return None

        mock_best_within.side_effect = record

        asyncio.run(
            self._run_synthesize(appeal_texts=self.sample_appeals, use_external=True)
        )

        # Ours first, then the hosted ones, which are the held-back tasks.
        self.assertEqual(len(seen["tasks"]), 3)
        self.assertEqual(list(seen["deferred"]), seen["tasks"][1:])
        self.assertEqual(
            seen["defer_seconds"], AppealGenerator.SYNTHESIS_HOSTED_DELAY_SECONDS
        )
        self.assertLess(
            AppealGenerator.SYNTHESIS_HOSTED_DELAY_SECONDS,
            AppealGenerator.SYNTHESIS_TIMEOUT_SECONDS,
        )

    @patch("fighthealthinsurance.generate_appeal.best_within_timelimit")
    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_nothing_is_held_back_when_only_hosted_models_remain(
        self, mock_router, mock_best_within
    ):
        """With none of ours to go first there is nothing to wait for."""
        hosted = self._backend(self.LETTER, quality=210, name="hosted")
        mock_router.synthesis_backends.return_value = ([], [hosted])
        seen = {}

        async def record(tasks, **kwargs):
            seen.update(kwargs)
            for task in tasks:
                task.close()
            return None

        mock_best_within.side_effect = record

        asyncio.run(
            self._run_synthesize(appeal_texts=self.sample_appeals, use_external=True)
        )

        self.assertEqual(seen["defer_seconds"], 0.0)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_consent_is_passed_to_the_router_and_defaults_to_no(self, mock_router):
        mock_router.synthesis_backends.return_value = ([], [])

        asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        mock_router.synthesis_backends.assert_called_once_with(use_external=False)

        mock_router.synthesis_backends.reset_mock()
        asyncio.run(
            self._run_synthesize(appeal_texts=self.sample_appeals, use_external=True)
        )
        mock_router.synthesis_backends.assert_called_once_with(use_external=True)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_a_stray_number_from_every_model_yields_none(self, mock_router):
        ours = self._backend("\n\n0.000", quality=101, name="fhi-legacy")
        mock_router.synthesis_backends.return_value = ([ours], [])

        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))

        self.assertIsNone(result)

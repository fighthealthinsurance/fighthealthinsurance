import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fighthealthinsurance.generate_appeal import AppealGenerator
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

    def test_returns_none_with_empty_list(self):
        result = asyncio.run(self._run_synthesize(appeal_texts=[]))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_when_no_model_available(self, mock_router):
        mock_router.best_internal_model.return_value = None
        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_synthesized_text_on_success(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        synthesized_text = "This is a well-crafted synthesized appeal letter that combines all the best arguments from the drafts provided."
        mock_model._infer_no_context = AsyncMock(return_value=synthesized_text)
        mock_router.best_internal_model.return_value = mock_model

        result = asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text="Your claim has been denied.",
                procedure="Knee replacement",
                diagnosis="Osteoarthritis",
            )
        )
        self.assertEqual(result, synthesized_text)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_when_result_too_short(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="Too short")
        mock_router.best_internal_model.return_value = mock_model

        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_returns_none_on_exception(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(
            side_effect=Exception("Model exploded")
        )
        mock_router.best_internal_model.return_value = mock_model

        result = asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))
        self.assertIsNone(result)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_prompt_includes_all_drafts_and_context(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(
            return_value="A" * 100  # Long enough to pass the 50-char check
        )
        mock_router.best_internal_model.return_value = mock_model

        denial_text = "Your claim for knee replacement has been denied."
        procedure = "Total knee arthroplasty"
        diagnosis = "Severe osteoarthritis"

        asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text=denial_text,
                procedure=procedure,
                diagnosis=diagnosis,
            )
        )

        # Verify the call was made
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

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_uses_low_temperature(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.best_internal_model.return_value = mock_model

        asyncio.run(self._run_synthesize(appeal_texts=self.sample_appeals))

        call_kwargs = mock_model._infer_no_context.call_args.kwargs
        self.assertEqual(call_kwargs["temperature"], 0.3)

    @patch("fighthealthinsurance.generate_appeal.ml_router")
    def test_denial_text_truncated_at_3000_chars(self, mock_router):
        mock_model = MagicMock(spec=RemoteModelLike)
        mock_model._infer_no_context = AsyncMock(return_value="A" * 100)
        mock_router.best_internal_model.return_value = mock_model

        long_denial = "X" * 5000

        asyncio.run(
            self._run_synthesize(
                appeal_texts=self.sample_appeals,
                denial_text=long_denial,
            )
        )

        prompt = mock_model._infer_no_context.call_args.kwargs["prompt"]
        # Should contain the truncated version (3000 chars), not the full 5000
        self.assertIn("X" * 3000, prompt)
        self.assertNotIn("X" * 3001, prompt)

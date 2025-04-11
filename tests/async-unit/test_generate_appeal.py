import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike


class TestAppealQuestionsGeneration(unittest.TestCase):
    """Tests for the question generation functionality in RemoteFullOpenLike."""

    def setUp(self):
        # Create a mock RemoteFullOpenLike instance
        self.model = MagicMock(spec=RemoteFullOpenLike)
        # Set up _infer_no_context as AsyncMock
        self.model._infer_no_context = AsyncMock()
        # Set get_system_prompts to return a test prompt
        self.model.get_system_prompts = MagicMock(return_value=["Test system prompt"])

    @pytest.mark.asyncio
    async def test_get_appeal_questions_basic(self):
        """Test basic question generation with different response formats."""
        # Mock the _infer_no_context response for a simple formatted output
        self.model._infer_no_context.return_value = """
        1. What medical evidence supports the necessity of this treatment? Clinical studies show efficacy
        2. Has the patient tried alternative treatments? No alternatives attempted
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0][0],
            "What medical evidence supports the necessity of this treatment?",
        )
        self.assertEqual(result[0][1], "Clinical studies show efficacy")
        self.assertEqual(result[1][0], "Has the patient tried alternative treatments?")
        self.assertEqual(result[1][1], "No alternatives attempted")

    @pytest.mark.asyncio
    async def test_get_appeal_questions_markdown_format(self):
        """Test question generation with markdown formatted output."""
        # Mock the _infer_no_context response for markdown formatted output
        self.model._infer_no_context.return_value = """
        **What medical evidence supports the necessity of this treatment?** Clinical studies show efficacy
        **Has the patient tried alternative treatments?** No alternatives attempted
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0][0],
            "What medical evidence supports the necessity of this treatment?",
        )
        self.assertEqual(result[0][1], "Clinical studies show efficacy")
        self.assertEqual(result[1][0], "Has the patient tried alternative treatments?")
        self.assertEqual(result[1][1], "No alternatives attempted")

    @pytest.mark.asyncio
    async def test_get_appeal_questions_multi_questions_per_line(self):
        """Test question generation with multiple questions per line."""
        # Mock the _infer_no_context response with multiple questions per line
        self.model._infer_no_context.return_value = """
        Was the stroke confirmed to occur during birth? Yes. Was it localized to the left MCA? Yes, it was.
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0][0], "Was the stroke confirmed to occur during birth?"
        )
        self.assertEqual(result[0][1], "Yes")
        self.assertEqual(result[1][0], "Was it localized to the left MCA?")
        self.assertEqual(result[1][1], "Yes, it was")

    @pytest.mark.asyncio
    async def test_get_appeal_questions_no_question_mark(self):
        """Test question generation with text without question marks."""
        # Mock the _infer_no_context response with no question marks
        self.model._infer_no_context.return_value = """
        This treatment is necessary
        Patient history includes condition X
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], "This treatment is necessary?")
        self.assertEqual(result[0][1], "")
        self.assertEqual(result[1][0], "Patient history includes condition X?")
        self.assertEqual(result[1][1], "")

    @pytest.mark.asyncio
    async def test_get_appeal_questions_empty_response(self):
        """Test question generation with an empty response."""
        # Mock the _infer_no_context response with None
        self.model._infer_no_context.return_value = None

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result is an empty list
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_get_appeal_questions_rationale_format(self):
        """Test handling of 'Rationale for questions' in response."""
        # Mock the _infer_no_context response with 'Rationale for questions'
        self.model._infer_no_context.return_value = """
        Rationale for questions: These questions will help establish medical necessity.

        1. What is the patient's age?
        2. Has the patient tried conservative treatments?
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result is an empty list since we should reject responses with "Rationale for questions"
        self.assertEqual(result, [])

    @pytest.mark.asyncio
    async def test_get_appeal_questions_with_answer_prefix(self):
        """Test parsing questions with answer prefixes like 'A:' or ':'."""
        # Mock the _infer_no_context response
        self.model._infer_no_context.return_value = """
        What is the diagnosis code? A: J84.112
        Is this treatment FDA approved?: Yes it is
        """

        # Call the actual method
        result = await RemoteFullOpenLike.get_appeal_questions(
            self.model,
            denial_text="Test denial",
            procedure="Test procedure",
            diagnosis="Test diagnosis",
        )

        # Verify the result has correct question-answer pairs
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], "What is the diagnosis code?")
        self.assertEqual(result[0][1], "J84.112")
        self.assertEqual(result[1][0], "Is this treatment FDA approved?")
        self.assertEqual(result[1][1], "Yes it is")

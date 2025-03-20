import asyncio
import json
import io
from asgiref.sync import async_to_sync
from unittest.mock import Mock, patch
from typing import AsyncIterator, List
from fighthealthinsurance.common_view_logic import (
    RemoveDataHelper,
    FindNextStepsHelper,
    AppealsBackendHelper,
    NextStepInfo,
    DenialCreatorHelper,
)
from fighthealthinsurance.models import Denial, DenialTypes
import pytest
from django.test import TestCase


class TestCommonViewLogic(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.Denial.objects")
    def test_remove_data_for_email(self, mock_denial_objects):
        mock_denial = Mock()
        mock_denial_objects.filter.return_value.delete.return_value = 1
        RemoveDataHelper.remove_data_for_email("test@example.com")
        mock_denial_objects.filter.assert_called()
        mock_denial_objects.filter.return_value.delete.assert_called()

    @pytest.mark.django_db
    def test_find_next_steps(self):
        # Create real DenialTypes objects
        insurance_company_type = DenialTypes.objects.get(name="Insurance Company")
        medically_necessary_type = DenialTypes.objects.get(name="Medically Necessary")

        # Create a real Denial object
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        # Add denial types to the denial
        denial.denial_type.add(insurance_company_type)
        denial.denial_type.add(medically_necessary_type)

        # Call the function being tested with real objects
        next_steps = FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email=email,
            semi_sekret=denial.semi_sekret,
            procedure="prep",
            plan_id="1",
            denial_type=None,
            denial_date=None,
            diagnosis="high risk homosexual behaviour",
            insurance_company="evilco",
            claim_id=7,
        )

        # Verify the result
        self.assertIsInstance(next_steps, NextStepInfo)

        # Clean up the test data
        denial.delete()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals(self, mock_appeal_generator):
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
        )

        async def async_generator(items) -> AsyncIterator[str]:
            """Test helper: Async generator yielding items with delay."""
            for item in items:
                await asyncio.sleep(0.1)
                yield item

        async def test():
            mock_appeal_generator.generate_appeals.return_value = async_generator(
                ["test"]
            )
            responses = AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": 1,
                    "email": email,
                    "semi_sekret": denial.semi_sekret,
                }
            )
            buf = io.StringIO()

            async for chunk in responses:
                buf.write(chunk)

            buf.seek(0)
            string_data = buf.getvalue()

        async_to_sync(test)()

    # Skip for now until we enable this.
    @pytest.skip
    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    async def test_generate_appeal_questions(self, mock_appeal_generator):
        # Create a denial object for testing
        email = "test@example.com"
        denial = await Denial.objects.acreate(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            denial_text="This is a test denial for medical service",
            health_history="Patient has a history of condition X",
        )

        # Mock the get_appeal_questions method to return test questions with answers
        test_questions = [
            (
                "What medical evidence supports the necessity of this treatment?",
                "Clinical studies show efficacy",
            ),
            ("Has the patient tried alternative treatments?", ""),
            (
                "How does this treatment align with current medical guidelines?",
                "It follows AMA recommendations",
            ),
        ]

        # Configure the mock for the async function - we need to make it awaitable by setting it as a coroutine function
        async def mock_get_appeal_questions(*args, **kwargs):
            return test_questions

        mock_appeal_generator.get_appeal_questions = mock_get_appeal_questions

        # Call the method being tested
        questions = await DenialCreatorHelper.generate_appeal_questions(
            denial.denial_id
        )

        # Verify the questions were returned correctly
        self.assertEqual(questions, test_questions)

        # Verify the questions were stored in the denial object
        updated_denial = await Denial.objects.aget(denial_id=denial.denial_id)

        # Django's JSON serialization converts tuples to lists, so we need to convert
        # the test_questions to lists for comparison or the stored questions to tuples
        stored_questions_as_tuples = [
            (q[0], q[1]) if isinstance(q, list) else q
            for q in updated_denial.generated_questions
        ]
        self.assertEqual(stored_questions_as_tuples, test_questions)

        # Test that calling the method again doesn't call the ML model again
        # Reset the mock to verify it's not called again
        async def mock_get_appeal_questions_not_called(*args, **kwargs):
            self.fail("This mock should not be called")
            return []

        mock_appeal_generator.get_appeal_questions = (
            mock_get_appeal_questions_not_called
        )

        questions_again = await DenialCreatorHelper.generate_appeal_questions(
            denial.denial_id
        )

        # Verify we still get the same questions
        self.assertEqual(questions_again, test_questions)

        # Clean up
        await Denial.objects.filter(denial_id=1).adelete()

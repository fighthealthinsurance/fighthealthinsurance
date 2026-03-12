import asyncio
import json
import io
from asgiref.sync import async_to_sync
from unittest.mock import Mock, patch, AsyncMock
from typing import AsyncIterator, List
from fighthealthinsurance.common_view_logic import (
    FindNextStepsHelper,
    AppealsBackendHelper,
    NextStepInfo,
    DenialCreatorHelper,
)
from fighthealthinsurance.helpers import SendFaxHelper, RemoveDataHelper
from fighthealthinsurance.models import Denial, DenialTypes, Appeal, FaxesToSend
import pytest
from django.test import TestCase


class TestCommonViewLogic(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def _create_test_denial(self, denial_id, gen_attempts=0):
        """Helper to create a test denial with specified gen_attempts."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=denial_id,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=gen_attempts,
        )
        return email, denial

    @staticmethod
    async def collect_appeal_responses(data):
        """Collect all responses from generate_appeals into categorized lists."""
        status_messages = []
        appeal_contents = []
        raw_chunks = []
        async for chunk in AppealsBackendHelper.generate_appeals(data):
            raw_chunks.append(chunk)
            if not chunk or not chunk.strip():
                continue
            try:
                parsed = json.loads(chunk)
                if parsed.get("type") == "status":
                    status_messages.append(parsed)
                elif "content" in parsed:
                    appeal_contents.append(parsed["content"])
            except json.JSONDecodeError:
                pass
        return status_messages, appeal_contents, raw_chunks

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

    @pytest.mark.django_db
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    def test_store_fax_number_as_destination(self, mock_fax_actor_ref):
        """Test that the fax number from a denial is stored as the destination in FaxesToSend."""
        # Create test data
        email = "test@example.com"
        fax_number = "1234567890"

        # Create a denial with a fax number
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            appeal_fax_number=fax_number,
        )

        # Create an appeal
        appeal = Appeal.objects.create(
            for_denial=denial,
            appeal_text="Test appeal text",
            hashed_email=Denial.get_hashed_email(email),
        )

        # Set up mock
        mock_fax_actor_ref.get.do_send_fax.remote.return_value = None

        # Call the method under test
        result = SendFaxHelper.stage_appeal_as_fax(appeal=appeal, email=email)

        # Verify the fax was created with the correct destination
        fax = FaxesToSend.objects.get(uuid=result.uuid)
        self.assertEqual(fax.destination, fax_number)
        self.assertEqual(fax.denial_id, denial)
        self.assertEqual(fax.appeal_text, appeal.appeal_text)

        # Verify the fax actor was called to send the fax
        mock_fax_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            fax.hashed_email, str(fax.uuid)
        )

    @pytest.mark.django_db
    @patch("fighthealthinsurance.helpers.fax_helpers.fax_actor_ref")
    def test_resend_sets_should_send_and_sent_flags(self, mock_fax_actor_ref):
        """Test that resend properly sets should_send=True and sent=False."""
        # Create test data
        email = "test@example.com"
        hashed_email = Denial.get_hashed_email(email)
        fax_number = "9876543210"
        new_fax_number = "1234567890"

        # Create a denial
        denial = Denial.objects.create(
            denial_id=1,
            semi_sekret="sekret",
            hashed_email=hashed_email,
            appeal_fax_number=fax_number,
        )

        # Create an appeal
        appeal = Appeal.objects.create(
            for_denial=denial,
            appeal_text="Test appeal text for resend",
            hashed_email=hashed_email,
        )

        # Create a fax that was already sent (simulating a previously sent fax)
        fax = FaxesToSend.objects.create(
            hashed_email=hashed_email,
            email=email,
            appeal_text=appeal.appeal_text,
            destination=fax_number,
            denial_id=denial,
            for_appeal=appeal,
            paid=True,
            sent=True,  # Already sent
            should_send=False,  # Should not send again
        )

        # Set up mock
        mock_fax_actor_ref.get.do_send_fax.remote.return_value = None

        # Call the resend method (uuid must be string per method signature)
        result = SendFaxHelper.resend(new_fax_number, str(fax.uuid), hashed_email)

        # Verify the method returned True
        self.assertTrue(result)

        # Verify the fax was updated correctly
        updated_fax = FaxesToSend.objects.get(uuid=fax.uuid)
        self.assertEqual(updated_fax.destination, new_fax_number)
        self.assertTrue(updated_fax.should_send)
        self.assertFalse(updated_fax.sent)

        # Verify the fax actor was called to send the fax
        mock_fax_actor_ref.get.do_send_fax.remote.assert_called_once_with(
            hashed_email, str(fax.uuid)
        )

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.MLAppealQuestionsHelper.generate_questions_for_denial",
        new_callable=AsyncMock,
    )
    def test_generate_appeal_questions(self, mock_generate_questions, mock_fire_forget):
        """Test that generate_appeal_questions generates and stores questions."""
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
        mock_generate_questions.return_value = test_questions

        async def test():
            email = "test@example.com"
            denial = await Denial.objects.acreate(
                denial_id=99,
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email(email),
                denial_text="This is a test denial for medical service",
                health_history="Patient has a history of condition X",
            )

            try:
                # Call the method being tested
                questions = await DenialCreatorHelper.generate_appeal_questions(
                    denial.denial_id
                )

                # Verify the questions were returned correctly
                self.assertEqual(questions, test_questions)

                # Verify ML helper was called
                mock_generate_questions.assert_called_once()

                # Verify the questions were stored in the denial object
                updated_denial = await Denial.objects.aget(denial_id=denial.denial_id)

                # Django's JSON serialization converts tuples to lists
                stored_questions_as_tuples = [
                    (q[0], q[1]) if isinstance(q, list) else q
                    for q in updated_denial.generated_questions
                ]
                self.assertEqual(stored_questions_as_tuples, test_questions)
            finally:
                await Denial.objects.filter(denial_id=99).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals_skips_research_on_high_gen_attempts(
        self, mock_appeal_generator
    ):
        """Test that research phase is skipped when gen_attempts >= 3."""
        email, denial = self._create_test_denial(12, gen_attempts=3)
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                status_messages, appeal_contents, _ = (
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 12,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                )

                # Check for the skip message
                research_messages = [
                    m for m in status_messages if m.get("phase") == "research"
                ]
                assert (
                    len(research_messages) > 0
                ), f"Should have a research skip status, got: {status_messages}"
                skip_msg = research_messages[0]
                assert (
                    skip_msg.get("substep") == "all"
                ), f"Skip message should have substep 'all': {skip_msg}"
                assert (
                    "skip" in skip_msg.get("message", "").lower()
                ), f"Skip message should mention 'skip': {skip_msg}"
            finally:
                await Denial.objects.filter(denial_id=12).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch("fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt")
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generate_appeals_has_phase_field(
        self, mock_appeal_generator, mock_pmt, mock_ml_citations, mock_rag
    ):
        """Test that status messages include the phase field."""
        mock_pmt.find_context_for_denial = AsyncMock(return_value=None)
        email, denial = self._create_test_denial(13, gen_attempts=0)
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                status_messages, _, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 13,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )

                # Verify phase field is present on status messages
                statuses_with_phase = [s for s in status_messages if "phase" in s]
                assert (
                    len(statuses_with_phase) > 0
                ), f"Status messages should include 'phase' field, got: {status_messages}"

                # Verify phase progression order
                phases_seen = []
                for msg in status_messages:
                    phase = msg.get("phase")
                    if phase and (not phases_seen or phases_seen[-1] != phase):
                        phases_seen.append(phase)
                assert (
                    phases_seen[0] == "init"
                ), f"First phase should be 'init', got: {phases_seen}"
                assert (
                    "generating" in phases_seen
                ), f"Should see 'generating' phase, got: {phases_seen}"
            finally:
                await Denial.objects.filter(denial_id=13).adelete()

        async_to_sync(test)()

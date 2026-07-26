import asyncio
import json
import io
from contextlib import contextmanager
from asgiref.sync import async_to_sync
from unittest.mock import Mock, patch, AsyncMock
from typing import AsyncIterator, List
from fighthealthinsurance.common_view_logic import (
    FindNextStepsHelper,
    AppealsBackendHelper,
    NextStepInfo,
    DenialCreatorHelper,
)
from fighthealthinsurance.utils import (
    MIN_APPEAL_CHARS,
    appeal_has_words,
    appeal_word_count,
    is_real_appeal,
    meaningful_appeal_length,
    warn_unusable_appeal,
)
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.helpers import SendFaxHelper, RemoveDataHelper
from fighthealthinsurance.models import (
    Denial,
    DenialTypes,
    Appeal,
    FaxesToSend,
    ProposedAppeal,
    Regulator,
)
import pytest
from django.test import TestCase


@contextmanager
def capture_logs(level="INFO"):
    """Capture loguru output for the duration of the block."""
    from loguru import logger as loguru_logger

    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level=level)
    try:
        yield sink
    finally:
        loguru_logger.remove(handler_id)


class TestCommonViewLogic(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    # Module-level helper, exposed on the class so the existing `self.` call
    # sites keep working.
    _capture_logs = staticmethod(capture_logs)

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
    def _fast_keepalive():
        """Patch the generating-phase heartbeat cadence from 15s down to 50ms.

        Returns a context manager. The real ``keepalive_frames`` still runs --
        only the interval shrinks -- so tests that depend on heartbeat-driven
        behavior (heartbeat frames, the speculative-fallback checkpoints that
        ride on them) exercise the production code path without a 15s sleep.
        """
        from fighthealthinsurance import utils as _fhi_utils

        real_keepalive = _fhi_utils.keepalive_frames

        def fast_keepalive(task, *, interval=15.0, overall_timeout=None, **kwargs):
            return real_keepalive(
                task, interval=0.05, overall_timeout=overall_timeout, **kwargs
            )

        return patch(
            "fighthealthinsurance.common_view_logic.keepalive_frames",
            fast_keepalive,
        )

    @staticmethod
    def _live_drafts(texts: List[str]) -> List[GeneratedAppeal]:
        """Wrap plain strings as the GeneratedAppeal drafts make_appeals emits."""
        return [
            GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
            for t in texts
        ]

    @classmethod
    def _slow_make_appeals(cls, seconds: float, texts: List[str]):
        """A make_appeals side effect that blocks ``seconds`` then yields ``texts``.

        The sleep runs in the database_sync_to_async threadpool, so it does NOT
        block the event loop -- the generating-phase keepalive loop (and the
        checkpoints on it) keep running meanwhile.
        """
        import time as _time

        def slow_make_appeals(*args, **kwargs):
            _time.sleep(seconds)
            return iter(cls._live_drafts(texts))

        return slow_make_appeals

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
    def test_find_next_steps_populates_pharmacy_suggestion_for_drug_denial(self):
        """For a denial whose procedure is a known drug (Wegovy), the
        next-steps payload should carry a PharmacyCouponSuggestion so the
        consumer flow can surface GoodRx / Cost Plus / Crush Cost / Amazon
        Pharmacy."""
        from fighthealthinsurance.pharmacy_coupon_detector import (
            PharmacyCouponSuggestion,
        )

        email = "drug-denial@example.com"
        denial = Denial.objects.create(
            denial_id=2001,
            semi_sekret="sekret-drug",
            hashed_email=Denial.get_hashed_email(email),
            procedure="Wegovy",
            diagnosis="Obesity",
            denial_text="Wegovy denied as non-formulary.",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert isinstance(
                next_steps.pharmacy_coupon_suggestion, PharmacyCouponSuggestion
            )
            assert next_steps.pharmacy_coupon_suggestion.drug_name == "wegovy"
            # Pin by name set rather than count: option count is fragile as
            # new pharmacy discount programs (e.g. Crush Cost) are added.
            opt_names = {
                opt.name
                for opt in next_steps.pharmacy_coupon_suggestion.pharmacy_options
            }
            assert {
                "GoodRx",
                "Mark Cuban Cost Plus Drugs",
                "Crush Cost",
                "Amazon Search",
            } <= opt_names
        finally:
            denial.delete()

    @pytest.mark.django_db
    def test_find_next_steps_no_pharmacy_suggestion_for_non_drug_denial(self):
        """For a non-drug denial (MRI of knee), no pharmacy suggestion is
        produced and the field stays None - the partial template renders
        nothing in that case."""
        email = "mri-denial@example.com"
        denial = Denial.objects.create(
            denial_id=2002,
            semi_sekret="sekret-mri",
            hashed_email=Denial.get_hashed_email(email),
            procedure="MRI of knee",
            diagnosis="Knee pain",
            denial_text="MRI denied as not medically necessary.",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert next_steps.pharmacy_coupon_suggestion is None
            # MRI denials shouldn't surface a financial-assistance section
            # either - has_specific_matches() gates on diagnosis/drug/state
            # matches, and "Knee pain" doesn't match any catalog entry.
            assert next_steps.financial_assistance is None
        finally:
            denial.delete()

    @pytest.mark.django_db
    def test_find_next_steps_populates_financial_assistance_for_hiv_truvada(self):
        """A Truvada (PrEP) denial with an HIV-related diagnosis must
        surface the financial-assistance directory on the consumer flow:
        ADAP / Ryan White matches via the diagnosis keyword, the state
        Medicaid pathway attaches when ``your_state`` is set, and the
        general copay-foundation catalog rides along."""
        from fighthealthinsurance.financial_assistance_directory import (
            FinancialAssistanceResults,
        )

        email = "truvada-prep@example.com"
        denial = Denial.objects.create(
            denial_id=2003,
            semi_sekret="sekret-truvada",
            hashed_email=Denial.get_hashed_email(email),
            procedure="Truvada",
            diagnosis="HIV pre-exposure prophylaxis",
            denial_text="Truvada denied as non-formulary.",
            your_state="CA",
        )
        try:
            next_steps = FindNextStepsHelper.find_next_steps_for_denial(denial, email)
            assert isinstance(
                next_steps.financial_assistance, FinancialAssistanceResults
            )
            diagnosis_names = [
                p.name for p in next_steps.financial_assistance.diagnosis_specific
            ]
            assert any("ADAP" in n for n in diagnosis_names), diagnosis_names
            safety_net_names = [
                p.name for p in next_steps.financial_assistance.safety_net
            ]
            assert any("Ryan White" in n for n in safety_net_names), safety_net_names
            # State pathway attached for CA via state_help.
            assert next_steps.financial_assistance.state_medicaid_name is not None
        finally:
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

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_generating_phase_emits_heartbeats_during_slow_make_appeals(
        self, mock_appeal_generator
    ):
        """Regression for the silent-generation bug: while the blocking
        make_appeals runs, the generating phase must keep emitting status
        frames so the client's 90s inactivity watchdog never fires."""
        email, denial = self._create_test_denial(14, gen_attempts=3)
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.0,
            ["Dear Insurance Company, this is a sufficiently long appeal letter."],
        )

        async def test():
            try:
                with self._fast_keepalive():
                    status_messages, _, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 14,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )

                # Heartbeats are generating-phase frames whose message reports
                # elapsed seconds. At least a couple must have fired during the
                # ~1s make_appeals sleep.
                heartbeats = [
                    m
                    for m in status_messages
                    if m.get("phase") == "generating"
                    and "elapsed" in m.get("message", "")
                ]
                assert len(heartbeats) >= 2, (
                    f"Expected heartbeat frames during slow make_appeals, got: "
                    f"{status_messages}"
                )
            finally:
                await Denial.objects.filter(denial_id=14).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_init_and_done_frames_carry_generation_id(self, mock_appeal_generator):
        """The correlation id is emitted in the init frame (captured before any
        timeout) and echoed in the done frame alongside generating-phase
        timing, so a client report can be joined to the server trace."""
        email, denial = self._create_test_denial(15, gen_attempts=3)
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is a sufficiently long appeal letter."]
        )

        async def test():
            try:
                status_messages, _, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 15,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                init = [m for m in status_messages if m.get("phase") == "init"]
                done = [m for m in status_messages if m.get("phase") == "done"]
                assert init and init[0].get("generation_id"), status_messages
                assert done, status_messages
                assert done[0].get("generation_id") == init[0].get("generation_id")
                assert "make_appeals_seconds" in done[0]
            finally:
                await Denial.objects.filter(denial_id=15).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_speculative_excluded_from_existing_and_served_as_fallback(
        self, mock_appeal_generator
    ):
        """A speculative row is held back from the normal existing-appeals loop
        but promoted (and flipped to permanent) when the live run
        underdelivers."""
        email, denial = self._create_test_denial(16, gen_attempts=3)
        # A normal existing appeal -> served as `old`.
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="An existing non-speculative appeal letter here.",
            speculative=False,
        )
        # A held-back speculative appeal -> served only via the fallback.
        spec = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A speculative fallback appeal letter goes here.",
            speculative=True,
            context_level="speculative",
        )
        # Live generation produces nothing -> underdelivered.
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                status_messages, appeal_contents, _ = (
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 16,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                )
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                # The non-speculative existing appeal is served as `old`; the
                # speculative one is NOT (it's promoted as `new` via fallback).
                self.assertEqual(done["existing_appeals"], 1)
                self.assertEqual(done["new_appeals"], 1)
                joined = " ".join(appeal_contents)
                self.assertIn("A speculative fallback appeal letter goes here.", joined)
                # The speculative row was promoted to permanent.
                await spec.arefresh_from_db()
                self.assertFalse(spec.speculative)
            finally:
                await Denial.objects.filter(denial_id=16).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_speculative_not_served_when_live_run_delivers_enough(
        self, mock_appeal_generator
    ):
        """When the live run delivers enough appeals (>= threshold), the
        held-back speculative reserve is a "mini" row and stays held back --
        padding an already-sufficient result with weaker reduced-context drafts
        adds no value."""
        email, denial = self._create_test_denial(17, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="Held-back speculative draft that must not be served.",
            speculative=True,
            context_level="speculative",
        )
        # The live run below delivers 3 appeals, so we're at/over threshold and
        # the mini/speculative reserve is not served. gen_attempts=0 keeps the
        # research phase on; the enrichment sources are patched to None below
        # purely for test isolation (no real ML calls), not because the gate
        # depends on them anymore.
        Denial.objects.filter(denial_id=17).update(
            plan_documents_summary="Some gathered plan context.", gen_attempts=0
        )
        mock_appeal_generator.make_appeals.return_value = iter(
            [
                GeneratedAppeal(
                    text=f"A live internal appeal letter number {i} here.",
                    model_name="fhi-internal",
                    context_level="full",
                )
                for i in range(3)
            ]
        )

        async def test():
            try:
                # Skip the research gather cleanly (gen_attempts=0 would run it),
                # so patch the enrichment sources to None but keep the plan
                # summary as the gathered-data signal.
                with patch(
                    "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
                    new_callable=AsyncMock,
                    return_value=None,
                ), patch(
                    "fighthealthinsurance.common_view_logic."
                    "MLCitationsHelper.generate_citations_for_denial",
                    new_callable=AsyncMock,
                    return_value=None,
                ), patch(
                    "fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt"
                ) as mock_pmt:
                    mock_pmt.find_context_for_denial = AsyncMock(return_value=None)
                    status_messages, _, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 17,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                # 3 live appeals, no fallback -> speculative stays held back.
                self.assertGreaterEqual(done["new_appeals"], 3)
                spec = await ProposedAppeal.objects.aget(
                    for_denial=denial, speculative=True
                )
                self.assertTrue(spec.speculative)
            finally:
                await Denial.objects.filter(denial_id=17).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reserve_is_served_when_make_appeals_raises(self, mock_appeal_generator):
        """A raising make_appeals is the likeliest failure the reserve exists
        for. It used to propagate straight out of the generator, skipping
        synthesis AND the reconciliation -- so the user got an error even though
        we had drafts ready. The stream must instead end with a proper done frame
        carrying the reserve."""
        email, denial = self._create_test_denial(19, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve appeal held for exactly this failure.",
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.side_effect = RuntimeError(
            "every backend is down"
        )

        async def test():
            try:
                status_messages, appeal_contents, _ = (
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 19,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                )
                done = [m for m in status_messages if m.get("phase") == "done"]
                self.assertTrue(done, "must still emit a done frame, not blow up")
                self.assertEqual(done[0]["new_appeals"], 1)
                self.assertIn(
                    "A reserve appeal held for exactly this failure.",
                    " ".join(appeal_contents),
                )
                spec = await ProposedAppeal.objects.aget(for_denial=denial)
                self.assertFalse(spec.speculative, "served rows are promoted")
            finally:
                await Denial.objects.filter(denial_id=19).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reconciliation_caps_mini_rows_at_threshold(self, mock_appeal_generator):
        """When the live run underdelivers, held-back speculative ("mini") rows
        are promoted only up to the threshold; the surplus stays held back."""
        email, denial = self._create_test_denial(18, gen_attempts=3)
        for i in range(5):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=(
                    f"Speculative reserve appeal number {i} with sufficient length."
                ),
                speculative=True,
                context_level="speculative",
            )
        # Live run produces nothing -> heavily underdelivered.
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                status_messages, _, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 18,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                # Exactly ENOUGH_APPEALS (3) promoted, not all 5.
                self.assertEqual(done["new_appeals"], 3)
                promoted = await ProposedAppeal.objects.filter(
                    for_denial=denial, speculative=False
                ).acount()
                held = await ProposedAppeal.objects.filter(
                    for_denial=denial, speculative=True
                ).acount()
                self.assertEqual(promoted, 3)
                self.assertEqual(held, 2)
            finally:
                await Denial.objects.filter(denial_id=18).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reserve_is_served_before_a_slow_live_draft_past_the_deadline(
        self, mock_appeal_generator
    ):
        """Once a run with nothing delivered passes the no-appeal deadline while short of
        ENOUGH_APPEALS, the reserve is handed over mid-flight -- ahead of a live
        draft that is still generating -- instead of being held to the end."""
        email, denial = self._create_test_denial(20, gen_attempts=3)
        spec = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve draft the user should get without waiting.",
            speculative=True,
            context_level="speculative",
        )
        live_text = "A slow live appeal letter that finishes much later on."
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.5, [live_text]
        )

        async def test():
            try:
                with self._fast_keepalive(), patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.5
                ):
                    status_messages, appeal_contents, _ = (
                        await self.collect_appeal_responses(
                            {
                                "denial_id": 20,
                                "email": email,
                                "semi_sekret": denial.semi_sekret,
                            }
                        )
                    )
                reserve_text = "A reserve draft the user should get without waiting."
                self.assertIn(reserve_text, appeal_contents)
                self.assertIn(live_text, appeal_contents)
                self.assertLess(
                    appeal_contents.index(reserve_text),
                    appeal_contents.index(live_text),
                    f"reserve must arrive first, got: {appeal_contents}",
                )
                # Served rows are promoted, exactly as the end-of-flow path does.
                await spec.arefresh_from_db()
                self.assertFalse(spec.speculative)
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                self.assertEqual(done["new_appeals"], 2)
                self.assertEqual(done["speculative_appeals"], 1)
            finally:
                await Denial.objects.filter(denial_id=20).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reserve_lands_under_target_once_the_longer_deadline_passes(
        self, mock_appeal_generator
    ):
        """A user holding one appeal is not empty-handed, so the reserve waits
        for the longer under-target deadline -- but once that passes while still
        short of ENOUGH_APPEALS, it lands rather than waiting out the run."""
        email, denial = self._create_test_denial(29, gen_attempts=3)
        # One appeal already on screen -> the no-appeal rule does not apply.
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The one appeal this user already has in hand now.",
            speculative=False,
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve draft for a run that stays under target.",
            speculative=True,
            context_level="speculative",
        )
        live_text = "A slow live appeal letter that finishes much later on."
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.5, [live_text]
        )

        async def test():
            try:
                with self._fast_keepalive(), patch.object(
                    AppealsBackendHelper,
                    "SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS",
                    0.5,
                ):
                    status_messages, appeal_contents, _ = (
                        await self.collect_appeal_responses(
                            {
                                "denial_id": 29,
                                "email": email,
                                "semi_sekret": denial.semi_sekret,
                            }
                        )
                    )
                reserve_text = "A reserve draft for a run that stays under target."
                self.assertIn(reserve_text, appeal_contents)
                self.assertLess(
                    appeal_contents.index(reserve_text),
                    appeal_contents.index(live_text),
                    f"reserve must arrive before the slow live draft, got: "
                    f"{appeal_contents}",
                )
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                self.assertEqual(done["speculative_appeals"], 1)
            finally:
                await Denial.objects.filter(denial_id=29).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_no_appeal_deadline_does_not_apply_once_one_appeal_is_in_hand(
        self, mock_appeal_generator
    ):
        """The short deadline is the empty-screen rescue only. With an appeal
        already delivered, passing it must NOT flush the reserve -- that run
        waits for the longer under-target deadline instead."""
        email, denial = self._create_test_denial(30, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The one appeal this user already has in hand now.",
            speculative=False,
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve draft that the short deadline must not send.",
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.0, []
        )

        async def test():
            try:
                # Short deadline already past, long deadline unreachable.
                with self._fast_keepalive(), patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ), patch.object(
                    AppealsBackendHelper,
                    "SPECULATIVE_FALLBACK_UNDER_TARGET_SECONDS",
                    3600.0,
                ):
                    status_messages, _, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 30,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                # The mid-flight flush announces itself; the end-of-flow
                # reconciliation (which still serves the reserve) does not.
                self.assertEqual(
                    [
                        m
                        for m in status_messages
                        if "Sending a draft appeal now" in m.get("message", "")
                    ],
                    [],
                )
            finally:
                await Denial.objects.filter(denial_id=30).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reserve_stays_held_back_before_the_deadline(self, mock_appeal_generator):
        """A run that is still inside its deadline keeps the reserve held back:
        the early fallback only fires for runs that are actually stalling."""
        email, denial = self._create_test_denial(21, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve draft that must not be served early here.",
            speculative=True,
            context_level="speculative",
        )
        live_texts = [
            f"A live appeal letter number {i} of sufficient length here."
            for i in range(3)
        ]
        mock_appeal_generator.make_appeals.return_value = iter(
            self._live_drafts(live_texts)
        )

        async def test():
            try:
                # The default deadlines are far beyond this fast run, so the
                # reserve is never reached: the live drafts cover the user.
                _, appeal_contents, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 21,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )
                self.assertNotIn(
                    "A reserve draft that must not be served early here.",
                    appeal_contents,
                )
                spec = await ProposedAppeal.objects.aget(
                    for_denial=denial, speculative=True
                )
                self.assertTrue(spec.speculative)
            finally:
                await Denial.objects.filter(denial_id=21).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reconnect_serves_the_reserve_without_waiting_out_a_deadline(
        self, mock_appeal_generator
    ):
        """A reconnecting socket restarts this run's clock, but not the user's
        wait. With the default (far-off) deadlines a first connect holds the
        reserve back; reconnect=True hands it over in the first frames."""
        email, denial = self._create_test_denial(32, gen_attempts=3)
        reserve_text = "A reserve draft owed to a user whose socket dropped."
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=reserve_text,
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.0, []
        )

        async def test():
            try:
                # Deadlines left at their defaults, so nothing here is "stalled"
                # by the usual rule -- only the reconnect flag can flush this.
                _, appeal_contents, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 32,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                        "reconnect": True,
                    }
                )
                self.assertIn(reserve_text, appeal_contents)
            finally:
                await Denial.objects.filter(denial_id=32).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_a_reserve_row_another_run_already_claimed_is_not_re_served(
        self, mock_appeal_generator
    ):
        """Promotion claims the row atomically. A row that a concurrent flow
        promoted after this one selected it (the overlap a reconnect makes
        likely, since the dropped socket's generator may still be draining)
        must not be served twice."""
        email, denial = self._create_test_denial(34, gen_attempts=3)
        reserve_text = "A reserve draft that two overlapping runs both found."
        spec = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=reserve_text,
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                # Stand in for the concurrent run: the row is selected as
                # speculative, then claimed by someone else before this flow
                # promotes it. Patching aupdate to report "no rows updated" is
                # exactly what the losing side of that race observes.
                real_filter = ProposedAppeal.objects.filter

                def filter_with_lost_claim(*args, **kwargs):
                    qs = real_filter(*args, **kwargs)
                    if "pk" in kwargs:
                        qs.aupdate = AsyncMock(return_value=0)
                    return qs

                with patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ), patch.object(
                    ProposedAppeal.objects,
                    "filter",
                    side_effect=filter_with_lost_claim,
                ):
                    _, appeal_contents, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 34,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                self.assertNotIn(reserve_text, appeal_contents)
                # The row is left exactly as the claim found it, for the run
                # that actually won it.
                await spec.arefresh_from_db()
                self.assertTrue(spec.speculative)
            finally:
                await Denial.objects.filter(denial_id=34).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_reconnect_holds_the_reserve_once_enough_appeals_are_in_hand(
        self, mock_appeal_generator
    ):
        """The reconnect waiver drops the deadline, not the ENOUGH_APPEALS
        condition: a user who already has a full set keeps their reserve held
        back even on a reconnect."""
        email, denial = self._create_test_denial(33, gen_attempts=3)
        for i in range(AppealsBackendHelper.ENOUGH_APPEALS):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"An appeal this user already has in hand, no {i}.",
                speculative=False,
                context_level="full",
            )
        reserve_text = "A reserve draft that a full set must keep held back."
        spec = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=reserve_text,
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                _, appeal_contents, _ = await self.collect_appeal_responses(
                    {
                        "denial_id": 33,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                        "reconnect": True,
                    }
                )
                self.assertNotIn(reserve_text, appeal_contents)
                await spec.arefresh_from_db()
                self.assertTrue(spec.speculative)
            finally:
                await Denial.objects.filter(denial_id=33).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_undeliverable_reserve_rows_are_not_counted_or_served(
        self, mock_appeal_generator
    ):
        """A reserve of junk is no safety net. Rows that fail the deliverability
        rule must be filtered out before the availability count and the serving
        gate see them, and must never reach the user -- covering both halves:
        the runt is dropped DB-side, the long identifier-echo row survives that
        and is caught by is_real_appeal at serve time."""
        email, denial = self._create_test_denial(31, gen_attempts=3)
        runt = "$1,250"
        wordless = "AB123456789 CD987654321 99213 11/02/2026 $1,250.00"
        real = "A reserve draft with real words the user should receive."
        for text in (runt, wordless, real):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=text,
                speculative=True,
                context_level="speculative",
            )
        mock_appeal_generator.make_appeals.side_effect = self._slow_make_appeals(
            1.5, []
        )

        async def test():
            try:
                with self._capture_logs() as sink, self._fast_keepalive(), patch.object(
                    AppealsBackendHelper,
                    "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS",
                    0.5,
                ):
                    _, appeal_contents, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 31,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )

                self.assertIn(real, appeal_contents)
                self.assertNotIn(runt, appeal_contents)
                self.assertNotIn(wordless, appeal_contents)
                # Undeliverable rows are never promoted: promotion is what makes
                # a row persist as a real appeal for every later call.
                for text in (runt, wordless):
                    row = await ProposedAppeal.objects.aget(
                        for_denial=denial, appeal_text=text
                    )
                    self.assertTrue(row.speculative, f"{text!r} must stay held back")
                # The availability figure counts the one row that could be
                # served, not all three.
                self.assertIn(
                    "speculative reserve available at start=1", sink.getvalue()
                )
            finally:
                await Denial.objects.filter(denial_id=31).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_early_served_reserve_is_not_fed_to_synthesis(self, mock_appeal_generator):
        """Promotion flips an early-served reserve row to speculative=False, but
        it must still not count toward the >=2 synthesis gate -- synthesizing
        the reduced-context precompute with itself is what that gate excludes."""
        email, denial = self._create_test_denial(22, gen_attempts=3)
        for i in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"Reserve draft number {i} with plenty of length here.",
                speculative=True,
                context_level="speculative",
            )
        # Live generation delivers nothing, so the only rows are the reserve.
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                with patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ):
                    status_messages, _, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 22,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                mock_appeal_generator.synthesize_appeals.assert_not_called()
                self.assertEqual(
                    [m for m in status_messages if m.get("phase") == "synthesizing"],
                    [],
                )
            finally:
                await Denial.objects.filter(denial_id=22).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_early_served_reserve_is_not_re_served_at_end_of_flow(
        self, mock_appeal_generator
    ):
        """The end-of-flow reconciliation must not ship a reserve row the early
        fallback already sent (both paths dedupe through the same served set)."""
        email, denial = self._create_test_denial(23, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The one and only reserve draft for this denial here.",
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                with patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ):
                    status_messages, appeal_contents, _ = (
                        await self.collect_appeal_responses(
                            {
                                "denial_id": 23,
                                "email": email,
                                "semi_sekret": denial.semi_sekret,
                            }
                        )
                    )
                self.assertEqual(
                    appeal_contents.count(
                        "The one and only reserve draft for this denial here."
                    ),
                    1,
                    f"reserve draft sent more than once: {appeal_contents}",
                )
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                self.assertEqual(done["new_appeals"], 1)
            finally:
                await Denial.objects.filter(denial_id=23).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_logs_reserve_availability_at_start_and_when_picked(
        self, mock_appeal_generator
    ):
        """A trace must show, up front, whether a reserve was available, and
        again at the end whether the run actually leaned on it."""
        email, denial = self._create_test_denial(26, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="The reserve draft this run is going to fall back on.",
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                with self._capture_logs() as sink:
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 26,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                logs = sink.getvalue()
                self.assertIn("speculative reserve available at start=1", logs)
                self.assertIn("picked 1 appeal(s) from the speculative reserve", logs)
                self.assertIn("available_at_start=1", logs)
            finally:
                await Denial.objects.filter(denial_id=26).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_logs_a_reserve_that_arrives_mid_generation(self, mock_appeal_generator):
        """The precompute is a detached actor, so a reserve can land after the
        run starts. That case must log start=0 and still report the pick."""
        email, denial = self._create_test_denial(27, gen_attempts=3)

        async def land_a_reserve_row(*args, **kwargs):
            # Runs on the event loop after the start-of-generation count and
            # before generation -- i.e. the precompute finishing mid-flight.
            await ProposedAppeal.objects.acreate(
                for_denial=denial,
                appeal_text="A reserve draft that landed while we generated.",
                speculative=True,
                context_level="speculative",
            )
            return None

        mock_appeal_generator.make_appeals.return_value = iter([])

        async def test():
            try:
                with self._capture_logs() as sink, patch(
                    "fighthealthinsurance.common_view_logic."
                    "MLAppealContextHelper.maybe_summarize_denial_text",
                    side_effect=land_a_reserve_row,
                ), patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ):
                    _, appeal_contents, _ = await self.collect_appeal_responses(
                        {
                            "denial_id": 27,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                logs = sink.getvalue()
                self.assertIn("speculative reserve available at start=0", logs)
                self.assertIn("arrived mid-generation", logs)
                self.assertIn(
                    "A reserve draft that landed while we generated.",
                    appeal_contents,
                )
            finally:
                await Denial.objects.filter(denial_id=27).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_logs_when_the_reserve_was_available_but_not_needed(
        self, mock_appeal_generator
    ):
        """A held-back reserve on a run that delivered is worth saying out loud
        too -- otherwise the only reserve signal in the logs is failure."""
        email, denial = self._create_test_denial(28, gen_attempts=3)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="A reserve draft this run will never need to use.",
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter(
            self._live_drafts(
                [
                    f"A live appeal letter number {i} of sufficient length here."
                    for i in range(3)
                ]
            )
        )

        async def test():
            try:
                with self._capture_logs() as sink:
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 28,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                logs = sink.getvalue()
                self.assertIn("did NOT need the speculative reserve", logs)
                self.assertIn("1 row(s) still held back", logs)
            finally:
                await Denial.objects.filter(denial_id=28).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_live_draft_matching_an_early_served_reserve_is_dropped(
        self, mock_appeal_generator
    ):
        """The precompute runs the same internal models on the same denial, so a
        live draft can come back identical to a reserve row already served. It
        must not be streamed again: the client dedupes by content, so counting
        it would make the done frame promise more appeals than are on screen."""
        email, denial = self._create_test_denial(24, gen_attempts=3)
        shared_text = "An appeal letter both the reserve and the live run produce."
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=shared_text,
            speculative=True,
            context_level="speculative",
        )
        mock_appeal_generator.make_appeals.return_value = iter(
            self._live_drafts([shared_text])
        )

        async def test():
            try:
                with patch.object(
                    AppealsBackendHelper, "SPECULATIVE_FALLBACK_NO_APPEAL_SECONDS", 0.0
                ):
                    status_messages, appeal_contents, _ = (
                        await self.collect_appeal_responses(
                            {
                                "denial_id": 24,
                                "email": email,
                                "semi_sekret": denial.semi_sekret,
                            }
                        )
                    )
                self.assertEqual(appeal_contents.count(shared_text), 1)
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                self.assertEqual(
                    done["new_appeals"],
                    1,
                    "the count must match what the client can actually show",
                )
            finally:
                await Denial.objects.filter(denial_id=24).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_late_live_draft_is_not_counted_as_a_speculative_appeal(
        self, mock_appeal_generator
    ):
        """The reconciliation also serves late FULL-context drafts. Those are
        live generation, so they must not be attributed to the reserve in the
        done frame's speculative_appeals count."""
        email, denial = self._create_test_denial(25, gen_attempts=3)
        late_text = "A late live draft that missed the streaming window here."
        # Two existing drafts so the run reaches the synthesis step, which is
        # where the late row is landed below.
        for i in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"An existing appeal draft number {i} of real length.",
                speculative=False,
                context_level="full",
            )
        mock_appeal_generator.make_appeals.return_value = iter([])

        async def land_a_late_full_context_row(*args, **kwargs):
            # Synthesis runs after the flow has snapshotted what it sent and
            # before the reconciliation, so a row written here is one no yield
            # path saw -- the late full-context draft the reconciliation's
            # non-mini branch exists to pick up. Returns None (no synthesized
            # text) so nothing else is added to the stream.
            await ProposedAppeal.objects.acreate(
                for_denial=denial,
                appeal_text=late_text,
                speculative=False,
                context_level="full",
            )
            return None

        mock_appeal_generator.synthesize_appeals = AsyncMock(
            side_effect=land_a_late_full_context_row
        )

        async def test():
            try:
                status_messages, appeal_contents, _ = (
                    await self.collect_appeal_responses(
                        {
                            "denial_id": 25,
                            "email": email,
                            "semi_sekret": denial.semi_sekret,
                        }
                    )
                )
                # The reconciliation served it (it is a real appeal nobody sent)...
                self.assertIn(late_text, appeal_contents)
                done = [m for m in status_messages if m.get("phase") == "done"][0]
                self.assertEqual(done["new_appeals"], 1)
                # ...but it is live generation, not reserve output.
                self.assertEqual(done["speculative_appeals"], 0)
            finally:
                await Denial.objects.filter(denial_id=25).adelete()

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
    def test_rag_lookup_forwards_cpt_and_hcpcs_codes(
        self, mock_appeal_generator, mock_pmt, mock_ml_citations, mock_rag
    ):
        """The RAG context builder must extract CPT *and* HCPCS Level II
        codes from denial text and forward them to the RAG service via
        ``procedure_codes``. A regression that drops HCPCS support (e.g.
        reverting to the CPT-only regex) would leave DME-coded denials
        without enriched RAG context."""
        mock_pmt.find_context_for_denial = AsyncMock(return_value=None)
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=14,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            gen_attempts=0,
            denial_text=(
                "Service with CPT 99213 and DME E0260 hospital bed denied. "
                "Diagnosis Z00.00 documented."
            ),
        )
        mock_appeal_generator.make_appeals.return_value = iter(
            ["Dear Insurance Company, this is an appeal."]
        )

        async def test():
            try:
                await self.collect_appeal_responses(
                    {
                        "denial_id": 14,
                        "email": email,
                        "semi_sekret": denial.semi_sekret,
                    }
                )

                mock_rag.assert_called()
                _args, kwargs = mock_rag.call_args
                forwarded_codes = kwargs.get("procedure_codes") or []
                # Both the CPT code and the HCPCS DME code should be
                # forwarded for RAG enrichment.
                assert (
                    "99213" in forwarded_codes
                ), f"CPT 99213 missing from procedure_codes: {forwarded_codes}"
                assert (
                    "E0260" in forwarded_codes
                ), f"HCPCS E0260 missing from procedure_codes: {forwarded_codes}"
                forwarded_diagnosis = kwargs.get("diagnosis_codes") or []
                assert "Z00.00" in forwarded_diagnosis, (
                    f"ICD-10 Z00.00 missing from diagnosis_codes: "
                    f"{forwarded_diagnosis}"
                )
            finally:
                await Denial.objects.filter(denial_id=14).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_procedure_and_diagnosis",
        new_callable=AsyncMock,
        return_value=("pembrolizumab", "melanoma"),
    )
    def test_extract_schedules_clinical_trials_prefetch(
        self, _mock_get_proc_diag, mock_fire_forget
    ):
        """``extract_set_denial_and_diagnosis`` must fire the ClinicalTrials
        prefetch alongside the existing PubMed and speculative-context tasks
        once procedure/diagnosis are populated."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=15,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            denial_text="Insurer denied pembrolizumab for melanoma as experimental.",
        )

        async def test():
            try:
                await DenialCreatorHelper.extract_set_denial_and_diagnosis(
                    denial.denial_id
                )

                # All three background tasks must have been scheduled.
                assert mock_fire_forget.await_count == 3, (
                    f"Expected 3 fire-and-forget tasks (pubmed, clinical "
                    f"trials, speculative context); saw "
                    f"{mock_fire_forget.await_count}"
                )
                scheduled_names = [
                    call.args[0].cr_code.co_name
                    for call in mock_fire_forget.await_args_list
                ]
                assert "find_clinical_trials" in scheduled_names, (
                    f"ClinicalTrials prefetch coroutine not scheduled; "
                    f"saw {scheduled_names}"
                )
                # Close the captured coroutines so pytest doesn't warn about
                # un-awaited coros from the mocked-out fire_and_forget.
                for call in mock_fire_forget.await_args_list:
                    call.args[0].close()
            finally:
                await Denial.objects.filter(denial_id=15).adelete()

        async_to_sync(test)()

    @pytest.mark.django_db
    @patch(
        "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
        new_callable=AsyncMock,
    )
    @patch(
        "fighthealthinsurance.common_view_logic.ClinicalTrialsTools.find_trials_for_denial",
        new_callable=AsyncMock,
        side_effect=RuntimeError("simulated upstream failure"),
    )
    @patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_procedure_and_diagnosis",
        new_callable=AsyncMock,
        return_value=("pembrolizumab", "melanoma"),
    )
    def test_clinical_trials_prefetch_swallows_failures(
        self, _mock_get_proc_diag, _mock_find_trials, mock_fire_forget
    ):
        """The ``find_clinical_trials`` inner coroutine must swallow every
        exception so the daemon thread that ``fire_and_forget_in_new_threadpool``
        spawns can never re-raise into the appeal flow."""
        email = "test@example.com"
        denial = Denial.objects.create(
            denial_id=16,
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(email),
            denial_text="Insurer denied pembrolizumab for melanoma as experimental.",
        )

        async def test():
            try:
                await DenialCreatorHelper.extract_set_denial_and_diagnosis(
                    denial.denial_id
                )

                # Locate the captured clinical-trials coroutine and run it
                # directly. If the inner try/except is correct, awaiting it
                # must not raise even though find_trials_for_denial throws.
                ct_coros = [
                    call.args[0]
                    for call in mock_fire_forget.await_args_list
                    if call.args[0].cr_code.co_name == "find_clinical_trials"
                ]
                assert len(ct_coros) == 1, (
                    f"Expected exactly one find_clinical_trials coro; "
                    f"got {len(ct_coros)}"
                )
                # Should complete cleanly despite the upstream RuntimeError.
                await ct_coros[0]

                # Close any remaining captured coros to avoid pytest warnings.
                for call in mock_fire_forget.await_args_list:
                    coro = call.args[0]
                    if coro.cr_code.co_name != "find_clinical_trials":
                        coro.close()
            finally:
                await Denial.objects.filter(denial_id=16).adelete()

        async_to_sync(test)()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("", False),
        ("   \t\n  ", False),  # whitespace-only
        ("ok", False),  # below threshold
        ("a" * (MIN_APPEAL_CHARS - 1), False),  # just below threshold
        ("a" * MIN_APPEAL_CHARS, True),  # at threshold (inclusive >=)
        (123, False),  # non-string
        (["a"] * 100, False),  # non-string
        ("          short          ", False),  # strip-then-measure
        ("a" * (MIN_APPEAL_CHARS + 1), True),  # just over threshold
        ("this is a long enough appeal text for delivery", True),
        # Internal whitespace doesn't count: 10 letters padded with spaces.
        ("a " * 10, False),
        # Control characters don't count: 10 letters + 50 NUL bytes.
        ("a" * 10 + "\x00" * 50, False),
        # Exactly MIN_APPEAL_CHARS letters with control padding stays valid.
        ("a" * MIN_APPEAL_CHARS + "\x07" * 20, True),
        # Tabs/newlines between letters are ignored (only 12 real letters).
        ("a\tb\nc d e f g h i j k l", False),
        # Long enough, but nothing except digits and punctuation: not words.
        ("1234-5678-90, 11/02/2026: $1,250.00", False),
        ("1" * 500, False),
        ("-" * 80, False),
        (".,;:!?()[]{}<>/\\-_=+*&^%$#@~", False),
        # One word among the identifiers is still not a letter.
        ("Denied. 99213 11/02/2026 -- $1,250.00", False),
        # Letter prefixes welded to identifiers are not words: this is the
        # identifier-echo output the filter exists to reject.
        ("AB123456789 CD987654321", False),
        ("H5521-001 BCBS12345 99213 $1,250.00", False),
        # Two words clears the floor: it is deliberately a worst-of-the-worst
        # bar, not a quality bar (real appeals run to hundreds of words).
        ("Claim #1234-5678-90 denied 11/02/2026 -- $1,250.00", True),
        # Non-Latin scripts count: one unspaced run of letters is prose.
        ("这是一封足够长的申诉信正文内容示例。", True),
    ],
)
def test_is_real_appeal(value, expected):
    assert is_real_appeal(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        (123, False),
        ("", False),
        ("   ", False),
        ("1234567890", False),
        ("$1,250.00 (11/02/2026)", False),
        ("---===---", False),
        ("a", False),  # a lone letter is not a word
        ("Plan A 1234", False),  # only one word-like token
        ("Claim 1234 denied", True),
        # Unspaced script: one run of MIN_APPEAL_CHARS+ letters counts as prose.
        ("这是一封足够长的申诉信正文内容示例", True),
        ("申诉信", False),  # one short run is not enough
        # Combining marks attach to the letter before them rather than being
        # letters themselves, so scripts that use them (Thai, Devanagari) must
        # still tokenize into words.
        ("ที่นี่ดี ที่นี่ดี ที่นี่ดี", True),
        ("नमस्ते दुनिया यह एक अपील है", True),
        # Unspaced AND mark-heavy: one token, whose length has to be measured
        # as written (24 characters) rather than on its 9 base letters.
        ("ที่นี่ดีที่นี่ดีที่นี่ดี", True),
        # ...but the length bar still applies: 8 characters is not prose.
        ("ที่นี่ดี", False),
    ],
)
def test_appeal_has_words(value, expected):
    assert appeal_has_words(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        (123, 0),
        ("", 0),
        ("1234-5678-90, 11/02/2026: $1,250.00", 0),
        ("Plan A 1234", 1),  # the lone "A" is not a token
        ("Claim 1234 denied", 2),
        ("this is a long enough appeal text for delivery", 8),  # "a" excluded
        # Letters welded to digits are identifier fragments, not words.
        ("AB123456789 CD987654321", 0),
        ("H5521-001 BCBS12345", 0),
        # ...but ordinary hyphenated and possessive prose still counts.
        ("well-documented", 2),
        # the / patient / rays; the possessive "s" and the lone "X" don't count
        ("the patient's X-rays", 3),
        ("COVID-19 treatment", 2),
        # Combining marks don't split a word: three Thai words, not zero.
        ("ที่นี่ดี ที่นี่ดี ที่นี่ดี", 3),
    ],
)
def test_appeal_word_count(value, expected):
    assert appeal_word_count(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0),
        (123, 0),
        ("", 0),
        ("   \t\n  ", 0),  # whitespace-only
        ("abc", 3),
        ("a b c", 3),  # internal spaces excluded
        ("  hi  ", 2),  # surrounding spaces excluded
        ("a\x00b\x07c", 3),  # control characters excluded
        ("a" * 15, 15),
        ("a" * 15 + "\n\n\n", 15),  # trailing whitespace excluded
    ],
)
def test_meaningful_appeal_length(value, expected):
    assert meaningful_appeal_length(value) == expected


def test_warn_unusable_appeal_logs_length_and_context():
    """The shared drop-site warning reports the measured length, the
    threshold, and the caller-supplied context. The reported length is the
    meaningful (non-whitespace) count, so internal spaces are not counted."""
    with capture_logs(level="WARNING") as sink:
        warn_unusable_appeal("a b c", "model='m' for denial 7")
    output = sink.getvalue()
    assert "too-short" in output
    assert "len=3" in output  # 3 letters, the 2 spaces are excluded
    assert f"< {MIN_APPEAL_CHARS} chars" in output
    assert "model='m' for denial 7" in output


def test_warn_unusable_appeal_reports_wordless_reason():
    """A long-enough but wordless appeal is reported as not-words rather
    than as too short."""
    with capture_logs(level="WARNING") as sink:
        warn_unusable_appeal(
            "1234-5678-90, 11/02/2026: $1,250.00", "model='m' for denial 7"
        )
    output = sink.getvalue()
    assert "not-words" in output
    assert "words=0" in output
    assert "too-short" not in output
    assert "model='m' for denial 7" in output


def test_warn_unusable_appeal_handles_non_string():
    """A non-string (e.g. None) is reported as length 0 without raising."""
    with capture_logs(level="WARNING") as sink:
        warn_unusable_appeal(None, "some context")
    output = sink.getvalue()
    assert "len=0" in output


class RegulatorContactInfoTest(TestCase):
    """Denials matched to a regulator surface that regulator's contact info."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def _make_denial(self, denial_text="denied"):
        return Denial.objects.create(
            denial_text=denial_text,
            hashed_email=Denial.get_hashed_email("regulator@example.com"),
        )

    def _outside_help_text(self, denial):
        details = FindNextStepsHelper._get_outside_help_details(denial)
        return " ".join(f"{option} {how}" for option, how in details)

    def test_extract_set_regulator_links_erisa_denial(self):
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA."
        )
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertEqual(getattr(denial.regulator, "alt_name", None), "ERISA")

    def test_extract_set_regulator_leaves_unmatched_denial_null(self):
        denial = self._make_denial("Nothing relevant in this text.")
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertIsNone(denial.regulator)

    def test_extract_set_regulator_does_not_overwrite_existing_match(self):
        cms = Regulator.objects.get(alt_name="CMS")
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA."
        )
        denial.regulator = cms
        denial.save()
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "CMS")

    def test_extract_set_regulator_multi_match_is_deterministic(self):
        """When denial text matches more than one regulator, the lowest-pk
        match wins deterministically (get_regulator orders by pk). ERISA is
        pk=1 and CMS pk=2 in the fixture, so ERISA wins."""
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA. "
            "Centers for Medicare and Medicaid."
        )
        async_to_sync(DenialCreatorHelper.extract_set_regulator)(denial.denial_id)
        denial.refresh_from_db()
        self.assertEqual(denial.regulator.alt_name, "ERISA")

    def test_extract_entity_sets_regulator_even_when_extraction_already_done(self):
        """extract_entity early-returns when procedure/diagnosis are already
        populated (e.g. entered manually); regulator matching must still run
        on that path so those denials get regulator contact info."""
        denial = self._make_denial(
            "You may have the right to file a civil action under ERISA."
        )
        denial.procedure = "MRI"
        denial.save()

        async def _consume():
            async for _ in DenialCreatorHelper.extract_entity(denial.denial_id):
                pass

        async_to_sync(_consume)()
        denial.refresh_from_db()
        self.assertEqual(getattr(denial.regulator, "alt_name", None), "ERISA")

    def test_outside_help_includes_regulator_phone_number(self):
        erisa = Regulator.objects.get(alt_name="ERISA")
        denial = self._make_denial()
        denial.regulator = erisa
        denial.save()
        self.assertIn("Call 1-866-444-3272", self._outside_help_text(denial))

    def test_outside_help_names_the_matched_regulator(self):
        erisa = Regulator.objects.get(alt_name="ERISA")
        denial = self._make_denial()
        denial.regulator = erisa
        denial.save()
        self.assertIn("Federal Department of Labor", self._outside_help_text(denial))

    def test_outside_help_has_no_contact_row_without_matched_regulator(self):
        denial = self._make_denial()
        self.assertNotIn("file a complaint online", self._outside_help_text(denial))

    def test_outside_help_escapes_html_in_regulator_name(self):
        """The outside-help rows render with autoescape off, so DB-sourced
        regulator fields must be escaped before being embedded in HTML."""
        regulator = Regulator.objects.create(
            name="Weird <b>Dept</b> & Co",
            website="https://example.com/",
            alt_name="WEIRD",
            regex="zzz-never-matches",
            negative_regex="",
            phone="1-800-555-0100",
        )
        denial = self._make_denial()
        denial.regulator = regulator
        denial.save()
        text = self._outside_help_text(denial)
        self.assertIn("Weird &lt;b&gt;Dept&lt;/b&gt; &amp; Co", text)

    def test_outside_help_does_not_linkify_non_http_website(self):
        regulator = Regulator.objects.create(
            name="Odd Regulator",
            website="javascript:alert(1)",
            alt_name="ODD",
            regex="zzz-never-matches",
            negative_regex="",
            phone="1-800-555-0101",
        )
        denial = self._make_denial()
        denial.regulator = regulator
        denial.save()
        self.assertNotIn("javascript:", self._outside_help_text(denial))

    def test_outside_help_linkifies_uppercase_scheme_website(self):
        """The scheme check must be case-insensitive so a valid HTTPS:// URL
        is still linkified rather than silently dropped."""
        regulator = Regulator.objects.create(
            name="Shouty Regulator",
            website="HTTPS://example.gov/complaint",
            alt_name="SHOUT",
            regex="zzz-never-matches",
            negative_regex="",
            phone="1-800-555-0102",
        )
        denial = self._make_denial()
        denial.regulator = regulator
        denial.save()
        self.assertIn("HTTPS://example.gov/complaint", self._outside_help_text(denial))

    def test_migration_backfill_fills_blank_regulator_phones(self):
        """The regulator-phone data migration must backfill phones for
        pre-existing deployments whose seeded rows predate the fixture
        change; keep its identifier→phone mapping in sync with the fixture."""
        import importlib

        from django.apps import apps

        migration = importlib.import_module(
            "fighthealthinsurance.migrations.0194_regulator_phone"
        )
        Regulator.objects.update(phone="")
        migration._seed_regulator_phones(apps, None)
        phones = set(Regulator.objects.values_list("phone", flat=True))
        self.assertEqual(
            phones,
            {
                "1-866-444-3272",
                "1-800-633-4227",
                "1-888-466-2219",
                "1-800-368-1019",
            },
        )
